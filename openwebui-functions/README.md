# OpenWebUI GCOR Functions

## gcor_file_ingest.py

Pushes files attached to a chat message into the GCOR knowledge pipeline —
the same `/api/ingest` endpoint the Knowledge UI uses. OpenWebUI's own local
RAG over the attachment still runs as normal; this happens alongside it, so
the document additionally lands in Neo4j + Qdrant and gets a durable copy in
MinIO, making it retrievable from the Knowledge UI, OpenClaw, and Buzz — not
just the one OpenWebUI chat it was uploaded in.

### Install

1. In OpenWebUI, open **Admin Panel > Functions** and create a new Function.
2. Paste the contents of `gcor_file_ingest.py`.
3. Enable it and make it **global** (unlike the collection-scope filter, this
   one should apply to every chat, since it's just a background side effect).
4. Optional valve settings: `collection` (target Qdrant collection, blank =
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
that field to search only the selected backend Qdrant collection, then runs its
normal Neo4j graph expansion and LLM routing.

This function does not upload documents, create embeddings, invoke OpenWebUI
Knowledge, or write data to OpenWebUI storage.

## Install

1. In OpenWebUI, open **Admin Panel > Functions** and create a new Function.
2. Paste the contents of `gcor_collection_scope.py`.
3. Keep it enabled but do not make it global.
4. Open the Function's valve settings and select an active backend collection
   from the `collection` dropdown. The list is loaded from the GCOR proxy.

Create one configured Function per backend collection scope. For example:

| Function | Collection |
| --- | --- |
| Finance collection scope | `bills_and_expenses` |
| Archive collection scope | `doc_archive` |
| Documents collection scope | `documents` |

Attach only the relevant configured Function to each OpenWebUI workspace or
model. A chat with no attached collection scope preserves the proxy's existing
default retrieval behavior.

The document archive is available as `Archives (archived documents)`. Other
internal and archived collections are omitted from the dropdown. If the GCOR
proxy is temporarily unavailable while loading the valve settings, the dropdown
falls back to `documents`.

## Security

This is routing metadata, not authorization. The current proxy accepts the
collection selector from a trusted OpenWebUI request. Do not expose the proxy
directly to untrusted clients if collection isolation is required. Enforce
workspace-to-collection authorization at the proxy when an identity provider
or access-control store, such as Supabase, is added.