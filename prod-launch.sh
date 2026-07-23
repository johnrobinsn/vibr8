#!/usr/bin/env bash
# Launch a prod hub + a local vibr8_node registered against it. The
# hub is stateless (host-as-node architecture — see
# docs/hub-node-contract-v1.md); this script starts the hub and one
# node process on the same box so a fresh install has a working local
# node out of the box. Remote nodes register the same way via
# install-node.sh.
#
# Layout:
#   * prod hub on $PROD_PORT (default 3456), data dir ~/.vibr8/,
#     serves the built frontend from web/dist (NODE_ENV=production)
#   * prod node $PROD_NODE_NAME (default "host") on $PROD_NODE_PORT
#     (default 3459), data dir ~/.vibr8-node/$PROD_NODE_NAME/ —
#     connects to the hub via loopback tunnel
#
# Prod does not run Vite — the hub serves web/dist directly. You must
# `make build` (or `cd web && bun run build`) before starting prod so
# web/dist exists.
#
# Usage:
#   ./prod-launch.sh              # start hub + node
#   ./prod-launch.sh stop         # stop everything

set -euo pipefail

PROD_HUB_DIR="${VIBR8_HUB_DATA_DIR:-$HOME/.vibr8}"
PROD_NODE_NAME="${VIBR8_PROD_NODE_NAME:-host}"
PROD_NODE_DIR="${VIBR8_NODE_DATA_DIR:-$HOME/.vibr8-node/$PROD_NODE_NAME}"
PROD_PORT="${VIBR8_PROD_PORT:-3456}"
PROD_NODE_PORT="${VIBR8_PROD_NODE_PORT:-3459}"
PROD_LOG="${VIBR8_LOG_FILE:-server.log}"

cd "$(dirname "$0")"

stop() {
  # Kill anything bound to prod ports.
  for port in "$PROD_PORT" "$PROD_NODE_PORT"; do
    pids="$(ss -ltnp 2>/dev/null | awk -v p=":$port" '$4 ~ p {gsub(/.*pid=/,""); gsub(/,.*/,""); print}' | sort -u)"
    [ -n "$pids" ] && kill $pids 2>/dev/null || true
  done
  pkill -f "vibr8_node.*--name $PROD_NODE_NAME.*--port $PROD_NODE_PORT" 2>/dev/null || true
  echo "[prod-launch] stopped"
}

mint_node_key() {
  # Mint or rotate the node's API key directly against the prod hub's
  # NodeRegistry (writes ~/.vibr8/nodes.json). The key is printed on
  # stdout once; prod-launch persists it to the node's data dir so
  # respawns reuse the same key.
  local key_file="$PROD_NODE_DIR/api-key"
  if [ -s "$key_file" ]; then
    cat "$key_file"
    return
  fi
  mkdir -p "$PROD_NODE_DIR"
  VIBR8_HUB_DATA_DIR="$PROD_HUB_DIR" uv run python - "$PROD_NODE_NAME" <<'PY' > "$key_file"
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
  # Bail early if the frontend isn't built — prod serves web/dist
  # statically, unlike dev which uses Vite HMR.
  if [ ! -f web/dist/index.html ]; then
    echo "[prod-launch] error: web/dist/index.html missing — run 'make build' first" >&2
    exit 1
  fi

  # Seed the prod hub data dir on first run (auth signing key only —
  # users.json and ice-servers.json are operator-provisioned).
  if [ ! -f "$PROD_HUB_DIR/secret.key" ]; then
    mkdir -p "$PROD_HUB_DIR"
    openssl rand -hex 32 > "$PROD_HUB_DIR/secret.key"
    echo "[prod-launch] seeded $PROD_HUB_DIR/secret.key"
  fi
  mkdir -p "$PROD_NODE_DIR"

  # Background backend (stateless hub, serves built web/dist as SPA).
  NODE_ENV=production \
  VIBR8_HUB_DATA_DIR="$PROD_HUB_DIR" \
  PORT="$PROD_PORT" \
  VIBR8_LOG_FILE="$PROD_LOG" \
    nohup uv run python -m server.main >/tmp/vibr8-prod-stdout.log 2>&1 &
  echo "[prod-launch] backend PID $! → https://localhost:$PROD_PORT (log: $PROD_LOG)"

  # Wait for the hub HTTP listener to bind so the node can register.
  for _ in $(seq 1 50); do
    if ss -ltn 2>/dev/null | grep -q ":$PROD_PORT "; then break; fi
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
  # it's obvious before someone clicks Desktop.
  if [ -z "${DISPLAY:-}" ]; then
    echo "[prod-launch] warning: \$DISPLAY not set — desktop capture will fail. Re-run with e.g. 'DISPLAY=:0 ./prod-launch.sh' if the local node should serve the Desktop tab."
  fi
  VIBR8_NODE_DATA_DIR="$PROD_NODE_DIR" \
    nohup uv run python -m vibr8_node \
      --hub "$scheme://127.0.0.1:$PROD_PORT" \
      --api-key "$key" \
      --name "$PROD_NODE_NAME" \
      --port "$PROD_NODE_PORT" \
      >/tmp/vibr8-prod-node.log 2>&1 &
  echo "[prod-launch] node '$PROD_NODE_NAME' PID $! → :$PROD_NODE_PORT (data: $PROD_NODE_DIR)"
}

case "${1:-start}" in
  start) start ;;
  stop)  stop ;;
  *) echo "usage: $0 [start|stop]"; exit 2 ;;
esac
