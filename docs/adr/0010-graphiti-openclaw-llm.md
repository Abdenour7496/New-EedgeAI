# ADR 0010: Graphiti entity/edge extraction routed through openclaw, not local Ollama

**Status:** Accepted
**Date:** 2026-08-27
**Decision owners:** EedgeAI maintainers

## Context

Auditing the stack against its core objective — chat discussions and files
uploaded within a chat session should be stored and retrievable, not just
documents ingested through the Knowledge API — surfaced that
`gcor_chat_session_ingest.py` (the Function that archives the conversation
itself, see README's "Chat and file capture") had never actually archived a
single real conversation since deployment, despite being written, documented,
and apparently tested.

Three separate problems, found by reproducing each step directly rather than
guessing:

1. **The Function was never installed.** Only `gcor_file_ingest` and
   `gcor_collection_scope` were registered in OpenWebUI's `function` table;
   `gcor_chat_session_ingest` was missing entirely. The `chat_sessions`
   Graphiti group held only 7 synthetic entries from 2026-08-21/22
   (`smoke-test-session`, `real-session-uuid-0001`, …) — manual API test
   calls made during development, not real captured conversations. Fixed by
   installing it into the `function` table the same way its siblings already
   were (matching row structure/valves), then recreating `openwebui`.

2. **`_flatten_messages` read the wrong field even once installed.** It read
   `chat.messages` (a flat array) expecting it to hold the full conversation.
   Reproduced directly against a real stored chat: after a genuine 6-message
   exchange, `chat.messages` held only 1 item, while the authoritative full
   history lived in `chat.history.messages` (an id-keyed dict) — untouched by
   the flattening logic. Every real conversation therefore looked like it had
   fewer than `min_messages` (2) and silently never archived. Fixed by
   preferring `chat.history.messages`, falling back to the flat array only if
   history is absent.

3. **Even fixed, the Function's own POST timeout (60s) was far shorter than
   the proxy's own patience for Graphiti** (`GRAPHITI_INGEST_TIMEOUT_SECONDS`,
   default 1800s) — the Function gave up on the proxy long before the proxy
   would give up on Graphiti. Fixed by reading the same env var for its own
   timeout, so the two can't drift out of sync.

With all three fixed, archiving the same real 6-message chat still failed —
the proxy waited the full 1800s and returned `502 {"detail":"Graphiti: "}`.
Graphiti's own logs showed real work happening the whole time (extraction +
edge-deduplication calls against local `qwen2.5:7b`), not a hang — it was
just genuinely too slow to finish a real transcript inside 30 minutes. This
matters beyond one slow call: `gcor_chat_session_ingest` fires after *every*
assistant turn, so a real back-and-forth conversation would queue a new
~30+ minute extraction job after every reply, faster than any of them could
complete.

Tested the fix directly with `graphiti-core`'s own `OpenAIGenericClient`
against `http://openclaw:18799/v1` (the same OpenAI-compatible gateway the
rest of this stack already uses for interactive chat) before committing to
it: a structured entity-extraction call that would run on `qwen2.5:7b`
locally completed in **8.8 seconds** via openclaw.

## Decision

