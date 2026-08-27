# ADR 0011: OpenWebUI's native file-attach RAG routed through an OCR-capable external loader

**Status:** Accepted
**Date:** 2026-08-27
**Decision owners:** EedgeAI maintainers

## Context

Reported: "there is an issue processing document in ingestion pipeline."
Investigated by reproducing directly rather than guessing.

First found something real but unrelated: `gcor_file_ingest` and
`gcor_chat_session_ingest` were both logging repeated `401 Unauthorized`
against OpenWebUI's own `/api/v1/files/{id}/content` and
`/api/v1/chats/{id}` — but the `chat_id` involved carried a `temporary:`
prefix, i.e. a genuine Temporary Chat (per
[[feedback_openwebui_temporary_chat_trap]]: those never persist to the DB,
so any background capture of them legitimately 401s/404s). Checked with the
user directly — this specific document was **not** attached in a temporary
chat, so that 401 was a red herring from an unrelated stale session, not the
reported issue.

The user's actual symptom, once clarified: "upload looked fine, but the
assistant couldn't reference it" — the exact signature of ADR 0008's bug
class, but that fix (the `RAG_EMBEDDING_MODEL` mismatch) was already
verified still correctly applied. Reproduced the real cause directly against
the actual file in question (a 5.3MB "Marriage Certificate.pdf"):

```
document_intel._is_scanned_pdf(data)          → True
document_intel._extract_pdf_text_pdfminer(data) → '' (0 chars)
```

A scanned PDF with no embedded text layer. ADR 0009 already fixed this for
the *background* `gcor_file_ingest → /api/ingest` pipeline (auto-retry
through OCR when plain extraction is empty) — but that's a **separate code
path** from OpenWebUI's own **built-in** file-attach RAG (see ADR 0008's
same distinction), which does its own independent extraction and was never
covered by that fix. Confirmed directly: OpenWebUI's
`rag.content_extraction_engine` was `""` (its default, non-OCR built-in
extraction) — so a scanned document attached directly to a chat would
*always* silently extract to empty content in that same conversation,
regardless of `gcor_file_ingest` correctly archiving it (with OCR) in the
background for later search — the two pipelines don't share results, and
the background one doesn't help the immediate "attach and ask" experience.

## Decision

OpenWebUI supports an `external` content-extraction engine
(`EXTERNAL_DOCUMENT_LOADER_URL` / `EXTERNAL_DOCUMENT_LOADER_API_KEY`): a
`PUT {url}/process` of the raw file bytes, expecting back
`{"page_content": ..., "metadata": ...}`
(`open_webui/retrieval/loaders/external_document.py`). Added a new proxy
route matching that exact contract, `PUT /api/external-loader/process`
(`proxy/main.py`), reusing the same plain-extraction-first,
OCR-only-if-needed logic ADR 0009 already proved out — **not** the full
DocInt pipeline (tables/forms/classification/entities via openclaw): this
call is synchronous and blocks the chat response OpenWebUI is building, so
it deliberately stays to fast local Tesseract OCR only.

Wired in two places, matching ADR 0008's pattern for an already-provisioned
deployment:
- `docker-compose.unified.yml`: `CONTENT_EXTRACTION_ENGINE=external`,
  `EXTERNAL_DOCUMENT_LOADER_URL=http://proxy:5001/api/external-loader`,
  `EXTERNAL_DOCUMENT_LOADER_API_KEY=${GCOR_API_KEY}` on the `openwebui`
  service, for fresh deployments.
- Directly patched the already-persisted `rag.content_extraction_engine`,
  `rag.external_document_loader_url`, and
  `rag.external_document_loader_api_key` rows in OpenWebUI's sqlite
  `config` table, then recreated `openwebui` so it reloads them.

Verified before wiring OpenWebUI to it: called the new endpoint directly
with the actual scanned "Marriage Certificate.pdf" — `200 OK` in 19.4s,
correct real OCR'd text (names, addresses, dates from the actual
document). Confirmed `requests.put()` in OpenWebUI's loader has no
client-side timeout that would cut off a slow OCR response, and Caddy's
config has no proxy timeout override either.

## Consequences

- OpenWebUI's file-attach RAG is now OCR-capable, closing the gap between
  it and `gcor_file_ingest`'s already-OCR-capable background pipeline.
  Both paths now handle scanned documents; they remain functionally
  separate (immediate in-chat context vs. background Graphiti archival),
  by design.
- Every file attached in a normal (non-temporary) chat now round-trips
  through the proxy for extraction, even non-scanned ones with a real text
  layer — `_extract_text()` runs first and is fast, so this adds
  negligible latency for the common case; only the empty-extraction/OCR
  fallback path costs real time (~20s observed for a 5-page-equivalent
  scanned PDF).
- Not yet confirmed: a live end-to-end test through the actual OpenWebUI UI
  (attach the scanned document in a real chat, ask about it). Verified so
  far: the endpoint directly, and that the settings persisted through an
  `openwebui` recreate. The next step is a real user test.
- The `401` on the `temporary:` chat_id from this investigation remains
  unaddressed and by design (Temporary Chat mode never persists — nothing
  to fix there); logged here only because it was found along the way and
  briefly suspected as related before being ruled out.
