#!/usr/bin/env bash
set -euo pipefail

# ── vibr8 node installer ────────────────────────────────────────────────────
#
# Universal installer for vibr8 node agent. Works on:
#   - macOS (bare metal)
#   - Linux (bare metal)
#   - Docker containers (called from Dockerfile)
#
# Usage:
#   # Interactive install (macOS / Linux):
#   curl -fsSL https://vibr8.ringzero.ai/install-node | bash
#
#   # From local repo:
#   ./install-node.sh
#
#   # Non-interactive (Docker / CI):
#   ./install-node.sh --non-interactive --no-service --source ./
#
#   # Add desktop streaming deps to existing install:
#   ./install-node.sh --desktop-only
#
# Flags:
#   --prefix PATH         Install directory (default: ~/.vibr8-node)
#   --source PATH         Source directory (local repo). If omitted, downloads release.
#   --no-desktop          Skip desktop streaming deps (aiortc, av, etc.)
#   --desktop-only        Only install desktop deps into existing venv (for layered Docker)
#   --no-service          Skip launchd/systemd service installation
#   --non-interactive     Skip setup wizard (for Docker / CI)
#   --system-python       Use system Python directly (no uv, no venv). For containers.
#   --hub URL             Hub URL (non-interactive)
#   --api-key KEY         API key (non-interactive)
#   --name NAME           Node name (non-interactive)

# ── Defaults ─────────────────────────────────────────────────────────────────

PREFIX="${VIBR8_NODE_PREFIX:-$HOME/.vibr8-node}"
SOURCE_DIR=""
INSTALL_DESKTOP=true
DESKTOP_ONLY=false
INSTALL_SERVICE=true
INTERACTIVE=true
SYSTEM_PYTHON=false
HUB_URL=""
API_KEY=""
NODE_NAME=""
REPO_ROOT="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo "")"

# ── Parse flags ──────────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)           PREFIX="$2"; shift 2 ;;
        --source)           SOURCE_DIR="$2"; shift 2 ;;
        --no-desktop)       INSTALL_DESKTOP=false; shift ;;
        --desktop-only)     DESKTOP_ONLY=true; shift ;;
        --no-service)       INSTALL_SERVICE=false; shift ;;
        --non-interactive)  INTERACTIVE=false; shift ;;
        --system-python)    SYSTEM_PYTHON=true; shift ;;
        --hub)              HUB_URL="$2"; shift 2 ;;
        --api-key)          API_KEY="$2"; shift 2 ;;
        --name)             NODE_NAME="$2"; shift 2 ;;
        -h|--help)          head -35 "$0" | tail -30; exit 0 ;;
        *)                  echo "Unknown flag: $1" >&2; exit 1 ;;
    esac
done

# ── Helpers ──────────────────────────────────────────────────────────────────

_info()  { echo "==> $*"; }
_warn()  { echo "Warning: $*" >&2; }
_die()   { echo "Error: $*" >&2; exit 1; }

_is_docker() { [ -f /.dockerenv ] || grep -q docker /proc/1/cgroup 2>/dev/null; }
_is_macos()  { [ "$(uname -s)" = "Darwin" ]; }
_is_linux()  { [ "$(uname -s)" = "Linux" ]; }

_cmd_exists() { command -v "$1" >/dev/null 2>&1; }

# ── Detect environment ──────────────────────────────────────────────────────

OS="$(uname -s)"
ARCH="$(uname -m)"
IN_DOCKER=false
_is_docker && IN_DOCKER=true

# Docker containers: skip service install, default to non-interactive
if $IN_DOCKER; then
    INSTALL_SERVICE=false
    if [ -z "${VIBR8_INTERACTIVE:-}" ]; then
        INTERACTIVE=false
    fi
fi

_info "vibr8 node installer"
_info "Platform: $OS/$ARCH$(${IN_DOCKER} && echo ' (Docker)' || true)"

