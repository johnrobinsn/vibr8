#!/usr/bin/env bash
# Launch a dev hub that runs in parallel to a live prod hub on the same
# box. The hub is fully stateless (no self-node spawn); the host node is
# a separate vibr8_node process this script also starts and registers.
#
# Layout:
#   * dev hub on $DEV_PORT (default 4456), data dir ~/.vibr8-dev/
#   * dev Vite on $DEV_VITE_PORT (default 5184), proxies /api+/ws to hub
#   * host node on $DEV_HOST_NODE_PORT (default 4459), data dir
#     ~/.vibr8-host-node/ — connects to the dev hub via loopback tunnel
#
# Usage:
#   ./dev-launch.sh              # start backend + vite + host node
#   ./dev-launch.sh stop         # stop everything

set -euo pipefail

DEV_HUB_DIR="${VIBR8_DEV_HUB_DIR:-$HOME/.vibr8-dev}"
DEV_HOST_NODE_DIR="${VIBR8_DEV_HOST_NODE_DIR:-$HOME/.vibr8-dev-host-node}"
DEV_PORT="${VIBR8_DEV_PORT:-4456}"
DEV_HOST_NODE_PORT="${VIBR8_DEV_HOST_NODE_PORT:-4459}"
DEV_VITE_PORT="${VIBR8_DEV_VITE_PORT:-5184}"
DEV_LOG="${VIBR8_DEV_LOG:-server-dev.log}"
DEV_HOST_NODE_NAME="${VIBR8_DEV_HOST_NODE_NAME:-host}"
PROD_HUB_DIR="${VIBR8_PROD_HUB_DIR:-$HOME/.vibr8}"

cd "$(dirname "$0")"

stop() {
  # Kill anything bound to dev ports.
  for port in "$DEV_PORT" "$DEV_HOST_NODE_PORT" "$DEV_VITE_PORT"; do
    pids="$(ss -ltnp 2>/dev/null | awk -v p=":$port" '$4 ~ p {gsub(/.*pid=/,""); gsub(/,.*/,""); print}' | sort -u)"
    [ -n "$pids" ] && kill $pids 2>/dev/null || true
  done
  pkill -f "vibr8_node.*--name $DEV_HOST_NODE_NAME.*--port $DEV_HOST_NODE_PORT" 2>/dev/null || true
  echo "[dev-launch] stopped"
}

mint_host_node_key() {
  # Mint or rotate the host node's API key directly against the dev
  # hub's NodeRegistry (writes ~/.vibr8-dev/nodes.json). The key is
  # printed on stdout once; dev-launch persists it to the host-node
  # data dir so respawns reuse the same key.
  local key_file="$DEV_HOST_NODE_DIR/api-key"
  if [ -s "$key_file" ]; then
    cat "$key_file"
    return
  fi
  mkdir -p "$DEV_HOST_NODE_DIR"
  VIBR8_HUB_DATA_DIR="$DEV_HUB_DIR" uv run python - "$DEV_HOST_NODE_NAME" <<'PY' > "$key_file"
import sys
from server.node_registry import NodeRegistry
nr = NodeRegistry()
key, _ = nr.generate_api_key(name=f"{sys.argv[1]}-node-bootstrap")
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
  mkdir -p "$DEV_HOST_NODE_DIR"

  # Background backend (stateless hub).
  VIBR8_HUB_DATA_DIR="$DEV_HUB_DIR" \
  PORT="$DEV_PORT" \
  VIBR8_LOG_FILE="$DEV_LOG" \
    nohup uv run python -m server.main >/tmp/vibr8-dev-stdout.log 2>&1 &
  echo "[dev-launch] backend PID $! → https://localhost:$DEV_PORT (log: $DEV_LOG)"

  # Wait for the hub HTTP listener to bind so the host node can register.
  for _ in $(seq 1 50); do
    if ss -ltn 2>/dev/null | grep -q ":$DEV_PORT "; then break; fi
    sleep 0.2
  done

  # Mint (or reuse) the host node's API key and start the host node.
  key="$(mint_host_node_key)"
  scheme="wss"
  [ -f "$(dirname "$0")/certs/cert.pem" ] || scheme="ws"
  VIBR8_NODE_DATA_DIR="$DEV_HOST_NODE_DIR" \
    nohup uv run python -m vibr8_node \
      --hub "$scheme://127.0.0.1:$DEV_PORT" \
      --api-key "$key" \
      --name "$DEV_HOST_NODE_NAME" \
      --port "$DEV_HOST_NODE_PORT" \
      >/tmp/vibr8-dev-host-node.log 2>&1 &
  echo "[dev-launch] host-node PID $! → :$DEV_HOST_NODE_PORT (data: $DEV_HOST_NODE_DIR)"

  # Background Vite, proxying to the dev backend.
  (cd web && VITE_BACKEND_PORT="$DEV_PORT" nohup bun run dev --port "$DEV_VITE_PORT" >/tmp/vibr8-dev-vite.log 2>&1 &
   echo "[dev-launch] vite PID $! → https://localhost:$DEV_VITE_PORT")
}

case "${1:-start}" in
  start) start ;;
  stop)  stop ;;
  *) echo "usage: $0 [start|stop]"; exit 2 ;;
esac
