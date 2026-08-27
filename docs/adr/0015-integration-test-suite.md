# ADR 0015: An integration test suite, and the real bug it found on day one

**Status:** Accepted
**Date:** 2026-08-27
**Decision owners:** EedgeAI maintainers

## Context

Addressing "no automated test suite" from a production-readiness review.
This stack's real bugs across this whole session — `_flatten_messages`
reading the wrong field, timeout mismatches between a client and the
proxy, duplicate episodes from a naive retry, a shared concurrency lane
getting starved — were all integration-level: a caller, a real backend,
real timing. None of them were the kind of thing a mocked unit test
catches. A meaningful test suite for this stack has to include tests that
hit the real thing.

There was already a pure-unit suite (`proxy/tests/test_collection_metadata.py`,
`test_governance.py` — mocked httpx, no live dependencies, gated in CI per
`.github/workflows/ci.yml` and ADR 0005) and one existing live-stack smoke
test (`test_openwebui_stream_smoke.py`, deliberately excluded from the CI
gate as a "post-deploy check"). Found this by actually checking — first
attempt at building "a" test suite from scratch nearly duplicated
infrastructure that already existed; `git log -- proxy/tests/` surfaced it
before that happened.

## Decision

- `proxy/tests/test_ingest.py` + `helpers.py`: new live-stack integration
  tests, same category as `test_openwebui_stream_smoke.py` — not part of
  the CI gate (needs `GCOR_API_KEY` and a reachable proxy; skips, not
  fails, if either is missing). Covers: basic ingest creates an episode,
  a duplicate submission is deduped (ADR 0012 regression test), and — see
  below — delete actually removes the derived facts.
- `openwebui-functions/tests/test_gcor_chat_session_ingest.py`: new *pure*
  unit tests (no live stack, no mocking needed — the debounce/in-flight
  logic and `_flatten_messages` are both plain Python) for
  `gcor_chat_session_ingest.py`'s trickiest logic. Added to the CI gate
  (`.github/workflows/ci.yml`'s new `gcor-functions-tests` job) since
  nothing about them needs a live stack.
- `image-pin-report`'s CI check fixed alongside this — its old
  `grep "image:.*:latest"` would false-positive against ADR 0014's digest
  pins (the human-readable tag portion is unchanged, e.g.
  `falkordb:latest@sha256:...`); now checks for the absence of
  `@sha256:` instead.

### The bug this suite found immediately

Writing `test_ingest.py`'s delete-then-verify-it's-gone test surfaced a
real, serious gap: `graphiti/app.py`'s `delete_episode()` deleted only the
episode node. The `EntityEdge`s (facts) Graphiti extracted from it
survived, completely untouched, permanently orphaned — still fully
returned by `/api/search` with their original `graphiti_edge_uuid`, long
after the episode was gone from `/episodes/{group}`'s own listing.
Confirmed directly: test content ingested and deleted earlier in this same
session (`"Acme Corp's quarterly revenue..."`) was still the top search
result for an unrelated query, minutes and several unrelated ingests
later.

This means **every single-document delete this stack has ever done left
its extracted facts behind** — a real data-lifecycle gap (a user asking
for a document to be "removed" would find its content still fully
searchable and citable in chat afterward) not previously known, since
nothing had checked for it directly before this test suite forced the
question "is the deleted content actually gone?"

Fixed in `graphiti/app.py`:
- `delete_episode()` now also finds every edge in the episode's group
  referencing it (`EntityEdge.episodes`, which can list multiple
  corroborating episodes per fact) and either strips just this episode's
  uuid (if others remain — the fact is still backed by them) or deletes
  the edge outright (if this was its sole source). Returns
  `edges_deleted`/`edges_updated` counts, now surfaced through the
  proxy's own `/api/collections/{name}/docs/{doc_id}` response
  (`proxy/main.py`) instead of being discarded.
- New `POST /admin/gc-orphaned-edges/{group_id}`: a retroactive cleanup
  for edges already orphaned by the pre-fix behavior — any edge in the
  group whose `episodes` list references a uuid that no longer exists
  gets the same strip-or-delete treatment. Idempotent; a clean group does
  no writes. Run once against every existing group after deploying this
  fix. Used directly to clean up this session's own accumulated test
  debris: 12 orphaned edges in `documents`, 1 in `chat_sessions`.

### Also found and fixed while building the unit tests

`_flatten_messages` (`gcor_chat_session_ingest.py`) crashed —
`AttributeError`, not a graceful skip — on a malformed (non-dict) entry
in `chat.history.messages`, because the sort ran before the type-filtering
that already existed for every other malformed-entry case. Caught by
`test_skips_malformed_entries` before ever reaching production. Fixed by
filtering to dict entries before sorting, not after.

### A second, separate gap found but deliberately not chased here

Building `test_ingest.py`'s basic ingest-then-search test surfaced a
different, real, but distinct issue: a freshly-created fact was not
reliably found by `/api/search` — or even Graphiti's own raw `/search`
endpoint — for an on-topic query, immediately or after retrying with
backoff, *despite the edge itself independently confirmed to exist*
(the delete-cascade test's own delete call found and removed exactly one
edge for equivalent fresh content). That gap — an edge existing but not
surfacing via hybrid search — looks like embedding/indexing timing or
ranking behavior against a now-substantially-populated real graph, not
something in the same fix-family as this ADR's other items. Deliberately
not investigated further in this pass (scope was already three unplanned
findings deep); the corresponding test asserts only what's reliably true
(ingest creates an episode + fact) rather than asserting on search
ranking that isn't yet understood. Worth its own dedicated look.

**Update:** got that dedicated look — see
[docs/adr/0016-falkordb-connection-health.md](0016-falkordb-connection-health.md).
Root cause traced to the long-lived FalkorDB connection, not search logic
or ranking at all.

## Consequences

- Anyone running this stack before this fix has orphaned edges sitting in
  FalkorDB right now if they've ever deleted a single document (not a
  whole group — `delete_group()` was already correct). Run
  `POST /admin/gc-orphaned-edges/{group_id}` for every group once after
  upgrading.
- `test_ingest.py` is not fast (each test does a real ingest, which
  depends on Graphiti extraction — tens of seconds to a few minutes under
  load, per ADR 0012). Not run in CI for exactly this reason; run manually
  or as a post-deploy check.
- The unresolved search-retrieval gap above means "ingestion succeeded"
  and "the content is reliably findable via search right now" are not the
  same guarantee yet — worth keeping in mind before treating a successful
  `/api/ingest` response as proof the content is immediately usable in a
  chat's retrieved context.
