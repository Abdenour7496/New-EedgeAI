#!/usr/bin/env bash
# Restores FalkorDB from a snapshot produced by backup/backup.sh.
#
# Usage:
#   backup/restore-falkordb.sh [snapshot-path] [-y|--yes]
#
# With no path given, restores the newest snapshot under ./backups/falkordb.
# Stops the falkordb service, copies the chosen .rdb into its data volume as
# dump.rdb, then starts it back up — FalkorDB loads dump.rdb from its data
# directory on startup. This is the same procedure backup/README.md used to
# document as manual steps; verify-backup.sh runs an equivalent restore
# against a disposable volume to prove a snapshot is actually loadable
# without touching the live stack.
#
# Run from the repo root (where docker-compose.unified.yml lives).
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.unified.yml}"
BACKUP_DIR="${BACKUP_DIR:-backups/falkordb}"

SNAPSHOT=""
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes) ASSUME_YES=1 ;;
        *) SNAPSHOT="$arg" ;;
    esac
done

if [ -z "$SNAPSHOT" ]; then
    SNAPSHOT="$(ls -t "$BACKUP_DIR"/*.rdb 2>/dev/null | head -n1 || true)"
    if [ -z "$SNAPSHOT" ]; then
        echo "No snapshots found in $BACKUP_DIR and none given on the command line." >&2
        exit 1
    fi
    echo "No snapshot given — using newest: $SNAPSHOT"
fi
if [ ! -f "$SNAPSHOT" ]; then
    echo "Snapshot not found: $SNAPSHOT" >&2
    exit 1
fi

# Resolve the real volume name by its compose label instead of assuming a
# project-name prefix (e.g. "eedgeai_falkordb_data") that varies with the
# directory the stack was started from.
VOLUME="$(docker volume ls --filter label=com.docker.compose.volume=falkordb_data --format '{{.Name}}' | head -n1)"
if [ -z "$VOLUME" ]; then
    echo "Could not find the falkordb_data Docker volume (has the stack ever been started?)." >&2
    exit 1
fi

echo "Restoring $SNAPSHOT -> volume $VOLUME (this overwrites FalkorDB's current data)"
if [ "$ASSUME_YES" -ne 1 ]; then
    read -r -p "Continue? [y/N] " confirm
    case "$confirm" in
        y|Y) ;;
        *) echo "Aborted."; exit 1 ;;
    esac
fi

docker compose -f "$COMPOSE_FILE" stop falkordb

docker run --rm \
    -v "$VOLUME:/data" \
    -v "$(cd "$(dirname "$SNAPSHOT")" && pwd):/backup:ro" \
    alpine cp "/backup/$(basename "$SNAPSHOT")" /data/dump.rdb

docker compose -f "$COMPOSE_FILE" up -d falkordb

echo "Restore complete. Tail logs to confirm the load succeeded:"
echo "  docker compose -f $COMPOSE_FILE logs -f falkordb"