# ── Desktop-only mode (add deps to existing install) ────────────────────────

if $DESKTOP_ONLY; then
    _info "Installing desktop streaming dependencies into existing install..."

    if $SYSTEM_PYTHON; then
        PIP_CMD="pip install"
    elif [ -f "$PREFIX/venv/bin/pip" ]; then
        PIP_CMD="$PREFIX/venv/bin/pip install"
    elif _cmd_exists uv && [ -d "$PREFIX/venv" ]; then
        PIP_CMD="uv pip install --python $PREFIX/venv/bin/python"
    else
        _die "No existing install found at $PREFIX. Run full install first."
    fi

    REQ_FILE=""
    if [ -n "$SOURCE_DIR" ] && [ -f "$SOURCE_DIR/requirements-node-desktop.txt" ]; then
        REQ_FILE="$SOURCE_DIR/requirements-node-desktop.txt"
    elif [ -f "$PREFIX/lib/requirements-node-desktop.txt" ]; then
        REQ_FILE="$PREFIX/lib/requirements-node-desktop.txt"
    elif [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/requirements-node-desktop.txt" ]; then
        REQ_FILE="$REPO_ROOT/requirements-node-desktop.txt"
    else
        _die "Cannot find requirements-node-desktop.txt"
    fi

    $PIP_CMD -r "$REQ_FILE"
    _info "Desktop dependencies installed."
    exit 0
fi

# ── Step 1: Ensure Python ────────────────────────────────────────────────────

if $SYSTEM_PYTHON; then
    PYTHON="python3"
    $PYTHON --version >/dev/null 2>&1 || _die "python3 not found"
    _info "Using system Python: $($PYTHON --version)"
else
    # Install uv if not present
    if ! _cmd_exists uv; then
        _info "Installing uv (Python package manager)..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        # Source the env so uv is on PATH
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
        _cmd_exists uv || _die "uv installation failed"
    fi
    _info "Using uv: $(uv --version)"
fi

# ── Step 2: Create install directory ─────────────────────────────────────────

mkdir -p "$PREFIX"/{lib,bin,logs}

# ── Step 3: Copy node code ───────────────────────────────────────────────────

# Determine source — local repo or download
if [ -z "$SOURCE_DIR" ]; then
    # Check if we're running from within the repo
    if [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/vibr8_node/__main__.py" ]; then
        SOURCE_DIR="$REPO_ROOT"
    else
        _die "No --source specified and not running from vibr8 repo. Pass --source /path/to/vibr8"
    fi
fi

[ -f "$SOURCE_DIR/vibr8_node/__main__.py" ] || _die "Source directory missing vibr8_node/. Is $SOURCE_DIR the vibr8 repo root?"

_info "Copying node code from $SOURCE_DIR ..."

# Node agent modules
rm -rf "$PREFIX/lib/vibr8_node"
cp -r "$SOURCE_DIR/vibr8_node" "$PREFIX/lib/vibr8_node"

# Server modules (only what the node needs)
rm -rf "$PREFIX/lib/server"
mkdir -p "$PREFIX/lib/server"

NODE_SERVER_MODULES=(
    __init__.py
    cli_launcher.py
    ws_bridge.py
    session_store.py
    session_types.py
    ring0.py
    ring0_mcp.py
    ring0_events.py
    routes.py
    session_names.py
    screen_capture.py
    input_injector.py
    video_track.py
    audio_track.py
    auth.py
    env_manager.py
    git_utils.py
    voice_profiles.py
    voice_logger.py
    usage_limits.py
    worktree_tracker.py
    terminal.py
)

for mod in "${NODE_SERVER_MODULES[@]}"; do
    if [ -f "$SOURCE_DIR/server/$mod" ]; then
        cp "$SOURCE_DIR/server/$mod" "$PREFIX/lib/server/$mod"
    fi
done

# Copy requirements files
cp "$SOURCE_DIR/requirements-node.txt" "$PREFIX/lib/"
cp "$SOURCE_DIR/requirements-node-desktop.txt" "$PREFIX/lib/" 2>/dev/null || true

# Copy pyproject.toml (needed for package metadata)
cp "$SOURCE_DIR/pyproject.toml" "$PREFIX/lib/"

# ── Step 4: Create venv + install deps ───────────────────────────────────────

if $SYSTEM_PYTHON; then
    _info "Installing Python dependencies (system-wide)..."
    pip install -q -r "$PREFIX/lib/requirements-node.txt"
    if $INSTALL_DESKTOP; then
        pip install -q -r "$PREFIX/lib/requirements-node-desktop.txt" 2>/dev/null || \
            _warn "Desktop deps failed to install (aiortc/av may need build tools)"
    fi
else
    if [ ! -d "$PREFIX/venv" ]; then
        _info "Creating Python virtual environment..."
        uv venv "$PREFIX/venv" --python 3.12 2>/dev/null || \
        uv venv "$PREFIX/venv" --python 3.11 2>/dev/null || \
        uv venv "$PREFIX/venv"
    fi

    _info "Installing Python dependencies..."
    uv pip install --python "$PREFIX/venv/bin/python" -r "$PREFIX/lib/requirements-node.txt"

    if $INSTALL_DESKTOP; then
        _info "Installing desktop streaming dependencies..."
        uv pip install --python "$PREFIX/venv/bin/python" -r "$PREFIX/lib/requirements-node-desktop.txt" 2>/dev/null || \
            _warn "Desktop deps failed (aiortc/av may need build tools). Desktop streaming disabled."
    fi
fi

# ── Step 5: Create CLI wrapper ───────────────────────────────────────────────

_info "Creating vibr8-node CLI wrapper..."

cat > "$PREFIX/bin/vibr8-node" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail

PREFIX="$(cd "$(dirname "$0")/.." && pwd)"

# Activate venv if it exists
if [ -d "$PREFIX/venv" ]; then
    export PATH="$PREFIX/venv/bin:$PATH"
    export VIRTUAL_ENV="$PREFIX/venv"
fi

export PYTHONPATH="$PREFIX/lib${PYTHONPATH:+:$PYTHONPATH}"

case "${1:-}" in
    setup)
        shift
        exec python3 -m vibr8_node.setup "$@"
        ;;
    run)
        shift
        CONFIG="$HOME/.vibr8-node/config.json"
        if [ -f "$CONFIG" ]; then
            exec python3 -m vibr8_node --config "$CONFIG" "$@"
        else
            echo "No config found. Run 'vibr8-node setup' first." >&2
            exit 1
        fi
        ;;
    update)
        shift
        echo "==> Updating vibr8 node..."
        if [ -n "${VIBR8_REPO:-}" ] && [ -d "$VIBR8_REPO" ]; then
            exec bash "$VIBR8_REPO/install-node.sh" --prefix "$PREFIX" --source "$VIBR8_REPO" --no-service --non-interactive
        else
            echo "Set VIBR8_REPO to the vibr8 repo path, or re-run the installer." >&2
            exit 1
        fi
        ;;
    stop)
        PID_FILE="$HOME/.vibr8-node/node.pid"
        if [ -f "$PID_FILE" ]; then
            kill "$(cat "$PID_FILE")" 2>/dev/null && echo "Stopped." || echo "Not running."
            rm -f "$PID_FILE"
        else
            echo "No PID file found. Node may not be running." >&2
        fi
        ;;
    status)
        PID_FILE="$HOME/.vibr8-node/node.pid"
        CONFIG="$HOME/.vibr8-node/config.json"
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "vibr8 node is running (PID $(cat "$PID_FILE"))"
        else
            echo "vibr8 node is not running"
        fi
        [ -f "$CONFIG" ] && python3 -c "
