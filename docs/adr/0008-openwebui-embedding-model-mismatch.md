# ADR 0008: OpenWebUI's native file-attach RAG silently failed on embedding model mismatch

**Status:** Accepted
**Date:** 2026-08-24
**Decision owners:** EedgeAI maintainers

## Context

A user attached a PDF directly to an OpenWebUI chat message and asked for a
summary. The assistant replied "I don't see anything attached yet" — as if
no file had been sent at all — even though the proxy, Graphiti, and every
container health check reported fine.

This is **not** the `gcor_file_ingest`/`gcor_chat_session_ingest` capture
pipeline (README's "Chat and file capture" section, ADR 0002) — that's an
opt-in custom Function archiving files/sessions into MinIO + Graphiti.
This was OpenWebUI's own **built-in** "attach a file to a chat message"
RAG feature (Documents/Knowledge), which embeds the file client-side of
the chat request using `rag.embedding_engine`/`rag.embedding_model` —
entirely separate config, separate code path.

Root cause, found by reproducing the exact request instead of guessing:

1. `docker-compose.unified.yml` set `RAG_EMBEDDING_ENGINE: openai` (so
   OpenWebUI POSTs to the proxy's OpenAI-compatible `/v1/embeddings`) but
   never set `RAG_EMBEDDING_MODEL`. OpenWebUI's stock default for that
   field is `sentence-transformers/all-MiniLM-L6-v2` — a local
   `sentence-transformers` model id, irrelevant once the engine is
   `openai`, but nothing overrides it.
2. Every embedding request OpenWebUI made therefore carried
   `"model": "sentence-transformers/all-MiniLM-L6-v2"`. The proxy's
   `/v1/embeddings` (`proxy/main.py`) forwards that model name verbatim to
   Ollama (`EMBEDDING_BACKEND=ollama` by default) — but Ollama only has
   `nomic-embed-text` pulled (`OLLAMA_EMBEDDING_MODEL`, via `ollama-init`).
   Ollama returns 404 for the unknown model name; the proxy passes that
   404 straight back (`proxy/main.py`'s `embeddings()` re-raises with
   `exc.response.status_code`).
3. Confirmed directly against the live stack:
   `curl .../v1/embeddings -d '{"model":"sentence-transformers/all-MiniLM-L6-v2",...}'`
   → `404 {"detail":"Embeddings upstream error"}`;
   the same call with `"model":"nomic-embed-text"` → `200` with a real
   vector.
4. OpenWebUI persists this setting in its own sqlite `config` table at
   first boot and — same gotcha as ADR 0002's follow-on fix — never
   re-syncs it from the container's env var afterward. Adding
   `RAG_EMBEDDING_MODEL` to the compose file alone would not have fixed an
   already-running deployment.

## Decision

- Added `RAG_EMBEDDING_MODEL: ${OLLAMA_EMBEDDING_MODEL:-nomic-embed-text}`
  to the `openwebui` service in `docker-compose.unified.yml`, so a fresh
  deployment seeds the correct value from the start.
- For the already-running deployment, updated the persisted
  `rag.embedding_model` row directly in OpenWebUI's sqlite `config` table
  to `nomic-embed-text`, then recreated the `openwebui` container so it
  reloads that value into its in-memory config on boot (the same effect as
  saving it through Admin Panel > Settings > Documents, which was the
  documented workaround in ADR 0002's follow-on fix — verified the value
  survived the restart, and that `/v1/embeddings` calls against the proxy
  succeed with it).

## Consequences

- Any deployment that changes `OLLAMA_EMBEDDING_MODEL` away from
  `nomic-embed-text` (or switches `EMBEDDING_BACKEND`/`GRAPHITI_EMBEDDING_MODEL`
  to something else) needs the same two-part fix: update the env var *and*
  the already-persisted `rag.embedding_model` in OpenWebUI's DB (or via
  Admin Panel), because the container won't do the second half on its own.
- This was silent by design from OpenWebUI's side: a failed per-file
  embedding doesn't surface as a visible chat error, it just means the
  model gets no retrieved content for that attachment — worth remembering
  as a specific instance of the general
  [[feedback_openwebui_temporary_chat_trap]] class of failure (OpenWebUI
  swallowing failures that look like "nothing was sent" rather than
  reporting them).
- `gcor_file_ingest`'s pipeline (MinIO + Graphiti, gated by `GCOR_API_KEY`)
  was never affected by this — it doesn't go through OpenWebUI's `rag.*`
  config at all. A file "not being seen" by the assistant in a normal chat
  doesn't imply that capture pipeline is broken too; check them
  independently.
