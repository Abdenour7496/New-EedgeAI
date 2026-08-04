#!/bin/sh
set -eu

missing=""
for var in BUZZ_PRIVATE_KEY OPENAI_COMPAT_API_KEY; do
    eval "val=\${$var:-}"
    if [ -z "$val" ]; then
        missing="$missing $var"
    fi
done

if [ -n "$missing" ]; then
    echo "buzz-bridge: missing required env var(s):$missing" >&2
    echo "See .env.example (Buzz Knowledge Bridge section) for how to generate them." >&2
    exit 1
fi

export BUZZ_ACP_AGENT_COMMAND="${BUZZ_ACP_AGENT_COMMAND:-/usr/local/bin/reply_adapter.py}"
export BUZZ_ACP_AGENT_ARGS="${BUZZ_ACP_AGENT_ARGS:-}"

# channel_supervisor.sh computes --channels itself (every channel this
# identity can see) and relaunches buzz-acp only when that set changes — see
# its header comment for why a static BUZZ_ACP_CHANNELS list is no longer
# needed.
exec /usr/local/bin/channel_supervisor.sh
