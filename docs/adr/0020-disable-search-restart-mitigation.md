# ADR 0020: Disable Graphiti's scheduled self-restart — its premise was already invalidated, and it was causing real harm

**Status:** Accepted
**Date:** 2026-08-28
**Decision owners:** EedgeAI maintainers
**Supersedes:** the operational effect of docs/adr/0017 (the ADR itself is
left as an honest historical record; see docs/adr/0019 for how its
diagnosis was corrected)

## Context

Investigating a real user report: a document attached in OpenWebUI
appeared to upload successfully (file safely stored in MinIO) but its
Graphiti episode never showed up, and the assistant couldn't reference
it. Direct evidence, reproduced twice:

- The `documents` group's raw graph had **no episode at all** for the
  file — confirmed via a direct FalkorDB query, bypassing every app-level
  cache or connection.
- The proxy had logged the *intent* to ingest (`Ingest 'Water bill.pdf':
  3 chunks (ext=.pdf, docint=True, ...)`) but never logged completion —
  no success, no error.
- Re-ingesting the same file directly (bypassing OpenWebUI, a client I
  fully controlled) reproduced it again: **1 of 3 chunks landed**
  (`Water bill.pdf — chunk 1`, confirmed in the graph), then nothing
  further — my own client eventually hit its own timeout (28 minutes)
  without ever getting a response, success or failure.

### Root cause

`graphiti/app.py`'s `POST /messages` handler (which `graphiti_ingest()`
calls with *all* of a document's chunks in one request) processes them
**sequentially, in a single HTTP request** — each chunk's `add_episode()`
call commits to FalkorDB as it completes, before the next chunk starts.

Meanwhile, ADR 0017's scheduled self-restart (`SEARCH_RESTART_INTERVAL_
SECONDS`, default 900s) calls `os._exit(0)` unconditionally on a timer —
it does not check whether a request is in flight. A DocInt-enabled,
multi-chunk ingest, under real concurrent load from live chat traffic
sharing the same openclaw concurrency lane (`SEMAPHORE_LIMIT=2`), can
easily take well over 15 minutes. If the 900s restart fires while chunk 2
or 3 is mid-extraction, the whole graphiti-1 process dies immediately,
silently dropping that connection. The proxy's httpx client (a single
`timeout=GRAPHITI_INGEST_TIMEOUT_SECONDS`, default 1800s, covering
connect/read/write together) doesn't always detect a dead TCP connection
promptly — it can sit blocked on a read that will never return data until
its own long timeout elapses. Net effect: a caller waits a very long time
for nothing, chunk 1 survives (already committed before the restart),
chunks 2+ are silently lost, and no error ever surfaces.

### Why the fix is "turn the restart off" and not "make ingest more robust to it"

ADR 0017 added this restart to bound *search* staleness — a real,
carefully-investigated symptom at the time. **docs/adr/0019 subsequently
found the actual root cause of every reproducible instance of that
symptom: a proxy-side temporal-validity filter (`_filter_hits()`)
silently dropping any dated fact, completely unrelated to Graphiti,
FalkorDB, or connection staleness.** That filter is now removed. No
genuine case of Graphiti/FalkorDB connection staleness was ever actually
confirmed — ADR 0017's own investigation ruled out every layer it could
test directly (raw Cypher queries, BM25, embeddings, a fresh instance, an
isolated event loop) and never found a reproducing case; the restart was
a mitigation for a diagnosis that turned out to be wrong.

Given that, the restart is not preventing a known-real problem — it's
purely a liability at this point, and just demonstrated exactly the kind
of damage a scheduled `os._exit(0)` with no in-flight-request awareness
can do. The correct fix is to remove the mitigation whose justification
no longer holds, not to add more complexity (in-flight-request tracking,
draining logic, etc.) defending a restart schedule nothing currently
requires.

## Decision

Set `SEARCH_RESTART_INTERVAL_SECONDS=0` in `.env` (disables the restart —
`graphiti/app.py`'s `_periodic_self_restart()` already no-ops at `<= 0`,
no code change needed). Recreated `graphiti` and confirmed the env var
took effect.

Cleaned up the partial episode (`Water bill.pdf — chunk 1`, 13 orphaned
edges) via `DELETE /api/collections/documents/docs/{uuid}` before
re-ingesting the full document cleanly.

### Update, same session: a second, independent cause found

Disabling the restart fixed the *silent-partial-ingest-with-no-error*
shape of the bug, but a re-ingest attempt right after still only landed 2
of 3 chunks before my own client's 1700s timeout, with graphiti's own
uptime confirming it never restarted this time. openclaw's own logs
explained it directly:

```
[diagnostic] stalled session: ... age=19478s queueDepth=0
  reason=active_work_without_progress classification=stalled_agent_run
  activeWorkKind=model_call lastProgress=codex_app_server:notification:item/completed
  lastProgressAge=19476s recovery=checking
[diagnostic] stuck session recovery: ... action=abort_embedded_run aborted=true drained=true released=0
[diagnostic] lane task error: lane=main durationMs=19480708 error="AbortError: agent run aborted"
```

A Codex-backed session in openclaw's shared `main` lane had been stuck in
`model_call` state for **19,478 seconds (5.4 hours)** — no error, no
timeout, just silently making zero progress — occupying one of only a
few concurrency slots the entire time and degrading every extraction
call routed through that lane (Graphiti's own entity/edge extraction,
DocInt, and interactive chat all share it). openclaw only detected and
aborted it because an unrelated Telegram integration's `stale-socket`
restart happened to trigger a health-check sweep — not on any fixed
schedule of its own. This is an **openclaw-internal reliability gap**
(a stuck agent run with no bounded max-duration watchdog of its own),
not a bug in this repo's code, and out of scope to fix here.

Once that stuck session cleared, a clean re-ingest (after deleting the
2 partial chunks from the run that overlapped with the stuck session)
completed all 3 chunks in ~28 minutes under continued real concurrent
chat load (~9-10 min/chunk) with zero errors, and the document verified
fully searchable via `/api/search` afterward (correct account number,
customer number, usage, amount due, issuer — all present and
well-ranked).

**Practical implication:** if a document ingest (especially DocInt-
enabled, multi-chunk) seems to hang or partially fail with no clear
error, check `docker logs eedgeai-openclaw-1 | grep -i stalled` for a
stuck session in the shared lane before assuming it's a Graphiti/FalkorDB
or proxy issue — this session's actual root cause, twice, was upstream in
openclaw, not in this repo.

## Consequences

- If a genuine Graphiti/FalkorDB connection-staleness case is ever found
  in the future (none has been, per ADR 0019's corrected account), this
  is trivially reversible — set `SEARCH_RESTART_INTERVAL_SECONDS` back to
  a positive value in `.env` and recreate `graphiti`.
- Removes a real, demonstrated risk to ingestion: any sufficiently slow
  multi-chunk ingest (DocInt-enabled, or just several chunks under
  concurrent chat load) previously had a real chance of colliding with a
  15-minute restart boundary and silently losing chunks with no error
  surfaced. That risk is gone.
- `docs/adr/0018`'s passive diagnostic instrumentation
  (`_diagnose_search_staleness` in `graphiti/app.py`) stays in place
  unchanged — independent of the restart schedule, and still a reasonable
  low-cost check for a failure mode that, while never confirmed, hasn't
  been definitively ruled out for all possible future scenarios either.
- Long-lived Graphiti processes now accumulate uptime indefinitely again
  (no periodic recycling). If memory growth or some other slow-degradation
  concern shows up over time, that would need its own investigation and
  its own justification — not a blind reinstatement of this restart.