- `graphiti/app.py`'s `create_graphiti()` now takes separate config for the
  LLM client (`GRAPHITI_LLM_BASE_URL` / `GRAPHITI_LLM_API_KEY` /
  `GRAPHITI_LLM_MODEL`) versus the embedder, which keeps using
  `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `EMBEDDING_MODEL_NAME` as before.
  Embeddings are **not** moved — they're fast locally already, and
  openclaw's gateway is a chat-completions endpoint, not an embeddings
  backend.
- `GRAPHITI_LLM_BASE_URL` points at **the proxy's own
  `/v1/openclaw/chat/completions`** (`http://proxy:5001/v1/openclaw`), not
  at openclaw directly — see the "fallback resilience" addendum below for
  why this changed from the initially-shipped version of this ADR.
  Authenticated with `GCOR_API_KEY` (the proxy's own shared secret), not
  `OPENCLAW_GATEWAY_TOKEN`.
- `graphiti` depends on `falkordb`/`ollama`/`openclaw` being healthy at
  startup (added `openclaw` here, wasn't previously required). It does
  **not** `depends_on: proxy` — `proxy` already `depends_on: graphiti`, and
  a mutual dependency isn't expressible in compose (nor needed): actual
  extraction calls only happen through requests the already-running proxy
  initiates, so proxy is guaranteed live by the time graphiti ever needs to
  call back into it.
- This only touches the `graphiti` service (this repo's own FastAPI
  wrapper) and `proxy` (one new route). `graphiti-mcp` (the separate vendor
  image `zepai/knowledge-graph-mcp:standalone`, used for openclaw's own
  internal memory-search MCP tool) is untouched — routing openclaw's own
  memory tool back through openclaw itself would be circular, and it's not
  our code to patch regardless.
- The two `gcor_chat_session_ingest.py` bugs above are fixed in the repo
  Function source and were pushed into the already-running installation the
  same way ADR 0008's DB patch was: directly updating the row in OpenWebUI's
  `function` table, then recreating `openwebui`.

## Addendum (same day): fallback resilience via the proxy, not direct-to-openclaw

The version of this ADR first shipped pointed `GRAPHITI_LLM_BASE_URL`
straight at `http://openclaw:18799/v1`. That worked (verified: 8.8s per
extraction call) but had a real gap: the main chat pipeline's
`call_openclaw()` (`proxy/main.py`) falls back to Ollama when openclaw's
provider chain is exhausted or unreachable (5xx / timeout / network error);
a direct connection from `graphiti-core`'s LLM client has no such fallback —
an openclaw outage would simply fail every extraction call.

Rather than reimplementing that fallback logic against `graphiti-core`'s
client interface (real risk: `generate_response` mutates its `messages` list
in place before calling the backend — retrying with a second client without
copying the list first would double-append schema/language instructions to
the prompt), added a **new proxy route**,
`POST /v1/openclaw/chat/completions`, that is a raw passthrough to the
existing, already-relied-upon `call_openclaw()` — same fallback behavior,
zero duplicated logic. Deliberately **not** the existing
`/v1/chat/completions` route: that one runs the full GCOR RAG pipeline
(Graphiti search + prompt augmentation for end-user chat turns), which would
corrupt a backend caller's own prompt (graphiti-core's extraction schema
instructions) with irrelevant injected context — wrong tool for this job.

Verified before and after switching:
- New route in isolation: `200 OK`, `"FALLBACK_ENDPOINT_OK"`.
- Existing `/v1/chat/completions` still works unchanged (regression check).
- `graphiti-core`'s own client against the new route: **7.3s** (comparable
  to the 8.8s direct-to-openclaw baseline — negligible added latency).
- Full `/api/ingest/session` pipeline end-to-end through the new routing:
  `200 OK` in 38.7s for a 2-message test session; cleaned up afterward.
- The original failing real chat (`01e2882e-…`, 6 messages) had already been
  confirmed archiving successfully (~5 min) against the direct-to-openclaw
  version before this addendum's routing change — not re-run a second time
  against the proxy-routed version, since the isolated client test above
  already demonstrates equivalent-or-better latency through the new path.

## Consequences

- Graphiti extraction/dedup calls now depend on the `proxy` service (for
  routing) and `openclaw` being healthy/authenticated. On openclaw failure,
  they now fall back to Ollama automatically, same as the main chat
  pipeline — this closes the gap the initial version of this ADR flagged.
- `SEMAPHORE_LIMIT` raised from 2 to 4 (was sized for local Ollama calls
  taking minutes each). Kept conservative rather than raised further —
  openclaw's own concurrency headroom under real concurrent ingest load
  hasn't been load-tested.
- Opt back out entirely by setting `GRAPHITI_LLM_BASE_URL` back to Ollama's
  URL (or the same value as `GRAPHITI_API_BASE_URL`) in `.env` — the code
  defaults to the old Ollama-only behavior if these env vars are never set.
- The *README's documented rationale for keeping Graphiti on Ollama*
  ("background OpenAI-compatible API workloads, separate from the
  interactive Codex and Claude Code runtimes") is superseded by this ADR
  for the LLM half — updated alongside this fix.
