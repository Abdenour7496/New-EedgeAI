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
- **Legacy `neo4j_data`/`qdrant_data`/Buzz volumes** — removed from
  `docker-compose.unified.yml` entirely (dead since the Graphiti/FalkorDB
  migration, no service ever mounted them again). If you deployed before that
  cleanup, the underlying Docker volumes may still exist on the host; they
  were never backed up and are safe to `docker volume rm` once you've
  confirmed you don't need to roll back to the old Neo4j/Qdrant/Buzz stack.

## Restoring FalkorDB from a snapshot

```bash
backup/restore-falkordb.sh                          # newest snapshot, asks to confirm
backup/restore-falkordb.sh backups/falkordb/falkordb-20260824T060000Z.rdb -y   # a specific one, no prompt
```

Stops the `falkordb` service, copies the chosen `.rdb` into its data volume as
`dump.rdb` (FalkorDB loads that from its data directory on startup), then
starts it back up. The script finds the right volume by its compose label, so
it doesn't matter what the project-name prefix happens to be
(`docker volume ls | grep falkordb` to see it directly).

## Restoring MinIO originals

```bash
backup/restore-minio.sh                              # backups/minio/documents -> documents bucket
backup/restore-minio.sh backups/minio/openwebui openwebui   # a different dir/bucket pair
```

Mirrors a backup dir back into a bucket — additive, never deletes anything
already in the destination. Requires the `minio` service to be up. For a
partial restore of specific files instead of the whole tree, run `mc mirror`
or `mc cp -r` by hand the same way the script does internally.

## Recovery drills — proving a backup is actually restorable

A backup job can "succeed" for months while quietly writing snapshots
nothing can load (disk full mid-write, a format change, a broken `mc`
alias) — the failure only shows up the day you actually need to restore.
`backup/verify-backup.sh` closes that gap: it loads the newest FalkorDB
snapshot into a disposable container + volume (never touches the live
stack), confirms it starts and answers a `GRAPH.QUERY`, and checks the MinIO
mirror is present and fresh. Exit code 0 means both checks passed.

```bash
backup/verify-backup.sh
```

Run it after any change to the backup pipeline, and on a schedule (daily
cron, or a CI job with Docker available) so a broken backup surfaces on its
own instead of at restore time. It needs a GNU userland and Docker on the
host — same assumptions as `backup.sh` itself — and takes on the order of a
minute (mostly waiting for the drill FalkorDB container to become ready).
