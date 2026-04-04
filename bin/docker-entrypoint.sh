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

# ── Virtual display (opt-in via VIRTUAL_DISPLAY env var) ────────────────────
# When set, starts Xvfb + XFCE desktop before the node agent.
# Value is the display dimensions, e.g. "1920x1080" (default if just "1").
if [ -n "${VIRTUAL_DISPLAY:-}" ]; then
    # Normalize: treat bare "1" or "true" as default resolution
    case "$VIRTUAL_DISPLAY" in
        1|true|yes) VIRTUAL_DISPLAY="1920x1080" ;;
    esac

    echo "[entrypoint] Starting virtual display ${VIRTUAL_DISPLAY} ..."

    # Clean up stale lock files from previous container runs
    rm -f /tmp/.X99-lock

    # Start Xvfb
    Xvfb :99 -screen 0 "${VIRTUAL_DISPLAY}x24" -ac -nolisten tcp &
    export DISPLAY=:99

    # Wait for X to be ready
    for _i in $(seq 1 20); do
        xdpyinfo -display :99 >/dev/null 2>&1 && break
        sleep 0.2
    done

    # Start XFCE desktop (taskbar, window snapping, etc.)
    # Run dbus session first (XFCE needs it)
    if command -v dbus-launch >/dev/null 2>&1; then
        eval "$(dbus-launch --sh-syntax)"
        export DBUS_SESSION_BUS_ADDRESS
    fi
    startxfce4 &

    # Give XFCE a moment to initialize
    sleep 1
    echo "[entrypoint] Virtual display ready (DISPLAY=$DISPLAY)"
fi

[ -n "$HUB_URL" ]   && ARGS+=(--hub "$HUB_URL")
[ -n "$API_KEY" ]   && ARGS+=(--api-key "$API_KEY")
[ -n "$NODE_NAME" ] && ARGS+=(--name "$NODE_NAME")
[ -n "$NODE_PORT" ] && ARGS+=(--port "$NODE_PORT")
[ -n "$WORK_DIR" ]  && ARGS+=(--work-dir "$WORK_DIR")

exec python -m vibr8_node "${ARGS[@]}" "$@"
