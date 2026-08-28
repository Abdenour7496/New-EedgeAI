# ADR 0018: Search-staleness — further theories ruled out, passive instrumentation added instead of another guess

**Status:** Accepted (still a mitigation, not a fix — root cause remains open)
**Date:** 2026-08-28
**Decision owners:** EedgeAI maintainers

## Context

ADR 0017 left the search-staleness bug mitigated (scheduled self-restart)
but unexplained. Asked to keep focusing on it specifically rather than
move on. This round tested a fresh batch of theories and, having run out
of ones that could be confirmed or refuted by reasoning alone, added
passive instrumentation instead of another synthetic-reproduction attempt.

### What was ruled out this round, with direct evidence

- **`uvloop`**: not installed (`ModuleNotFoundError`). Not the cause.
- **Module/process-level caching in `falkordb` or `redis-py`**: grepped
  both packages for `lru_cache`/`WeakValueDictionary`/`_registry`/`_cache`
  patterns. The only real hit was `redis/asyncio/connection.py`'s
  `himport_registry` — unrelated hash-field-expiration plumbing, not a
  connection or result cache. graphiti-core's only cache
  (`dedup_helpers._cached_shingles`) is a pure per-string function used
  during ingestion's entity-name dedup, unrelated to search.
- **Connection pooling at the FalkorDB-wrapper level**: read
  `falkordb/asyncio/falkordb.py` directly — every `FalkorDB(...)` call
  constructs a genuinely new `redis.Redis()` with its own
  `ConnectionPool`; no class- or module-level sharing. Confirms ADR 0017's
  "fresh instance per call" attempt really did get a fresh connection, and
  still failed — ruling out simple connection reuse/poisoning as the
  explanation for that.
- **FalkorDB server-side write visibility**: tested directly with
  `redis-cli` (bypassing all Python) — write a node, immediately read it
  back in a brand-new `redis-cli` process. Instant, consistent, every
  time. The server itself is not stale.
- **Vector index lag**: `CALL db.indexes()` shows no vector index exists
  on `RELATES_TO.fact_embedding` at all — only RANGE/FULLTEXT. Cosine
  distance is computed inline via `vec.cosineDistance(...)` in the search
  Cypher query itself (confirmed via `redis-cli MONITOR` capturing the
  actual query graphiti-core sends). No async index build to lag behind.
- **The `falkordb` client's compact-protocol schema cache**
  (`GraphSchema`, used to decode label/property/relationship IDs): starts
  empty on every fresh `AsyncGraph`, which `FalkorDriver._get_graph()`
  constructs fresh on every single query call already — and
  `get_label`/`get_relation`/`get_property` self-heal via a full refresh
  on `IndexError`. Not a plausible staleness source.
- **Cosine-threshold miss** (a new theory raised this round — maybe the
  original failure was a scoring/threshold issue, not a visibility one):
  computed the actual embedding similarity for the original failing
  test's exact query/fact pair against the live `nomic-embed-text` model —
  **~0.89**, far above the 0.6 cutoff baked into the search query's
  `WHERE` clause. Ruled out — confirmed a real visibility gap, not a
  ranking miss.

### The thread-affinity experiment (inconclusive)

FalkorDB reports `THREAD_COUNT=8` — an internal query-worker pool
independent of Redis's normal single-threaded command loop. Hypothesis:
if connections get pinned to specific worker threads by some
process-relative rule (source port, PID), and one thread's internal view
lags, that would explain why even a "fresh instance" or "isolated event
loop" (ADR 0017) didn't help — those only change the Python-level object,
not necessarily which FalkorDB thread serves it — while a genuinely
separate OS process might land differently.

Tested by restarting `falkordb` with `FALKORDB_ARGS: THREAD_COUNT 1`
(`GRAPH.CONFIG SET` refused it at runtime — needs a restart; AOF
persistence meant no data loss) and running concurrent ingest+search load
(3, then 4 simultaneous ingests, verified via each result's actual `text`
field, not a substring grep against the raw response — the API echoes
the query string back regardless of whether real hits exist, which
produced a false positive on the first attempt). All 7 concurrent writes
under `THREAD_COUNT=1` were found immediately. Reverted to `THREAD_COUNT=8`
and ran the identical test as a control — 4/4 also found immediately.

**Result: inconclusive.** Combined with ~8 sequential ingest→search
cycles earlier in the session, that's 19 clean attempts and zero
reproductions under any configuration tried. The experiment can't confirm
or refute the theory because neither condition failed — there's no
differential signal to observe. `THREAD_COUNT` was reverted to the
default (8); this repo makes no claim either way about worker-thread
affinity.

## Decision

Stop trying to force a synthetic reproduction — the bug is evidently rare
enough that neither sequential nor concurrent synthetic load reliably
triggers it, so further guess-and-check theories have a poor cost/benefit
ratio. Instead, `graphiti/app.py`'s `/search` now runs a lightweight,
query-independent diagnostic on every call
(`_diagnose_search_staleness`):

