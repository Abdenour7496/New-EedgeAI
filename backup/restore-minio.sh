#!/usr/bin/env bash
# Restores MinIO's documents bucket from the mirror backup/backup.sh maintains.
#
# Usage:
#   backup/restore-minio.sh [source-dir] [bucket]
#
# Defaults to restoring ./backups/minio/documents into the `documents`
# bucket. Uses `mc mirror` (additive: copies missing/changed objects in,
# never deletes from the destination) from a throwaway minio/mc container on
# the same Docker network as the running minio service.
#
# Run from the repo root, with the stack's minio service already up
# (docker compose -f docker-compose.unified.yml up -d minio).
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.unified.yml}"
S3_ACCESS_KEY="${DOCS_S3_ACCESS_KEY:-gcor_docs}"
S3_SECRET_KEY="${DOCS_S3_SECRET_KEY:-gcor_docs_secret}"
BUCKET="${2:-${DOCS_S3_BUCKET:-documents}}"
SRC_DIR="${1:-backups/minio/$BUCKET}"

if [ ! -d "$SRC_DIR" ]; then
    echo "Backup source dir not found: $SRC_DIR" >&2
    exit 1
fi

MINIO_CID="$(docker compose -f "$COMPOSE_FILE" ps -q minio)"
if [ -z "$MINIO_CID" ]; then
    echo "minio service is not running (docker compose -f $COMPOSE_FILE up -d minio first)." >&2
    exit 1
fi
NETWORK="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$MINIO_CID")"
if [ -z "$NETWORK" ]; then
    echo "Could not determine minio's Docker network." >&2
    exit 1
fi

echo "Restoring $SRC_DIR -> bucket '$BUCKET' (network: $NETWORK)"
docker run --rm \
    --network "$NETWORK" \
    -v "$(cd "$SRC_DIR" && pwd):/restore:ro" \
    minio/mc:latest sh -c "
        mc alias set restoresrc http://minio:9000 '$S3_ACCESS_KEY' '$S3_SECRET_KEY' >/dev/null &&
        mc mirror /restore restoresrc/$BUCKET
    "

echo "Restore complete."
