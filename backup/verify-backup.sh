#!/usr/bin/env bash
# Recovery drill: proves the latest backup is actually restorable, not just
# written. The gap this closes: a backup job can run "successfully" every 6
# hours for months while silently producing snapshots nothing can load
# (disk full mid-write, a FalkorDB version bump that changes the RDB format,
# a stale/broken mc alias) and nobody finds out until the day it matters.
#
# Run on demand, or wire into cron/CI to run daily — see backup/README.md.
# Never touches the live stack: FalkorDB is verified by loading the newest
# snapshot into a disposable container + volume that this script creates and
# tears down itself; the MinIO check only reads the mirror on disk.
#
# Exit code 0 = both checks passed. Non-zero = at least one failed; read the
# FAIL lines above the exit for which one.
#
# Requires a GNU userland (date -r, stat -c, find -printf) — matches the
# rest of this backup/ tooling, meant for the Linux Docker host this stack
# runs on.
set -uo pipefail

FALKORDB_DIR="${FALKORDB_DIR:-backups/falkordb}"
MINIO_DIR="${MINIO_DIR:-backups/minio}"
FALKORDB_DATABASE="${FALKORDB_DATABASE:-eedgeai}"
# Snapshots/mirrors older than this look stuck, not just "not synced yet" —
# default is 2x the backup service's own interval.
MAX_AGE_SECONDS="${BACKUP_MAX_AGE_SECONDS:-$(( ${BACKUP_INTERVAL_SECONDS:-21600} * 2 ))}"

fail=0
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) verify-backup: $*"; }

# ── FalkorDB: load the newest snapshot into a disposable container ───────────
SNAPSHOT="$(ls -t "$FALKORDB_DIR"/*.rdb 2>/dev/null | head -n1 || true)"
if [ -z "$SNAPSHOT" ]; then
    log "FAIL: no FalkorDB snapshots found in $FALKORDB_DIR"
    fail=1
else
    age=$(( $(date +%s) - $(date -r "$SNAPSHOT" +%s) ))
    size=$(stat -c%s "$SNAPSHOT" 2>/dev/null || stat -f%z "$SNAPSHOT")
    if [ "$age" -gt "$MAX_AGE_SECONDS" ]; then
        log "FAIL: newest snapshot $SNAPSHOT is ${age}s old (> ${MAX_AGE_SECONDS}s) — is the backup service running?"
        fail=1
    fi
    if [ "$size" -lt 100 ]; then
        log "FAIL: $SNAPSHOT is suspiciously small (${size} bytes) — likely a failed/truncated write"
        fail=1
    fi

    suffix="$$-$(date +%s)"
    DRILL_VOLUME="verify-backup-drill-$suffix"
    DRILL_CONTAINER="verify-backup-drill-$suffix"
    cleanup() {
        docker rm -f "$DRILL_CONTAINER" >/dev/null 2>&1 || true
        docker volume rm "$DRILL_VOLUME" >/dev/null 2>&1 || true
    }
    trap cleanup EXIT

    docker volume create "$DRILL_VOLUME" >/dev/null
    # Gate on this step explicitly — an unchecked failure here would leave the
    # drill container starting from an empty volume, which still answers
    # PING and GRAPH.QUERY (on an empty graph) and would report a false OK.
    if ! docker run --rm \
        -v "$DRILL_VOLUME:/data" \
        -v "$(cd "$(dirname "$SNAPSHOT")" && pwd):/backup:ro" \
        alpine cp "/backup/$(basename "$SNAPSHOT")" /data/dump.rdb; then
        log "FAIL: could not copy $SNAPSHOT into the drill volume — see docker output above"
        fail=1
    else
        docker run -d --name "$DRILL_CONTAINER" \
            -v "$DRILL_VOLUME:/var/lib/falkordb/data" \
            falkordb/falkordb:latest >/dev/null

        ready=0
        for _ in $(seq 1 30); do
            if docker exec "$DRILL_CONTAINER" redis-cli -p 6379 ping 2>/dev/null | grep -q PONG; then
                ready=1
                break
            fi
            sleep 1
        done
        if [ "$ready" -ne 1 ]; then
            log "FAIL: drill FalkorDB never came up from $SNAPSHOT — the snapshot is likely not restorable"
            fail=1
        else
            query_out="$(docker exec "$DRILL_CONTAINER" redis-cli -p 6379 GRAPH.QUERY "$FALKORDB_DATABASE" "MATCH (n) RETURN count(n)" 2>&1)"
            query_status=$?
            if [ "$query_status" -ne 0 ]; then
                log "FAIL: drill FalkorDB came up but GRAPH.QUERY against '$FALKORDB_DATABASE' failed: $query_out"
                fail=1
            else
                log "OK: $SNAPSHOT restores and is queryable (GRAPH.QUERY against '$FALKORDB_DATABASE' succeeded)"
            fi
        fi
    fi
fi

# ── MinIO mirror: freshness + non-empty ───────────────────────────────────────
newest_epoch="$(find "$MINIO_DIR" -type f -printf '%T@\n' 2>/dev/null | sort -n | tail -n1)"
if [ -z "$newest_epoch" ]; then
    log "FAIL: MinIO mirror $MINIO_DIR is missing or empty"
    fail=1
else
    mirror_age=$(( $(date +%s) - ${newest_epoch%.*} ))
    if [ "$mirror_age" -gt "$MAX_AGE_SECONDS" ]; then
        log "FAIL: newest file in $MINIO_DIR is ${mirror_age}s old (> ${MAX_AGE_SECONDS}s) — is the backup service running?"
        fail=1
    else
        log "OK: MinIO mirror in $MINIO_DIR is present and fresh"
    fi
fi

exit $fail