For the requested group's most recently created episode, check via
`app.state.graphiti`'s own connection whether *any* `RELATES_TO` edge is
sourced from it (`ep.uuid IN e.episodes`). Ingestion extracts
synchronously, so by the time `/api/ingest` returns 200 those edges
already exist — an episode older than 30s (generous buffer for legitimate
in-flight extraction) with visibly zero edges from it, checked on the
connection that's actually serving search traffic, is exactly this bug's
shape. Logs at `WARNING` with the episode's uuid/name/age and the
process's uptime since last restart, so the next real occurrence in
normal usage gets captured with full context instead of needing to be
forced.

An earlier version of this check only fired when `/search` returned zero
facts overall — wrong signal, caught before shipping: the one real
observed failure (ADR 0017) returned a *non-empty* result set that simply
omitted the one new fact, never zero, because the underlying hybrid
search returns loosely-related fallback results even for a nonsense query
(verified directly: a deliberately nonsensical query against a populated
group still returned 8 results). A per-query relevance check also can't
generically distinguish "irrelevant to this query" from "this connection
can't see it." Checking the latest episode's own edges directly sidesteps
both problems.

## Consequences

- No performance concern: one small aggregate Cypher query per search
  call, using the same connection already in use.
- Never raises — wrapped in try/except, logs at `DEBUG` on its own
  failure rather than affecting the real search response.
- This is diagnostic-only — it does not fix or further bound the bug
  beyond ADR 0017's scheduled restart, which stays in place unchanged.

### Update, same day: it fired almost immediately, and revealed the bug is not one thing

Re-ran the live test suite twice after shipping this. Both times,
`test_ingest_then_search_finds_it` reproduced the original failure again
— confirms this is a real, still-live, not-particularly-rare-under-load
bug, not something the ADR 0017 mitigation alone was masking.

**First rerun**: `_diagnose_search_staleness` did *not* fire for the
failing test's own query at all. Root check (#1, DB-level) found the
episode's edges existed; the (at-the-time ungated) search-level check
(#2) fired instead for a *completely unrelated* concurrent query —
`"Vorthex Industries net loss"` — against the group's actual most-recent
episode (`Electricity Bill.pdf — chunk 2`, 16 edges, 0 of them in that
search's results). Investigated: this was a **false positive in the
diagnostic itself**, caught the same session it shipped. Check #2 as
originally written compares an arbitrary query against whichever episode
happens to be most-recently-created in the group, with no relevance
check at all — in any populated multi-document collection, an
unconnected later query almost never should surface the single newest
episode's facts, so the check was guaranteed to fire constantly and mean
nothing. Fixed by gating check #2 to only run when the latest episode is
under ~2 minutes old (scoped to the actual scenario that matters — a
search shortly after its own target content was ingested, where
relevance is likely rather than merely possible). Verified the fix: a
search against an old "latest episode" no longer logs anything.

**Second rerun** (with the recency-gated build): failed again, but
structurally differently from the first documented failure (ADR 0017)
and differently from what either diagnostic check is built to catch. The
top search result was the test's *own* episode — but a vague, generic
edge (`"[doc] reports the quarterly revenue figure"`, type `SOURCE_OF`)
rather than the specific fact the test needs
(`"...Northwind Traders...7.8 million..."`). That specific fact-rich edge
was never present in the results at all, and — critically — neither
diagnostic check fired, because the episode's one actual edge *was*
findable both directly and via `search()`; there was simply no missing
edge to detect. The test's own `tearDown` deleted the episode
(auto-cleanup, by design) before this could be investigated further, so
this is a **lead, not a confirmed finding**: it looks like the LLM
extraction step (openclaw) sometimes produces a generic relationship
instead of the specific one a document actually supports — a different
bug category entirely (extraction quality, not retrieval/staleness) that
would explain "missing fact" without any connection or ranking bug at
all. Neither this instrumentation nor ADR 0017's investigation was built
to catch that; it was found here only by chance, on the specific words in
the failure message.

## Consequences (updated)

- The search-staleness investigation (ADR 0017 + this ADR) may have been
  chasing at least two, possibly three, distinct bugs under one name:
  (a) genuine connection/search staleness (ADR 0017's original,
  well-evidenced case), (b) a possible extraction-quality issue producing
  a generic edge instead of a specific one (this ADR's second rerun,
  unconfirmed — evidence was lost to test auto-cleanup), and (c) this
  ADR's own diagnostic false-positiving on unrelated queries (found and
  fixed within the same session).
- Next concrete step, if picked back up: stop letting the test's
  `tearDown` destroy evidence on failure — either skip cleanup when a
  test fails, or add an ingest-time log of exactly what edges got
  extracted per episode, so a future occurrence of the "generic edge
  instead of specific one" pattern can be confirmed or ruled out without
  needing to race a 5-second cleanup window.
- Check #1 (DB-level, query-independent) had zero false positives across
  both live reproductions and all synthetic testing this session — it
  remains the higher-confidence signal of the two. Check #2 (search-level
  omission) is real but noisier even after the recency gate; treat any
  hit as a lead to investigate manually, not a confirmed bug on its own.
