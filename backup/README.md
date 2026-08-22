# Backups

Added during the production-readiness review: the stack previously had one
manual, months-stale `backups/webui-backup-*.db` snapshot and nothing
automated for the actual sources of truth. The `backup` service in
`docker-compose.unified.yml` now runs continuously and, every
`BACKUP_INTERVAL_SECONDS` (default 21600s / 6h):

1. Pulls a point-in-time RDB snapshot of FalkorDB — the graph + vector store;
   every entity/fact/embedding Graphiti has ever extracted lives only here —
   via `redis-cli --rdb`, into `backups/falkordb/falkordb-<timestamp>.rdb`.
2. Mirrors the MinIO `documents` bucket (original files + chat-session JSON
   archives) into `backups/minio/documents/`.
3. Deletes FalkorDB snapshots older than `BACKUP_RETENTION_DAYS` (default 14).
   The MinIO mirror is a single always-current tree, not timestamped copies,
   so nothing is pruned there — deleting something from MinIO also removes it
   from the next mirror pass, exactly like it should.

`./backups/` on the host is where all of this lands (already gitignored). For
real production use, also copy this directory to storage that isn't the same
disk/node as the stack itself — this backup service protects against data
loss inside FalkorDB/MinIO, not against losing the whole host.

## What this deliberately does not cover

- **Ollama models** — re-`ollama pull`-able, not unique state.
- **Grafana/Prometheus config** — lives in `monitoring/`, already in git.
- **OpenWebUI's own sqlite DB** (`openwebui_data` volume) — chat metadata,
  user accounts, settings. Not covered by this automated job; the one
  existing manual snapshot in `backups/webui-backup-*.db` was of this. If
  losing OpenWebUI's user accounts/settings independent of the knowledge base
  matters to you, add a periodic `docker compose exec openwebui sqlite3
  /app/backend/data/webui.db ".backup /app/backend/data/backup.db"` (or
  equivalent) alongside this service.
- **Legacy `neo4j_data`/`qdrant_data`/Buzz volumes** — already dead, not
  backed up on purpose (see `docker-compose.unified.yml`'s volumes comment).

## Restoring FalkorDB from a snapshot

```bash
docker compose -f docker-compose.unified.yml stop falkordb
# Copy the chosen snapshot into the falkordb_data volume as dump.rdb, then:
docker run --rm -v eedgeai_falkordb_data:/data -v "$(pwd)/backups/falkordb:/backup" \
  alpine cp /backup/falkordb-<timestamp>.rdb /data/dump.rdb
docker compose -f docker-compose.unified.yml up -d falkordb
```

FalkorDB loads `dump.rdb` from its data directory on startup. Confirm the
volume name matches your project name (`docker volume ls | grep falkordb`) —
it's prefixed with the compose project name, `eedgeai_` by default.

## Restoring MinIO originals

The mirror in `backups/minio/documents/` is a plain file tree — copy it back
with `mc mirror backups/minio/documents local/documents` (after `mc alias set
local http://localhost:9000 <access-key> <secret-key>`), or `mc cp -r` for a
partial restore of specific documents.
