# ADR 0009: Auto-fallback to OCR when plain text extraction is empty

**Status:** Accepted
**Date:** 2026-08-24
**Decision owners:** EedgeAI maintainers

## Context

A user attached a scanned PDF (a photographed/screenshotted page saved as
PDF, with no embedded text layer) to an OpenWebUI chat and asked for a
summary. The assistant reported "I don't see anything attached" — with
zero indication anywhere in the chat that the real problem was OCR, not a
missing attachment.

Root cause chain, confirmed end-to-end rather than assumed:

1. `gcor_file_ingest` (the OpenWebUI Function that captures chat
   attachments into Graphiti) always calls `/api/ingest` with
   `enable_docint=false` — there's no way for a chat user to opt into OCR,
   the Function's valve default just doesn't ask for it.
2. `/api/ingest`'s plain-text extraction path (`_extract_text`) can't read
   pixels. For this file it returned empty text, and `_ingest_bytes`
   immediately raised `422 {"detail":"No text extracted"}` — reproduced
   directly: `curl .../api/ingest -F enable_docint=false ...` → 422; the
   same file with `-F enable_docint=true` → `200 {"status":"ok", ...,
   "docint_is_scanned":"True"}`, via the existing Tesseract-based OCR path
   in `proxy/document_intel.py` (already built for exactly this — it was
   just never reached).
3. OpenWebUI's own native file-attach RAG hit the identical wall
   independently (`ValueError: The content provided is empty` in
   `open_webui.routers.retrieval`), and has no OCR fallback of its own in
   this stack at all — that path is unaffected by this ADR, see the
   consequences below.

## Decision

In `proxy/main.py`'s `_ingest_bytes`, when the plain-text path is taken
(`enable_docint` was not requested) and returns empty text **and** the
file is `.pdf` or `.docx`, automatically retry through the Document
Intelligence/OCR pipeline (`process_document`) before failing — the same
code path `enable_docint=true` already used, just no longer opt-in-only
for this specific case. If OCR also finds nothing, the original `422 "No
text extracted"` still applies.

Scoped narrowly to `.pdf`/`.docx`: `.txt`/`.md`/`.json`/`.csv` are
text-native, so empty output from those means an actually-empty file, not
something OCR could rescue — they still fail fast instead of paying for a
~1-2 minute Tesseract pass for nothing. Images and medical formats already
always go through OCR/vision handling regardless of `enable_docint`, so
they're unaffected by this change.

Verified against the real failing file, not just a synthetic case:
replaying the *exact* request `gcor_file_ingest` sends
(`enable_docint=false`, no other change) against the rebuilt proxy now
returns `200 {"status":"ok", ..., "docint_is_scanned":"True"}` instead of
`422`.

## Consequences

- Any scanned PDF/DOCX attached through `gcor_file_ingest` (or any other
  caller that doesn't explicitly request `enable_docint`) now gets OCR'd
  automatically instead of silently failing — no Function/valve change
  needed.
- **Latency**: a plain PDF/DOCX with a real text layer is completely
  unaffected (extraction still succeeds on the fast path, no OCR
  attempted). Only the specific case that used to hard-fail — empty
  plain-text extraction on a PDF/DOCX — now pays the OCR cost (~1-2
  minutes on CPU) instead of erroring immediately. That trade is the
  point: slow-but-working beats fast-but-silently-broken for this case.
- **Still not covered**: OpenWebUI's own native "attach file to chat"
  RAG (separate from `gcor_file_ingest`) has no OCR fallback wired to this
  stack's Tesseract pipeline — a scanned file attached that way will still
  fail the same way it did before this ADR. Extending that is future work;
  it would mean pointing OpenWebUI's `rag.content_extraction_engine` at
  something OCR-capable (Tika, Docling, Mistral OCR, etc.), a separate,
  larger decision than this proxy-side fallback.
- Even once text is successfully extracted (via OCR or otherwise),
  Graphiti's own entity/fact extraction can still legitimately find
  nothing worth graphing from short or noisy OCR output — that's a
  separate concern from whether ingestion itself succeeds, and is not
  something this ADR changes. See [[project_openwebui_embedding_model_fix]]-adjacent
  memory notes and ADR 0003/the graphiti hotfix for that layer.
