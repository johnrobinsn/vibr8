#!/bin/bash
set -e

# Translate environment variables to vibr8-node CLI arguments.
# This allows the container to be configured entirely via env vars
# (from config.env) while also supporting extra CLI args via docker run.

ARGS=()

# Ensure .claude.json exists (Claude Code needs it)
if [ ! -f "$HOME/.claude.json" ] && [ -f "$HOME/.claude/.credentials.json" ]; then
    echo '{}' > "$HOME/.claude.json"
fi

# Rewrite localhost URLs to host.docker.internal so the container can reach the host
if [ -n "$HUB_URL" ]; then
    HUB_URL="${HUB_URL//localhost/host.docker.internal}"
    HUB_URL="${HUB_URL//127.0.0.1/host.docker.internal}"
fi

[ -n "$HUB_URL" ]   && ARGS+=(--hub "$HUB_URL")
[ -n "$API_KEY" ]   && ARGS+=(--api-key "$API_KEY")
[ -n "$NODE_NAME" ] && ARGS+=(--name "$NODE_NAME")
[ -n "$NODE_PORT" ] && ARGS+=(--port "$NODE_PORT")
[ -n "$WORK_DIR" ]  && ARGS+=(--work-dir "$WORK_DIR")

exec python -m vibr8_node "${ARGS[@]}" "$@"
