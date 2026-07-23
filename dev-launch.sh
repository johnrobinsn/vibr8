#!/usr/bin/env bash
# Launch a dev hub that runs in parallel to a live prod hub on the same
# box. The hub is stateless; this script also starts a vibr8_node process
# (no privileged name — pick whatever via VIBR8_DEV_NODE_NAME) which
# registers with the hub like any other operator-run node.
#
# Layout:
#   * dev hub on $DEV_PORT (default 4456), data dir ~/.vibr8-dev/
#   * dev Vite on $DEV_VITE_PORT (default 5184), proxies /api+/ws to hub
#   * dev node $DEV_NODE_NAME (default "blah") on $DEV_NODE_PORT
#     (default 4459), data dir ~/.vibr8-dev-$DEV_NODE_NAME/ — connects to
#     the dev hub via loopback tunnel
#
# Usage:
#   ./dev-launch.sh              # start backend + vite + node
#   ./dev-launch.sh stop         # stop everything

set -euo pipefail

DEV_HUB_DIR="${VIBR8_DEV_HUB_DIR:-$HOME/.vibr8-dev}"
DEV_NODE_NAME="${VIBR8_DEV_NODE_NAME:-blah}"
DEV_NODE_DIR="${VIBR8_DEV_NODE_DIR:-$HOME/.vibr8-dev-$DEV_NODE_NAME}"
DEV_PORT="${VIBR8_DEV_PORT:-4456}"
DEV_NODE_PORT="${VIBR8_DEV_NODE_PORT:-4459}"
DEV_VITE_PORT="${VIBR8_DEV_VITE_PORT:-5184}"
DEV_LOG="${VIBR8_DEV_LOG:-server-dev.log}"
PROD_HUB_DIR="${VIBR8_PROD_HUB_DIR:-$HOME/.vibr8}"

cd "$(dirname "$0")"

stop() {
  # Kill anything bound to dev ports.
  for port in "$DEV_PORT" "$DEV_NODE_PORT" "$DEV_VITE_PORT"; do
    pids="$(ss -ltnp 2>/dev/null | awk -v p=":$port" '$4 ~ p {gsub(/.*pid=/,""); gsub(/,.*/,""); print}' | sort -u)"
    [ -n "$pids" ] && kill $pids 2>/dev/null || true
  done
  pkill -f "vibr8_node.*--name $DEV_NODE_NAME.*--port $DEV_NODE_PORT" 2>/dev/null || true
  echo "[dev-launch] stopped"
}

mint_node_key() {
  # Mint or rotate the node's API key directly against the dev hub's
  # NodeRegistry (writes ~/.vibr8-dev/nodes.json). The key is printed on
  # stdout once; dev-launch persists it to the node's data dir so respawns
  # reuse the same key.
  local key_file="$DEV_NODE_DIR/api-key"
  if [ -s "$key_file" ]; then
    cat "$key_file"
    return
  fi
  mkdir -p "$DEV_NODE_DIR"
  VIBR8_HUB_DATA_DIR="$DEV_HUB_DIR" uv run python - "$DEV_NODE_NAME" <<'PY' > "$key_file"
import sys
from server.node_registry import NodeRegistry
nr = NodeRegistry()
key, _ = nr.generate_api_key(name=f"{sys.argv[1]}-bootstrap")
print(key)
PY
  chmod 600 "$key_file"
  cat "$key_file"
}

start() {
  # Seed the dev hub data dir on first run (auth + ICE only).
  if [ ! -f "$DEV_HUB_DIR/users.json" ]; then
    mkdir -p "$DEV_HUB_DIR"
    [ -f "$PROD_HUB_DIR/users.json" ] && cp -v "$PROD_HUB_DIR/users.json" "$DEV_HUB_DIR/"
    [ -f "$PROD_HUB_DIR/ice-servers.json" ] && cp -v "$PROD_HUB_DIR/ice-servers.json" "$DEV_HUB_DIR/"
    openssl rand -hex 32 > "$DEV_HUB_DIR/secret.key"
    echo "[dev-launch] seeded $DEV_HUB_DIR"
  fi
  mkdir -p "$DEV_NODE_DIR"

  # Background backend (stateless hub).
  VIBR8_HUB_DATA_DIR="$DEV_HUB_DIR" \
  PORT="$DEV_PORT" \
  VIBR8_LOG_FILE="$DEV_LOG" \
    nohup uv run python -m server.main >/tmp/vibr8-dev-stdout.log 2>&1 &
  echo "[dev-launch] backend PID $! → https://localhost:$DEV_PORT (log: $DEV_LOG)"

  # Wait for the hub HTTP listener to bind so the node can register.
  for _ in $(seq 1 50); do
    if ss -ltn 2>/dev/null | grep -q ":$DEV_PORT "; then break; fi
    sleep 0.2
  done

  # Mint (or reuse) the node's API key and start the node.
  key="$(mint_node_key)"
  scheme="wss"
  [ -f "$(dirname "$0")/certs/cert.pem" ] || scheme="ws"
  # Screen-capture (x11grab, used by desktop/v1) needs an X display —
  # the node inherits $DISPLAY from this shell. If it's unset the
  # desktop tab will 500 on webrtc/offer with "no display: $DISPLAY not
  # set" and the browser will show a "Node offline" card. Warn now so
  # it's obvious before the user clicks Desktop.
  if [ -z "${DISPLAY:-}" ]; then
    echo "[dev-launch] warning: \$DISPLAY not set — desktop capture will fail. Re-run with e.g. 'DISPLAY=:0 ./dev-launch.sh' if you need the Desktop tab."
  fi
  VIBR8_NODE_DATA_DIR="$DEV_NODE_DIR" \
    nohup uv run python -m vibr8_node \
      --hub "$scheme://127.0.0.1:$DEV_PORT" \
      --api-key "$key" \
      --name "$DEV_NODE_NAME" \
      --port "$DEV_NODE_PORT" \
      >/tmp/vibr8-dev-node.log 2>&1 &
  echo "[dev-launch] node '$DEV_NODE_NAME' PID $! → :$DEV_NODE_PORT (data: $DEV_NODE_DIR)"

  # Background Vite, proxying to the dev backend.
  (cd web && VITE_BACKEND_PORT="$DEV_PORT" nohup bun run dev --port "$DEV_VITE_PORT" >/tmp/vibr8-dev-vite.log 2>&1 &
   echo "[dev-launch] vite PID $! → https://localhost:$DEV_VITE_PORT")
}

case "${1:-start}" in
  start) start ;;
  stop)  stop ;;
  *) echo "usage: $0 [start|stop]"; exit 2 ;;
esac
