# ADR 0012: openclaw's shared lane concurrency, and ingest idempotency

**Status:** Accepted
**Date:** 2026-08-27
**Decision owners:** EedgeAI maintainers

## Context

Asked to verify ingestion and retrieval were working. Ran a full live cycle
against the real stack (not synthetic/isolated) while real concurrent usage
was happening, and found two real issues.

### 1. Graphiti's `SEMAPHORE_LIMIT` could starve interactive chat

A one-chunk plain-text ingest took **198.5 seconds** — previously ~8-10s
uncontended (per ADR 0010's measurements). `openclaw`'s own logs showed
real load: `lane wait exceeded: lane=main waitedMs=9658 queueAhead=2
activeAhead=4 activeNow=3 queueBehind=1`, alongside genuine concurrent
document processing (the user's own real usage, not test traffic).

Traced `activeAhead=4` to its source directly in openclaw's bundled code
(`dist/agent-limits-DGV0ALs8.js`):

```js
function resolveAgentMaxConcurrent(cfg) {
  const raw = cfg?.agents?.defaults?.maxConcurrent;
  if (typeof raw === "number" && Number.isFinite(raw)) return Math.max(1, Math.floor(raw));
  return 4;
}
```

Our `openclaw.json` never sets `agents.defaults.maxConcurrent`, so openclaw
is running its default of **4** — and every HTTP call to
`openclaw:18799/v1/chat/completions` (`command-queue-*.js`) lands on the
same `"main"` command lane regardless of caller: interactive user chat
turns, DocInt's vision/classification calls, *and*, since ADR 0010, Graphiti's
entity/edge extraction. ADR 0010 raised `GRAPHITI_SEMAPHORE_LIMIT` from 2 to
4 based on per-call speed alone, without knowing the shared lane's total
capacity was also 4 — meaning a busy ingest could claim the *entire* lane,
leaving zero concurrency for live chat or DocInt until it freed up.

### 2. A client-side timeout could leave duplicate Graphiti episodes

While verifying, a test ingest that appeared to time out client-side (at
120s) had actually *succeeded* server-side — a retry with a longer timeout
created a second, fully duplicate episode for the same logical content
(`"verify-ingest — chunk 1"` appeared twice in Graphiti). Root cause: both
`doc_id` computations (`proxy/main.py`, `/api/ingest` and
`/api/ingest/session`) are time-based
(`hashlib.md5(f"{filename}-{datetime.now().isoformat()}"...)`), not
content-based — so a retry of byte-identical content always gets a fresh
`doc_id` and a fresh episode. Given ADR 0010's own finding that Graphiti
extraction can take minutes even via openclaw under load (see above), any
caller whose own timeout is shorter than the server's has no way to know
whether its request actually succeeded — and a naive retry (or a caller
using the API directly, not through the two `gcor_*` Functions whose
timeouts were already fixed to track `GRAPHITI_INGEST_TIMEOUT_SECONDS` in
the previous PR) will duplicate it.

## Decision

**Lane concurrency**: `GRAPHITI_SEMAPHORE_LIMIT` default lowered from 4 back
to **2** — below the shared lane's known cap of 4, not at it, so background
ingestion can never fully starve interactive chat or DocInt. Openclaw's own
`agents.defaults.maxConcurrent` was deliberately *not* raised — that's the
underlying provider's real capacity (Codex/Claude Code CLI subscriptions),
and raising a local queue limit doesn't help if the actual subscription
throttles at a lower ceiling; that risk wasn't evaluated, so left alone.

**Ingest idempotency**: added a short-lived (`INGEST_IDEMPOTENCY_TTL_SECONDS`,
default 600s) in-memory cache in `proxy/main.py`, keyed by a hash of the
*actual submitted content* (filename/session_id + collection + agent_id +
access_level + the file bytes or rendered transcript text — deliberately
not the time-based `doc_id`). Checked at the top of `_ingest_bytes` and
`api_ingest_session`, before any extraction/Graphiti work; the result is
cached on success and returned as-is for a duplicate submission within the
window, without doing the (expensive, slow-under-load) work again. Scoped
to protect against "client gave up and is retrying" specifically, not
general dedup — re-ingesting genuinely new content, or the same content a
day later, still creates a fresh episode as expected. This also incidentally
prevents a duplicate S3 snapshot write for `/api/ingest/session` retries,
not just a duplicate Graphiti episode.

## Consequences

- Ingestion under real concurrent load is still slower than the
  uncontended baseline — this doesn't eliminate queueing, it just stops
  Graphiti's own background work from being the sole cause of chat
  unresponsiveness. If queueing remains a recurring problem, the next
  lever is raising `agents.defaults.maxConcurrent` in `openclaw.json`
  itself (total shared capacity), not `GRAPHITI_SEMAPHORE_LIMIT` again —
  that only reslices the same fixed pie.
- The idempotency cache is in-memory and per-process — it does not survive
  a `proxy` container restart, and does not coordinate across multiple
  proxy replicas if this stack is ever scaled beyond one. Acceptable for
  the specific risk being mitigated (a retry within seconds-to-minutes of
  the original attempt); not a substitute for a durable dedup mechanism if
  that's ever needed.
- A legitimate re-ingest of unchanged content within the 600s window (e.g.
  an operator manually re-running the same curl command twice to confirm
  it worked) will silently return the cached first result rather than
  creating a second episode — this is the intended behavior, but worth
  knowing if two "successful" ingests in quick succession look identical
  in the response, that's why.
