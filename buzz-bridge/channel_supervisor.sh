#!/bin/sh
# Keeps buzz-acp's --channels argument in sync with whatever channels this
# bot identity can currently see: every open channel in the community, plus
# any private channel an owner/admin has invited it into. Replaces a static
# BUZZ_ACP_CHANNELS list so the bridge needs no manual config change (or
# restart) as channels come and go.
#
# - Open channels: self-joined automatically (idempotent — re-attempting on
#   an existing membership is a harmless no-op, so we just always retry).
# - Private channels: joining requires an existing owner/admin to invite the
#   bot's pubkey from the Buzz client (NIP-29 group semantics — there is no
#   way around this by design, and no lesser-privileged identity can bypass
#   it either). Once invited, it shows up in `channels list` on the next
#   poll like any other membership.
# - buzz-acp itself live-subscribes to any channel already in its --channels
#   list the moment membership changes (confirmed against a real relay: a
#   mid-run `channels join` produced "membership notification: subscribing
#   to new channel" with no restart). So once a channel has ever appeared in
#   the set we last launched with, new membership activates immediately —
#   this loop only needs to relaunch buzz-acp when a *new* channel ID shows
#   up that wasn't there before.
set -u

STATE_FILE="${CHANNEL_STATE_FILE:-/home/buzzbridge/.known_channels}"
POLL_INTERVAL="${CHANNEL_POLL_INTERVAL:-60}"
ACP_PID=""

log() { echo "[channel_supervisor] $*" >&2; }

fetch_channel_ids() {
    buzz-cli channels list 2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ids = sorted({c["channel_id"] for c in data if "channel_id" in c})
print(",".join(ids))
'
}

start_acp() {
    channels="$1"
    log "starting buzz-acp with channels: ${channels:-<none>}"
    /usr/local/bin/buzz-acp --channels "$channels" &
    ACP_PID=$!
}

stop_acp() {
    if [ -n "$ACP_PID" ] && kill -0 "$ACP_PID" 2>/dev/null; then
        kill -TERM "$ACP_PID" 2>/dev/null
        wait "$ACP_PID" 2>/dev/null
    fi
    ACP_PID=""
}

trap 'log "shutting down"; stop_acp; exit 0' TERM INT

last_set=""
[ -f "$STATE_FILE" ] && last_set=$(cat "$STATE_FILE" 2>/dev/null || true)

while true; do
    current_set=$(fetch_channel_ids)

    if [ -n "$current_set" ]; then
        old_ifs=$IFS
        IFS=,
        for cid in $current_set; do
            buzz-cli channels join --channel "$cid" >/dev/null 2>&1 || true
        done
        IFS=$old_ifs
    fi

    if [ "$current_set" != "$last_set" ]; then
        log "channel set changed: '${last_set:-<none>}' -> '${current_set:-<none>}'"
        stop_acp
        start_acp "$current_set"
        printf '%s' "$current_set" > "$STATE_FILE"
        last_set="$current_set"
    elif [ -z "$ACP_PID" ] || ! kill -0 "$ACP_PID" 2>/dev/null; then
        log "buzz-acp not running, (re)starting"
        start_acp "$current_set"
    fi

    sleep "$POLL_INTERVAL" &
    wait $!
done