import json
c = json.load(open('$CONFIG'))
print(f\"  Hub:  {c.get('hub_url', '?')}\")
print(f\"  Name: {c.get('name', '?')}\")
"
        ;;
    version)
        echo "vibr8-node (installed at $PREFIX)"
        python3 --version
        ;;
    help|-h|--help|"")
        cat <<'EOF'
Usage: vibr8-node <command>

Commands:
  setup      Interactive configuration wizard
  run        Start the node agent (foreground)
  stop       Stop a running node agent
  status     Show node status and config
  update     Update to latest code (requires VIBR8_REPO)
  version    Show version info
  help       Show this help

Direct passthrough (same as 'run' with extra flags):
  vibr8-node --hub wss://... --api-key sk-node-... --name my-node
EOF
        ;;
    -*)
        # Direct passthrough: vibr8-node --hub ... --api-key ...
        exec python3 -m vibr8_node "$@"
        ;;
    *)
        echo "Unknown command: $1. Run 'vibr8-node help' for usage." >&2
        exit 1
        ;;
esac
WRAPPER

chmod +x "$PREFIX/bin/vibr8-node"

# ── Step 6: Create setup wizard module ───────────────────────────────────────

cat > "$PREFIX/lib/vibr8_node/setup.py" <<'SETUP'
"""Interactive setup wizard for vibr8 node."""

