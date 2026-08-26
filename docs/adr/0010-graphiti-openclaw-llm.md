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
- `docker-compose.unified.yml`'s `graphiti` service now defaults
  `GRAPHITI_LLM_BASE_URL` to `http://openclaw:18799/v1` and
  `GRAPHITI_LLM_MODEL` to `openclaw`, authenticated with the existing
  `OPENCLAW_GATEWAY_TOKEN`. `graphiti` now `depends_on: openclaw` (was only
  `falkordb`/`ollama`) to avoid a cold-start race.
- This only touches the `graphiti` service (this repo's own FastAPI
  wrapper). `graphiti-mcp` (the separate vendor image
  `zepai/knowledge-graph-mcp:standalone`, used for openclaw's own internal
  memory-search MCP tool) is untouched — routing openclaw's own memory tool
  back through openclaw itself would be circular, and it's not our code to
  patch regardless.
- The two `gcor_chat_session_ingest.py` bugs above are fixed in the repo
  Function source and were pushed into the already-running installation the
  same way ADR 0008's DB patch was: directly updating the row in OpenWebUI's
  `function` table, then recreating `openwebui`.

## Consequences

- Graphiti extraction/dedup calls now depend on `openclaw` being healthy and
  its Codex/Claude Code CLI subscriptions being authenticated — previously
  they only needed Ollama. If openclaw's provider chain is exhausted, note
  it does **not** fall back to Ollama the way the main chat pipeline's
  `call_openclaw()` does (`proxy/main.py`) — a Graphiti extraction call would
  simply fail. Revisit if this turns out to matter in practice; the *README's
  documented rationale for keeping Graphiti on Ollama* ("background
  OpenAI-compatible API workloads, separate from the interactive Codex and
  Claude Code runtimes") is superseded by this ADR for the LLM half only —
  the README needs a corresponding update.
- `SEMAPHORE_LIMIT` (default 2, caps concurrent Graphiti LLM calls) was not
  changed. Worth revisiting now that each call is ~8s instead of minutes —
  a higher limit may no longer risk overloading the backend the way it would
  have against local Ollama.
- Opt back out by setting `GRAPHITI_LLM_BASE_URL` back to Ollama's URL (or
  the same value as `GRAPHITI_API_BASE_URL`) in `.env` — the code defaults
  to the old Ollama-only behavior if these new env vars are never set.
- Not yet re-verified: the original failing chat session (`01e2882e-…`)
  successfully archiving end-to-end with openclaw wired in. The isolated
  `graphiti-core` client call against openclaw was verified (8.8s, valid
  structured JSON); the full `graphiti` service recreate + live retest is
  the next step.
