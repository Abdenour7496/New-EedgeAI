# OpenWebUI GCOR Functions

## gcor_chat_session_ingest.py

Archives the chat session itself — not just its attachments — into the GCOR
knowledge pipeline. On every outlet (after each assistant turn), it fetches
the chat's current title and full message history back from OpenWebUI's own
`/api/v1/chats/{id}` API and forwards them to the GCOR proxy's
`/api/ingest/session`. That endpoint writes a JSON snapshot of the transcript
to MinIO under `chat-sessions/<session_id>/<date-time>_<name>.json` and
indexes it into Graphiti (group `chat_sessions` by default, kept separate
from the document group), so past conversations are retrievable the same way
uploaded documents are.

Naming: the folder is keyed by the chat's stable `session_id`, so a
conversation's snapshots stay together for its whole life. Each snapshot's
filename carries the date, time, and the best human-readable name available
at that moment — the real chat title once OpenWebUI has generated one,
otherwise a snippet of the first user message (OpenWebUI stamps every new
chat with a placeholder like "New Chat" before its own async title-generation
renames it a turn or two later; that placeholder is never used as the name).
For example: `chat-sessions/3f9a2b7c-.../2026-08-22T14-05-30Z_what-is-the-capital-of-france.json`.

### Install

1. In OpenWebUI, open **Admin Panel > Functions** and create a new Function.
2. Paste the contents of `gcor_chat_session_ingest.py`.
3. Enable it and make it **global** (like `gcor_file_ingest.py`, this is a
   background side effect that should apply to every chat).
4. Optional valve settings: `collection` (target Graphiti group, blank =
   proxy default `chat_sessions`), `access_level`, `agent_id`, `min_messages`
   (skip archiving a session until it has at least this many messages —
   default 2, i.e. at least one full user/assistant exchange).

### Verify it's working

Have a short conversation, then:

```bash
docker compose -f docker-compose.unified.yml logs -f openwebui | grep gcor_chat_session_ingest
# expect: "archived chat_id=... (N messages) -> <document_id>"
```

Check the snapshot landed in MinIO (bucket `documents` by default) under
`chat-sessions/<session_id>/`, and that the session is searchable:

```bash
curl "http://localhost:5001/api/search?collection=chat_sessions&q=your+question"
```

### Notes

- Ingestion is fire-and-forget and runs after the chat response is already
  underway — it never delays the user's turn. Failures are logged, not shown
  to the user.
- Each assistant turn writes a **new** timestamped snapshot of the full
  transcript — that's intentional: it gives a timestamped history of how the
  conversation grew, not just its final state. An in-memory hash per
  `chat_id` (reset on an OpenWebUI restart) skips re-archiving an unchanged
  transcript if outlet fires more than once for the same turn.
- Requires the OpenWebUI request to carry its own auth (Bearer token or
  session cookie) — used to fetch the chat back from OpenWebUI's own
  `/api/v1/chats/{id}` endpoint. No separate credential needed.

---

## gcor_file_ingest.py

Pushes files attached to a chat message into the GCOR knowledge pipeline —
the same `/api/ingest` endpoint the Knowledge UI uses. OpenWebUI's own local
RAG over the attachment still runs as normal; this happens alongside it, so
the document additionally lands in Graphiti (FalkorDB) and gets a durable
copy in MinIO, making it retrievable from the Knowledge UI and OpenClaw — not
just the one OpenWebUI chat it was uploaded in.

### Install

1. In OpenWebUI, open **Admin Panel > Functions** and create a new Function.
2. Paste the contents of `gcor_file_ingest.py`.
3. Enable it and make it **global** (unlike the collection-scope filter, this
   one should apply to every chat, since it's just a background side effect).
4. Optional valve settings: `collection` (target Graphiti group, blank =
   proxy default), `access_level`, `agent_id`, `enable_docint` (run full OCR
   /table/entity extraction — slower, off by default).

### Verify it's working

Attach a file to a chat message and send it, then:

```bash
docker compose -f docker-compose.unified.yml logs -f openwebui | grep gcor_file_ingest
# expect: "ingested <filename> (file_id=...) -> <document_id>"
```

Check the file landed in GCOR:
```bash
curl -s http://localhost:5001/api/collections | grep -A3 '"documents"'
```
or open the Knowledge UI — the `documents` collection's doc/chunk count
should have gone up, and the original file is in MinIO under
`documents/originals/<document_id>/`.

### Notes

- Ingestion is fire-and-forget and runs after the chat response is already
  underway — it never delays the user's turn. Failures are logged, not shown
  to the user.
- Per-file dedup is in-memory only (a Python `set` on the Filter instance) —
  it resets on an OpenWebUI restart, so a file re-attached across a restart
  can be ingested again as a new document. Harmless (just a duplicate
  document), not corrupting.
- Requires the OpenWebUI request to carry its own auth (Bearer token or
  session cookie) — used to fetch the raw file back from OpenWebUI's own
  `/api/v1/files/{id}/content` endpoint. No separate credential needed.

---

`gcor_collection_scope.py` is an OpenWebUI Filter. It adds a
`gcor_collection` field to each chat completion request. The GCOR proxy uses
that field to search only the selected Graphiti group, then runs its normal
hybrid retrieval and LLM routing.

This function does not upload documents, create embeddings, invoke OpenWebUI
Knowledge, or write data to OpenWebUI storage.

## Install

1. In OpenWebUI, open **Admin Panel > Functions** and create a new Function.
2. Paste the contents of `gcor_collection_scope.py`.
3. Keep it enabled but do not make it global.
4. Open the Function's valve settings and select an active backend collection
   from the `collection` dropdown. The list is loaded from the GCOR proxy.

Create one configured Function per Graphiti group scope. For example:

| Function | Collection |
| --- | --- |
| Finance collection scope | `bills_and_expenses` |
| Chat sessions scope | `chat_sessions` |
| Documents collection scope | `documents` |

Attach only the relevant configured Function to each OpenWebUI workspace or
model. A chat with no attached collection scope preserves the proxy's existing
default retrieval behavior.

Internal groups (names starting with `_`) are omitted from the dropdown. If
the GCOR proxy is temporarily unavailable while loading the valve settings,
the dropdown falls back to `documents`.

## Security

This is routing metadata, not authorization. The current proxy accepts the
collection selector from a trusted OpenWebUI request. Do not expose the proxy
directly to untrusted clients if collection isolation is required. Enforce
workspace-to-collection authorization at the proxy when an identity provider
or access-control store, such as Supabase, is added.