import json
import os
import socket
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".vibr8-node"
CONFIG_FILE = CONFIG_DIR / "config.json"


def main() -> None:
    print("vibr8 node setup")
    print("=" * 40)
    print()

    existing = {}
    if CONFIG_FILE.exists():
        existing = json.loads(CONFIG_FILE.read_text())
        print(f"Existing config found at {CONFIG_FILE}")
        print()

    # Hub URL
    default_hub = existing.get("hub_url", "wss://vibr8.ringzero.ai")
    hub_url = input(f"Hub URL [{default_hub}]: ").strip() or default_hub

    # API key
    default_key = existing.get("api_key", "")
    prompt = "API key"
    if default_key:
        prompt += f" [{default_key[:12]}...]"
    prompt += ": "
    api_key = input(prompt).strip() or default_key
    if not api_key:
        print("Error: API key is required. Generate one in the vibr8 web UI:", file=sys.stderr)
        http_url = hub_url.replace("wss://", "https://").replace("ws://", "http://")
        print(f"  {http_url}/#/settings/api-keys", file=sys.stderr)
        sys.exit(1)

    # Node name
    default_name = existing.get("name", socket.gethostname())
    name = input(f"Node name [{default_name}]: ").strip() or default_name

    # Work directory
    default_work = existing.get("work_dir", os.getcwd())
    work_dir = input(f"Working directory [{default_work}]: ").strip() or default_work
    work_dir = str(Path(work_dir).expanduser().resolve())

    # Save config
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = {
        "hub_url": hub_url,
        "api_key": api_key,
        "name": name,
        "work_dir": work_dir,
    }
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")

    print()
    print(f"Config saved to {CONFIG_FILE}")
    print()
    print("Start the node with:")
    print("  vibr8-node run")
    print()


if __name__ == "__main__":
    main()
SETUP

# ── Step 7: Symlink to PATH ─────────────────────────────────────────────────

LINK_DIR=""
if _is_macos; then
    LINK_DIR="/usr/local/bin"
elif [ -d "$HOME/.local/bin" ]; then
    LINK_DIR="$HOME/.local/bin"
elif [ -w "/usr/local/bin" ]; then
    LINK_DIR="/usr/local/bin"
fi

if [ -n "$LINK_DIR" ] && [ -d "$LINK_DIR" ]; then
    if [ -w "$LINK_DIR" ]; then
        ln -sf "$PREFIX/bin/vibr8-node" "$LINK_DIR/vibr8-node" 2>/dev/null || true
        _info "Linked vibr8-node to $LINK_DIR/vibr8-node"
    else
        _warn "Cannot write to $LINK_DIR. Add $PREFIX/bin to your PATH:"
        _warn "  export PATH=\"$PREFIX/bin:\$PATH\""
    fi
