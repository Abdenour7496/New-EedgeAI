# ADR 0014: Every pulled image pinned by digest

**Status:** Accepted
**Date:** 2026-08-27
**Decision owners:** EedgeAI maintainers

## Context

Missing image pinning was a known, previously-documented gap (called out
during a production-readiness review): most services in
`docker-compose.unified.yml` referenced floating tags (`latest`, `alpine`,
`standalone`, `2.8-alpine`) — a re-pull at any point (a fresh clone, a
`docker compose pull`, a rebuild on another machine) could silently land a
different image than the one actually tested against.

## Decision

Every externally-pulled image now pins both its tag (kept, for
readability — this project's convention, not any registry's actual
semver) and its digest (`image:tag@sha256:...`), resolved from the exact
image already running and verified throughout this session's testing —
not upgraded to some other "latest stable," which would reintroduce
untested risk in the other direction. Covers: `falkordb/falkordb`,
`ollama/ollama` (both services using it), `minio/minio`, `minio/mc`,
`travisvn/openai-edge-tts`, `ghcr.io/open-webui/open-webui`,
`caddy`, `oliver006/redis_exporter`, `prom/prometheus`, `grafana/grafana`,
`nginx`, `alpine`, `zepai/knowledge-graph-mcp`.

Locally-built images (`eedgeai-proxy`, `eedgeai-graphiti`,
`eedgeai-openclaw`, `eedgeai-ingest-watcher`, `eedgeai-backup`) are
unaffected — their Dockerfile + build context, already version-controlled
in this repo, is the pin.

Verified: `docker compose config --quiet` validates, `docker compose pull`
resolves every pinned digest against the already-cached local image with
no unexpected download, and the full stack came back healthy after
compose reconciled the (byte-identical, digest-matched) image references.

## Consequences

- An intentional upgrade of any pinned image now requires manually
  updating both the tag and the digest together — there's no longer an
  implicit "just re-pull latest" path. This is the point: any version
  change becomes a visible, deliberate diff.
- Digest pins are immutable but not self-documenting about *which*
  version they represent beyond whatever the tag string still says (e.g.
  `latest@sha256:...` — the tag itself isn't informative for images whose
  upstream doesn't publish clean semver tags). Acceptable trade-off:
  immutability was the actual goal here, not a readable version history.
