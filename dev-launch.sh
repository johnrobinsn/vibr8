#!/usr/bin/env bash
# Launch a dev hub that runs in parallel to a live prod hub on the same
# box. Uses VIBR8_HUB_DATA_DIR + VIBR8_NODE_DATA_DIR for full disk
# isolation, alt ports for the API and Vite dev server, and a separate
# log file.
#
# Usage:
#   ./dev-launch.sh              # start backend + vite, background, log to /tmp
#   ./dev-launch.sh stop         # stop backend + vite + self-node
#
# Defaults match an existing prod hub on 3456/5174/~/.vibr8.

set -euo pipefail

DEV_HUB_DIR="${VIBR8_DEV_HUB_DIR:-$HOME/.vibr8-dev}"
DEV_SELF_DIR="${VIBR8_DEV_SELF_DIR:-$HOME/.vibr8-dev-self}"
DEV_PORT="${VIBR8_DEV_PORT:-4456}"
DEV_SELF_PORT="${VIBR8_DEV_SELF_PORT:-4459}"
DEV_VITE_PORT="${VIBR8_DEV_VITE_PORT:-5184}"
DEV_LOG="${VIBR8_DEV_LOG:-server-dev.log}"
PROD_HUB_DIR="${VIBR8_PROD_HUB_DIR:-$HOME/.vibr8}"

stop() {
  # Kill anything bound to dev ports.
  for port in "$DEV_PORT" "$DEV_SELF_PORT" "$DEV_VITE_PORT"; do
    pids="$(ss -ltnp 2>/dev/null | awk -v p=":$port" '$4 ~ p {gsub(/.*pid=/,""); gsub(/,.*/,""); print}' | sort -u)"
    [ -n "$pids" ] && kill $pids 2>/dev/null || true
  done
  pkill -f "vibr8_node.*--port $DEV_SELF_PORT" 2>/dev/null || true
  echo "[dev-launch] stopped"
}

start() {
  # Seed the dev data dir from prod if it doesn't exist yet (auth +
  # ICE only — sessions, ring0, artifacts stay fresh per instance).
  if [ ! -f "$DEV_HUB_DIR/users.json" ]; then
    mkdir -p "$DEV_HUB_DIR" "$DEV_SELF_DIR"
    [ -f "$PROD_HUB_DIR/users.json" ] && cp -v "$PROD_HUB_DIR/users.json" "$DEV_HUB_DIR/"
    [ -f "$PROD_HUB_DIR/ice-servers.json" ] && cp -v "$PROD_HUB_DIR/ice-servers.json" "$DEV_HUB_DIR/"
    openssl rand -hex 32 > "$DEV_HUB_DIR/secret.key"
    echo "[dev-launch] seeded $DEV_HUB_DIR"
  fi

  cd "$(dirname "$0")"

  # Background backend.
  VIBR8_HUB_DATA_DIR="$DEV_HUB_DIR" \
  VIBR8_SELF_NODE_DATA_DIR="$DEV_SELF_DIR" \
  PORT="$DEV_PORT" \
  VIBR8_SELF_NODE_PORT="$DEV_SELF_PORT" \
  VIBR8_LOG_FILE="$DEV_LOG" \
    nohup uv run python -m server.main >/tmp/vibr8-dev-stdout.log 2>&1 &
  echo "[dev-launch] backend PID $! → https://localhost:$DEV_PORT (log: $DEV_LOG)"

  # Background Vite, proxying to the dev backend.
  (cd web && VITE_BACKEND_PORT="$DEV_PORT" nohup bun run dev --port "$DEV_VITE_PORT" >/tmp/vibr8-dev-vite.log 2>&1 &
   echo "[dev-launch] vite PID $! → https://localhost:$DEV_VITE_PORT")
}

case "${1:-start}" in
  start) start ;;
  stop)  stop ;;
  *) echo "usage: $0 [start|stop]"; exit 2 ;;
esac
