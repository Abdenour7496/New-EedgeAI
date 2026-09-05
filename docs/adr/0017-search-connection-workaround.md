# ADR 0017: Search's stale-connection bug — root cause not found, mitigated with a scheduled self-restart

**Status:** Accepted (mitigation only — root cause still open)
**Date:** 2026-08-27/28
**Decision owners:** EedgeAI maintainers

## Context

ADR 0015 flagged a real gap; ADR 0016 attempted a fix (FalkorDB connection
health-checking) that turned out not to resolve it. Asked to address it
directly, properly this time — a full, rigorous investigation, documented
honestly including what didn't work.

### What was ruled out, with direct evidence (not guesses)

Every layer was tested in isolation against real, live data:

- **Embedding storage**: initially suspected — a direct fetch showed
  `fact_embedding` as `None`. False lead: graphiti-core deliberately omits
  it from generic fetches (`load_fact_embedding()` needed explicitly).
  Confirmed correctly stored (768 dims) once actually loaded.
- **Similarity scoring**: computed the real cosine similarity between a
  query and its matching stored embedding directly — 0.84–0.93, well
  above the 0.6 minimum-score threshold, every time.
- **The raw FalkorDB cosine-similarity Cypher query**: constructed
  graphiti-core's exact query and ran it directly — found the target fact
  as the #1 result, consistently.
- **The BM25/fulltext branch**: ran directly — also found the target
  fact, ranked #1–2, consistently.
- **The full orchestrated `search()` function** (RRF fusion of both
  branches): called directly with a freshly-constructed `Graphiti`
  instance in a brand-new Python process — found the target fact
  correctly, in early testing consistently.
- **ADR 0016's fix (health-checking the FalkorDB connection pool)**:
  reproduced the original failure again within minutes of a restart with
  that fix live. Ruled out as insufficient.
- **A fresh `Graphiti`/`FalkorDB` client constructed per search call, but
  still on the app's own event loop**: reproduced the failure 5/5 and
  later 8/8 times in controlled repeat testing. Ruled out.
- **Running that fresh-instance search in a dedicated thread with its own
  isolated `asyncio` event loop** (replicating the isolation of a separate
  process without the cost of actually spawning one): reproduced the
  failure 8/8 times regardless. Ruled out.
- **Patience**: retried a single still-missing fact every 15s for 135s
  straight — never appeared. Ruled out a pure propagation-delay
  explanation.

### What has *always* worked, every single time tested

A genuinely separate OS process (`docker exec ... python3 -c "..."`,
constructing its own fresh `Graphiti` instance) found every fact tested
against it — immediately, correctly ranked, no exceptions — even
immediately after the exact same fact had just failed to appear via the
running app's `/search` endpoint, with or without any of the in-process
fixes above active.

## Decision

**No in-process fix was found.** The evidence points at something
concurrency- or process-level in the async FalkorDB/Redis client (this
app's event loop also juggles chat traffic, ingest requests, health
checks, and metrics scraping concurrently — a genuinely separate process
never has any of that) rather than a simple stale-connection or
stale-object problem, since isolating just the object (fresh instance)
and just the event loop (dedicated thread) both failed to reproduce a
separate process's reliability. `/search` reverted to the simple form
(the long-lived `app.state.graphiti`, same as every other route) rather
than keep unproven complexity that added real overhead (a thread + a
second event loop per search call) for no measured benefit.

Mitigated instead with a scheduled self-restart:
`graphiti/app.py`'s lifespan now starts a background task
(`_periodic_self_restart`) that calls `os._exit(0)` after
`SEARCH_RESTART_INTERVAL_SECONDS` (default 900s / 15 min; `0` disables
it). Verified directly that this actually recovers automatically before
relying on it: `docker kill` from *outside* the container is correctly
**not** restarted by `restart: unless-stopped` (that policy deliberately
respects an explicit external stop) — but killing the container's own PID
1 *from inside* (`docker exec ... kill -9 1`, the same kind of event
`os._exit(0)` produces) triggered an automatic restart, healthy again
within a couple of seconds.

## Consequences

- This bounds the damage — at most `SEARCH_RESTART_INTERVAL_SECONDS` of
  possible search staleness — without fixing the actual bug. The next
  real step is either a proper upstream bug report (to `graphiti-core` or
  `falkordb-py`, with this ADR's reproduction steps) or much deeper
  systems-level debugging (packet capture, strace on the Redis socket
  under concurrent load) than was practical in this pass.
- A scheduled restart every 15 minutes means a brief window (health check
  passes in well under a minute) where `/search` is unavailable — the
  main chat pipeline's own resilience (falls back to general knowledge
  when Graphiti retrieval is empty, per `build_gcor_context()` in
  `proxy/main.py`) should absorb this, but it's a real, deliberate
  trade-off, not a free lunch.
- Ingestion was never observed to have this symptom, all session, so it
  keeps using `app.state.graphiti` unchanged — this restart schedule
  exists for `/search`'s sake specifically, not because writes are
  suspected too.
- If a future investigation narrows this down further, revisit whether
  the restart interval can be lengthened or removed — it's a stopgap,
  not a permanent architectural choice.
