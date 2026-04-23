#!/bin/bash
set -e

# ── vibr8 hub Docker entrypoint ──────────────────────────────────────────────
#
# Commands:
#   serve   (default) — Run first-run setup if needed, start virtual display, start server
#   setup   — Run first-run wizard only
#   warmup  — Pre-download ML models into cache volume
#   shell   — Drop to bash

VIBR8_DIR="$HOME/.vibr8"
USERS_FILE="$VIBR8_DIR/users.json"
SECRET_FILE="$VIBR8_DIR/secret.key"
RING0_FILE="$VIBR8_DIR/ring0.json"
CERTS_DIR="/app/certs"

# Ensure .claude.json exists (Claude Code needs it)
if [ ! -f "$HOME/.claude.json" ] && [ -f "$HOME/.claude/.credentials.json" ]; then
    echo '{}' > "$HOME/.claude.json"
fi

# ── First-run setup ─────────────────────────────────────────────────────────

_generate_password() {
    python3 -c "import secrets; print(secrets.token_urlsafe(16))"
}

_hash_password() {
    python3 -c "
import bcrypt, sys
pw = sys.argv[1].encode()
print(bcrypt.hashpw(pw, bcrypt.gensalt()).decode())
" "$1"
}

_create_user() {
    local username="$1"
    local password="$2"
    local hashed
    hashed="$(_hash_password "$password")"

    mkdir -p "$VIBR8_DIR"
    cat > "$USERS_FILE" <<EOF
{"users": {"$username": "$hashed"}}
EOF
    echo "[hub] Created admin user: $username"
}

_first_run_setup() {
    if [ -f "$USERS_FILE" ]; then
        echo "[hub] Users file exists, skipping first-run setup."
        return
    fi

    echo "[hub] ── First-run setup ──────────────────────────────"

    # Mode 1: Explicit credentials via env vars
    if [ -n "${VIBR8_ADMIN_USER:-}" ] && [ -n "${VIBR8_ADMIN_PASSWORD:-}" ]; then
        _create_user "$VIBR8_ADMIN_USER" "$VIBR8_ADMIN_PASSWORD"

    # Mode 2: Accept all defaults — auto-generate everything
    elif [ "${VIBR8_DEFAULTS:-}" = "1" ]; then
        local password
        password="$(_generate_password)"
        _create_user "admin" "$password"
        echo ""
        echo "  Admin credentials (save these!):"
        echo ""
        echo "    Username: admin"
        echo "    Password: $password"
        echo ""

    # Mode 3: Interactive prompts (TTY attached)
    elif [ -t 0 ]; then
        local username password password2
        echo ""
        read -rp "  Admin username [admin]: " username
        username="${username:-admin}"

        while true; do
            read -rsp "  Admin password: " password; echo
            [ -n "$password" ] || { echo "  Password cannot be empty."; continue; }
            read -rsp "  Confirm password: " password2; echo
            [ "$password" = "$password2" ] && break
            echo "  Passwords don't match. Try again."
        done

        _create_user "$username" "$password"

    # No TTY, no env vars — auto-generate (same as --defaults)
    else
        local password
        password="$(_generate_password)"
        _create_user "admin" "$password"
        echo ""
        echo "  Admin credentials (save these!):"
        echo ""
        echo "    Username: admin"
        echo "    Password: $password"
        echo ""
        echo "  (View again with: vibr8-hub logs)"
        echo ""
    fi

    # ── Generate signing secret ──────────────────────────────────────
    if [ ! -f "$SECRET_FILE" ]; then
        python3 -c "import secrets; print(secrets.token_hex(32))" > "$SECRET_FILE"
        chmod 600 "$SECRET_FILE"
        echo "[hub] Generated signing secret."
    fi

    # ── Generate self-signed SSL certs ───────────────────────────────
    if [ "${VIBR8_NO_SSL:-}" = "1" ]; then
        echo "[hub] SSL disabled (--external-ssl mode)."
        echo "[hub] WARNING: WebRTC (voice) requires HTTPS. Make sure your reverse"
        echo "[hub]          proxy terminates SSL, or remove --external-ssl to use"
        echo "[hub]          the built-in self-signed certificate."
    elif [ ! -f "$CERTS_DIR/cert.pem" ] || [ ! -f "$CERTS_DIR/key.pem" ]; then
        mkdir -p "$CERTS_DIR"
        openssl req -x509 -newkey rsa:2048 -nodes \
            -keyout "$CERTS_DIR/key.pem" \
            -out "$CERTS_DIR/cert.pem" \
            -days 365 \
            -subj "/CN=vibr8-hub" \
            2>/dev/null
        echo "[hub] Generated self-signed SSL certificate."
    fi

    # ── Initialize Ring0 config ──────────────────────────────────────
    if [ ! -f "$RING0_FILE" ]; then
        local ring0_sid="ring0-hub-$(head -c8 /dev/urandom | xxd -p)"
        cat > "$RING0_FILE" <<EOF
{"enabled": true, "sessionId": "$ring0_sid"}
EOF
        echo "[hub] Initialized Ring0 (session: $ring0_sid)."
    fi

    # ── Copy Ring0 event config example if needed ────────────────────
    if [ ! -f "$VIBR8_DIR/ring0-events.json5" ] && [ -f /app/ring0-events.example.json5 ]; then
        cp /app/ring0-events.example.json5 "$VIBR8_DIR/ring0-events.json5"
        echo "[hub] Copied Ring0 event config example."
    fi

    echo "[hub] ── First-run setup complete ─────────────────────"
    echo ""
}

