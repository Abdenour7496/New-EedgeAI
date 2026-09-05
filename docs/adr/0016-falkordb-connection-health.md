# ADR 0016: FalkorDB connection health-checking, for the "search can't find what was just ingested" gap

**Status:** Accepted
**Date:** 2026-08-27
**Decision owners:** EedgeAI maintainers

## Context

ADR 0015 flagged, but deliberately didn't chase, a real gap: freshly
ingested facts weren't reliably found by `/api/search` — or even Graphiti's
own raw `/search` — despite the edge itself independently confirmed to
exist. Asked to address it directly.

Traced the entire pipeline end to end, disproving each layer in turn by
testing it in isolation against the real, live data:

1. **Embedding storage** — suspected first, since a direct fetch showed
   `fact_embedding` as `None`. False lead: graphiti-core deliberately does
   not return `fact_embedding` on generic fetches (a documented
   performance choice — `load_fact_embedding()` must be called
   explicitly). Confirmed correctly stored once actually loaded (768
   dims).
2. **Similarity scoring** — computed the real cosine similarity between
   the query and the stored embedding directly: 0.84, well above the
   0.6 minimum-score threshold. Not the problem.
3. **The raw FalkorDB cosine-similarity Cypher query** — constructed and
   ran graphiti-core's exact query directly against FalkorDB: found the
   fact as the #1 result. Not the problem.
4. **The BM25/fulltext branch** — ran directly: also found the fact,
   ranked #1-2. Not the problem.
5. **A brand-new `Graphiti` instance** (fresh `create_graphiti()` call,
   same FalkorDB, same config) — called `.search()` on it directly:
   found the fact immediately, correctly ranked. **This is the key
   result**: the search *logic* is entirely correct. The bug is specific
   to the app's own long-lived instance.
6. **The actual running app's `/search` endpoint**, tested again
   immediately after step 5 (which required restarting the `graphiti`
   container to load diagnostic instrumentation): now found the fact
   correctly too. Confirmed this wasn't just "enough time had passed" —
   ingested and immediately searched for a brand-new fact right after the
   restart: found in 0.2s, correctly ranked #1.

Conclusion: something in the long-lived `app.state.graphiti` instance's
FalkorDB connection degrades under sustained real usage (this session's
container had been up for many operations — ingests, searches, deletes,
concurrent chat traffic), silently causing search to miss recent writes,
while a fresh connection against the identical data works perfectly.
Restarting the container was an accidental, real fix — but not a
sustainable one.

Traced the likely mechanism: `graphiti_core.driver.falkordb_driver.FalkorDriver`
constructs its own `falkordb.asyncio.FalkorDB` client with every
connection-pool tuning parameter left at the library's defaults —
including `health_check_interval=0` (disabled). With health-checking off,
a connection that goes stale in the pool (a dropped socket, a Redis-side
timeout, anything short of an outright connection error) is never
detected or replaced — it just sits there and gets reused, silently
returning whatever it can rather than surfacing an error.

## Decision

`graphiti/app.py`'s `create_graphiti()` now constructs the
`falkordb.asyncio.FalkorDB` client itself, with health-checking enabled
(`health_check_interval`, default 30s — configurable via
`FALKORDB_HEALTH_CHECK_INTERVAL_SECONDS`), a socket timeout (default 30s,
`FALKORDB_SOCKET_TIMEOUT_SECONDS`), and `socket_keepalive=True`, then
hands that client to `FalkorDriver` via its `falkor_db=` parameter (which
uses a caller-provided client as-is instead of building its own) — rather
than letting `FalkorDriver` build one with no health-checking at all.

## Consequences

- **Not fully confirmed to prevent recurrence.** The original degradation
  took an extended period of real, sustained usage to manifest — not
  practically reproducible on demand within this session to prove the fix
  holds under the same conditions long-term. What *is* confirmed: the
  fix doesn't break anything (full regression pass: config valid, all 16
  containers healthy, search/ingest/chat completions all still correct),
  and it directly targets the one connection-pool setting that most
  plausibly explains the observed behavior (silent staleness with no
  detection). If the same symptom recurs despite this, the next
  candidates are `max_connections` (currently unbounded) or a genuine
  upstream bug in `graphiti-core`/`falkordb-py` worth reporting rather
  than working around locally.
- **Cheap, safe fallback exists regardless**: restarting the `graphiti`
  container is fast (health check passes in well under a minute) and
  loses no data (FalkorDB is a separate service on its own volume) — a
  reasonable manual or scheduled mitigation if this is ever suspected
  again, while this fix's actual effectiveness is still being observed
  over time.
- `docker-compose.unified.yml` needs no changes for this — the new env
  vars are optional overrides with working defaults baked into
  `graphiti/app.py` itself.
