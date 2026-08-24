# ADR 0005: CI release gate for merges to master

**Status:** Accepted
**Date:** 2026-08-24
**Decision owners:** EedgeAI maintainers

## Context

This repo had no CI at all — every change (including three prior rounds of
production-readiness fixes) shipped by direct inspection and manual
verification against a locally running stack. That caught real bugs each
time, but only because someone remembered to run the checks; nothing
enforced it. Concretely, `proxy/tests/test_collection_metadata.py` had five
tests silently broken since the Graphiti/FalkorDB migration (ADR 0001) —
referencing `main._qdrant_collection_doc_ids`, `main._neo4j_fetch_documents`,
`main._EXPAND_CYPHER`, none of which exist anymore — and nothing surfaced
that until this review went looking. A broken test suite that nothing runs
is equivalent to no test suite.

## Decision

Add `.github/workflows/ci.yml`, gating every push/PR to `master` on:

1. **`docker compose -f docker-compose.unified.yml config -q`** — catches
   YAML/interpolation errors before anyone finds out at deploy time. Verified
   to pass with zero environment variables set (a fresh CI checkout has no
   `.env`); every required-looking variable already has a `:-default` in the
   compose file, so this only fails on genuine structural breakage.
2. **`python -m py_compile` over every tracked `.py` file** (`git ls-files
   '*.py'`) — a fast, dependency-free syntax gate across `proxy/`,
   `graphiti/`, `ingest-watcher/`, and the OpenWebUI Functions, without
   needing each service's own dependency set installed.
3. **The proxy's unit test suite** (`test_collection_metadata.py`,
   `test_governance.py`), run inside the actual `proxy/Dockerfile` image
   with the working tree mounted over `/app` — same execution environment
   production runs in, not a bare-runner Python. Fixed the five drifted
   tests as part of adding this gate (see commit history); a gate that starts
   red on day one trains everyone to ignore it.

`test_openwebui_stream_smoke.py` is deliberately excluded from this gate —
it makes a live HTTP call to a running proxy (`PROXY_CHAT_URL`) and is a
post-deploy smoke check, not something a stateless CI runner can satisfy.

A fourth job reports any service still pinned to `:latest`
(`falkordb`, `ollama`, `minio`, `minio/mc`, `prometheus`, `grafana`,
`graphiti-mcp`, `tts` — see README) as an informational, always-passing
step. It is not a blocking gate: making it one right now would fail on
every PR for pre-existing, already-documented debt unrelated to the change
being reviewed. Promoting it to blocking is a separate decision, to be made
once those images are actually pinned.

## Consequences

- A PR that breaks `docker compose config`, introduces a Python syntax
  error, or breaks the proxy's request-handling contract (collection
  listing, search filtering, streaming chat completions, access-level
  validation) now fails visibly instead of shipping.
- The gate is only as good as its coverage: it does not run
  `graphiti/app.py`'s hotfix logic, `ingest-watcher`, or any integration
  path end-to-end. Extending coverage there is future work, not blocked by
  this ADR.
- Branch protection (requiring these checks before merge) is a GitHub
  repository setting, not something this workflow file enforces by itself —
  turn it on in the repo's Settings > Branches once this workflow has run
  successfully at least once.