# ── Virtual display ──────────────────────────────────────────────────────────

_start_virtual_display() {
    if [ -z "${VIRTUAL_DISPLAY:-}" ]; then
        return
    fi

    # Check if Xvfb is available (not present in lite image)
    if ! command -v Xvfb >/dev/null 2>&1; then
        echo "[hub] Warning: VIRTUAL_DISPLAY set but Xvfb not available (lite image?)."
        return
    fi

    # Normalize: treat bare "1" or "true" as default resolution
    case "$VIRTUAL_DISPLAY" in
        1|true|yes) VIRTUAL_DISPLAY="1920x1080" ;;
    esac

    echo "[hub] Starting virtual display ${VIRTUAL_DISPLAY} ..."

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
    if command -v dbus-launch >/dev/null 2>&1; then
        eval "$(dbus-launch --sh-syntax)"
        export DBUS_SESSION_BUS_ADDRESS
    fi
    startxfce4 &

    # Give XFCE a moment to initialize
    sleep 1
    echo "[hub] Virtual display ready (DISPLAY=$DISPLAY)"
}

# ── Commands ─────────────────────────────────────────────────────────────────

cmd_serve() {
    _first_run_setup
    _start_virtual_display

    echo "[hub] Starting vibr8 hub server on port ${PORT:-3456} ..."

    # Serve loop: restart on exit code 75 (server requested restart)
    while true; do
        python -m server.main
        rc=$?
        [ $rc -ne 75 ] && break
        echo "[hub] Restart requested, relaunching..."
        sleep 1
    done
}

cmd_setup() {
    _first_run_setup
    echo "[hub] Setup complete. Start with: vibr8-hub start"
}

cmd_warmup() {
    echo "[hub] Pre-downloading ML models ..."
    python3 -c "
from server.stt import AsyncSTT
AsyncSTT.preload_shared_resources()
print('[hub] STT models ready (Whisper, Silero VAD, EOU)')
"
    echo "[hub] Model warmup complete."
}

# ── Main ─────────────────────────────────────────────────────────────────────

case "${1:-serve}" in
    serve)  cmd_serve ;;
    setup)  cmd_setup ;;
    warmup) cmd_warmup ;;
    shell)  exec /bin/bash ;;
    *)      exec "$@" ;;
esac
