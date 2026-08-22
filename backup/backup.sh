#!/usr/bin/env bash
# Periodic backup for the two things that actually hold irreplaceable state:
#   - FalkorDB (the graph + vector store Graphiti writes to — every extracted
#     entity/fact/embedding lives only here)
#   - MinIO's documents bucket (original files + chat-session JSON archives)
#
# Everything else in the stack (Ollama models, Grafana/Prometheus config,
# OpenWebUI's own sqlite state) is either re-fetchable or not the system of
# record, so it's out of scope here. See backup/README.md for the restore
# procedure and what this deliberately does not cover.
set -euo pipefail

FALKORDB_HOST="${FALKORDB_HOST:-falkordb}"
FALKORDB_PORT="${FALKORDB_PORT:-6379}"
FALKORDB_PASSWORD="${FALKORDB_PASSWORD:-}"
S3_ENDPOINT_URL="${S3_ENDPOINT_URL:-http://minio:9000}"
S3_ACCESS_KEY="${S3_ACCESS_KEY:-gcor_docs}"
S3_SECRET_KEY="${S3_SECRET_KEY:-gcor_docs_secret}"
S3_BUCKET="${S3_BUCKET:-documents}"
INTERVAL="${BACKUP_INTERVAL_SECONDS:-21600}"   # default: every 6 hours
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

BACKUP_ROOT=/backups
FALKORDB_DIR="$BACKUP_ROOT/falkordb"
MINIO_DIR="$BACKUP_ROOT/minio"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup: $*"; }

redis_cli() {
    if [ -n "$FALKORDB_PASSWORD" ]; then
        redis-cli -h "$FALKORDB_HOST" -p "$FALKORDB_PORT" -a "$FALKORDB_PASSWORD" --no-auth-warning "$@"
    else
        redis-cli -h "$FALKORDB_HOST" -p "$FALKORDB_PORT" "$@"
    fi
}

backup_falkordb() {
    mkdir -p "$FALKORDB_DIR"
    local ts dest
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    dest="$FALKORDB_DIR/falkordb-$ts.rdb"

    # --rdb pulls a point-in-time RDB snapshot over the wire (SYNC), no shell
    # access on the falkordb container needed.
    if [ -n "$FALKORDB_PASSWORD" ]; then
        redis-cli -h "$FALKORDB_HOST" -p "$FALKORDB_PORT" -a "$FALKORDB_PASSWORD" --no-auth-warning --rdb "$dest"
    else
        redis-cli -h "$FALKORDB_HOST" -p "$FALKORDB_PORT" --rdb "$dest"
    fi
    log "FalkorDB snapshot -> $dest ($(du -h "$dest" | cut -f1))"
}

backup_minio() {
    mkdir -p "$MINIO_DIR"
    mc alias set backupsrc "$S3_ENDPOINT_URL" "$S3_ACCESS_KEY" "$S3_SECRET_KEY" >/dev/null
    # Mirror only (never --remove/--overwrite-newer-than-src semantics that could
    # delete a local backup because the source changed) — this is additive:
    # new/changed objects are copied in, nothing already backed up is pruned
    # here (pruning is handled separately, by age, below).
    mc mirror --quiet "backupsrc/$S3_BUCKET" "$MINIO_DIR/$S3_BUCKET"
    log "MinIO bucket '$S3_BUCKET' mirrored -> $MINIO_DIR/$S3_BUCKET"
}

prune_old() {
    # Age-based retention for the FalkorDB snapshots (each run adds a new
    # timestamped file). The MinIO mirror is a single always-current tree, not
    # timestamped copies, so nothing to prune there.
    find "$FALKORDB_DIR" -name '*.rdb' -mtime "+$RETENTION_DAYS" -print -delete 2>/dev/null | while read -r f; do
        log "pruned old snapshot $f"
    done || true
}

log "starting: interval=${INTERVAL}s retention=${RETENTION_DAYS}d bucket=$S3_BUCKET"
while true; do
    if backup_falkordb; then :; else log "FalkorDB backup FAILED"; fi
    if backup_minio; then :; else log "MinIO backup FAILED"; fi
    prune_old
    sleep "$INTERVAL"
done
