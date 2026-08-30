# ADR 0021: ingest-cli's missing idempotency, and correcting openclaw's stale self-documentation

**Status:** Accepted
**Date:** 2026-08-30
**Decision owners:** EedgeAI maintainers

## Context

Reported: asking the "openclaw" model directly to summarize and ingest an
attached document initially got a refusal ("I can't provide a summary of
the document or ingest it into a collection/categorization system"), and
a later attempt left the file safely in a custom MinIO bucket
(`new-algeria`) but with only 1 of what turned out to be 3 upload
attempts producing a matching Graphiti episode.

### First hypothesis (wrong, corrected before shipping)

Initially assumed the agent was improvising raw MinIO writes plus a
separate, uncoordinated `graphiti-memory` MCP call — reasonable given the
symptom (files without matching episodes), but wrong. Tracing openclaw's
own logs for the exact window showed the JSON payloads exchanged with the
LLM were graphiti-core's own internal extraction schemas
(`ExtractedEntities`, entity-resolution output, `edges` with
`relation_type`/`source_entity_id`/`valid_at`/`invalid_at`) — the
signature of a real Graphiti `add_episode()` pipeline running, not an
ad-hoc agent script.

### Actual root cause

`openclaw/ingest.js` (installed as `ingest-cli`, already wired into the
openclaw image's `PATH`) is a real, purpose-built, self-contained
ingestion tool — extracts text, chunks it, stores the original to MinIO
under the same `originals/<doc_id>/<filename>` convention
`proxy/main.py` uses, then POSTs episodes directly to Graphiti's
`/messages`. It doesn't need `GCOR_API_KEY` (talks to `graphiti:8000`
directly, which has no auth layer of its own — only the proxy adds one)
and openclaw's container already has the S3 credentials it needs. The
agent used this tool correctly.

What it was missing: **any idempotency protection**. Unlike
`proxy/main.py`'s `/api/ingest` (docs/adr/0012 — an in-memory,
short-TTL cache viable because the proxy is one long-lived server
process), `ingest-cli` is a fresh short-lived process every invocation,
with `documentId` derived from `` `${source}-${Date.now()}` `` — a new
id, and therefore a new MinIO key, on every single call regardless of
content. Sequence, reconstructed from timestamps and MinIO ETags (three
uploads, identical byte-for-byte content, 48 seconds apart): the agent's
first `ingest-cli` invocation for a Graphiti-extraction-heavy document
(competing for the same limited openclaw concurrency lane as everything
else in this stack) either errored or appeared to hang; the agent
retried twice more before one attempt finally completed. Each attempt's
MinIO write succeeds fast and unconditionally; only the slower,
contention-prone Graphiti call was the actual point of failure — so
every retry left a fresh, orphaned S3 object with no matching episode,
until the last one landed.

### A second, independent, real gap found along the way

`TOOLS.md` — the agent's own persisted notes about its environment,
something it's explicitly instructed (`AGENTS.md`) to consult and keep
current — still described the stack as **"OpenClaw + Neo4j + Qdrant +
MCP + Open WebUI + MinIO"** and said "Neo4j connected: Used for graph
long-term memory" / "Qdrant connected: Used for vector search." Neo4j
and Qdrant were removed from this stack well before this session (see
docs/adr/0001) — this note was never updated across that migration, and
never mentioned `ingest-cli` or the Graphiti/FalkorDB backend at all.
This is very plausibly what the initial refusal was about: consulting
stale notes that don't describe any working ingestion path, the agent
had nothing correct to reach for.

## Decision

1. **Added idempotency to `ingest-cli`** (`openclaw/ingest.js`):
   `documentId` is now derived from a hash of the actual content (+
   collection + title) instead of `Date.now()`, so retrying the same
   file/title/collection reliably computes the same id. Before ingesting,
   it now checks Graphiti directly (`GET /episodes/{collection}`) for an
   existing episode carrying that id, and if found, skips re-ingesting
   entirely and reports the existing result with `"deduplicated": true`
   instead of creating a duplicate.
2. **Corrected `TOOLS.md`** in the agent's workspace (a runtime-persisted
   file, not part of this repo's own tracked source — edited directly in
   the running container): removed the stale Neo4j/Qdrant references,
   documented `ingest-cli` as the correct, already-atomic-enough tool for
   turning a document into searchable knowledge, and noted
   `graphiti-memory` MCP's actual role (querying/recalling existing
   knowledge, not creating new document episodes).
3. **Did not** redirect `ingest-cli` to call the proxy's `/api/ingest`
   instead of talking to Graphiti directly. Considered this explicitly
   (the user's original suggestion) and rejected it: `ingest-cli` already
   mirrors the proxy's own storage convention and extraction logic
   closely enough, adding a hop through the proxy would require
   distributing `GCOR_API_KEY` into openclaw's environment (a real
   secret-exposure question, and it currently has none), and the actual
   gap — missing idempotency — is now fixed at the source instead.

## Consequences

- Verified directly: ran `ingest-cli --stdin` twice with identical
  content — first run created a real episode and MinIO object; second
  run detected the existing episode, reported `deduplicated: true`,
  created no duplicate object or episode. Cleaned up the two orphaned
  MinIO uploads from the original incident (the third, successful
  attempt's episode and file are intact and were left alone).
- `ingest-cli`'s idempotency check adds one `GET /episodes/{group}`
  round-trip before ingesting — cheap, and only Graphiti-local (no
  openclaw/LLM call involved), so it doesn't compete for the
  concurrency-constrained lane that caused the original failures.
- `TOOLS.md` is runtime state in the `openclaw_config` volume, not
  tracked by git — this fix doesn't show up in a `git diff` of this
  repo. Worth knowing if this exact gap needs re-checking after a volume
  reset or a fresh deployment: the corrected content lives in this ADR
  and should be re-applied (or a bootstrap step added) if that ever
  happens.
- Does not address why the underlying Graphiti call was slow/erroring
  enough to need three attempts in the first place — that's downstream
  of the same openclaw concurrency-lane contention already documented in
  docs/adr/0012 and docs/adr/0020, not a new mechanism.