elif ! $IN_DOCKER; then
    mkdir -p "$HOME/.local/bin"
    ln -sf "$PREFIX/bin/vibr8-node" "$HOME/.local/bin/vibr8-node" 2>/dev/null || true
    _info "Linked vibr8-node to $HOME/.local/bin/vibr8-node"
    case "$PATH" in
        *"$HOME/.local/bin"*) ;;
        *) _warn "Add to your PATH: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
    esac
fi

# ── Step 8: Install service (launchd / systemd) ─────────────────────────────

if $INSTALL_SERVICE; then
    if _is_macos; then
        PLIST_DIR="$HOME/Library/LaunchAgents"
        PLIST_FILE="$PLIST_DIR/ai.ringzero.vibr8-node.plist"
        mkdir -p "$PLIST_DIR"

        cat > "$PLIST_FILE" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.ringzero.vibr8-node</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PREFIX/bin/vibr8-node</string>
        <string>run</string>
    </array>
    <key>RunAtLoad</key>
    <false/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>$PREFIX/logs/node.log</string>
    <key>StandardErrorPath</key>
    <string>$PREFIX/logs/node.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$PREFIX/venv/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
PLIST

        _info "Installed launchd service: $PLIST_FILE"
        _info "  Enable auto-start:  launchctl load $PLIST_FILE"
        _info "  Start now:          launchctl start ai.ringzero.vibr8-node"
        _info "  Stop:               launchctl stop ai.ringzero.vibr8-node"
        _info "  Disable auto-start: launchctl unload $PLIST_FILE"

    elif _is_linux && _cmd_exists systemctl && ! $IN_DOCKER; then
        UNIT_DIR="$HOME/.config/systemd/user"
        UNIT_FILE="$UNIT_DIR/vibr8-node.service"
        mkdir -p "$UNIT_DIR"

        cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=vibr8 Node Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PREFIX/bin/vibr8-node run
Restart=always
RestartSec=5
Environment=PATH=$PREFIX/venv/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONPATH=$PREFIX/lib

[Install]
WantedBy=default.target
UNIT

        systemctl --user daemon-reload 2>/dev/null || true
        _info "Installed systemd user service: $UNIT_FILE"
        _info "  Enable auto-start:  systemctl --user enable vibr8-node"
        _info "  Start now:          systemctl --user start vibr8-node"
        _info "  Stop:               systemctl --user stop vibr8-node"
        _info "  View logs:          journalctl --user -u vibr8-node -f"
    fi
fi

# ── Step 9: Interactive setup ────────────────────────────────────────────────

if $INTERACTIVE; then
    echo ""
    _info "Installation complete!"
    echo ""

    CONFIG_DIR_USER="$HOME/.vibr8-node"
    if [ ! -f "$CONFIG_DIR_USER/config.json" ]; then
        echo "Run setup to configure your node:"
        echo "  vibr8-node setup"
    else
        echo "Existing config found. Start with:"
        echo "  vibr8-node run"
    fi
    echo ""
else
    # Non-interactive: write config if hub/key provided
    if [ -n "$HUB_URL" ] && [ -n "$API_KEY" ]; then
        CONFIG_DIR_NI="$HOME/.vibr8-node"
        mkdir -p "$CONFIG_DIR_NI"
        cat > "$CONFIG_DIR_NI/config.json" <<CONF
{
  "hub_url": "$HUB_URL",
  "api_key": "$API_KEY",
  "name": "${NODE_NAME:-$(hostname)}",
  "work_dir": "${WORK_DIR:-/code}"
}
CONF
        _info "Config written to $CONFIG_DIR_NI/config.json"
    fi
    _info "Installation complete."
fi
