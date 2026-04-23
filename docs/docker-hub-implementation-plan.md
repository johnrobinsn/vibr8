# Docker Hub + Native Node — Implementation Plan

## Context

The architecture is locked in: Docker container as compute head (web server, ML inference, WebRTC audio) + native node on the host for desktop streaming, CLI sessions, and Ring0. The native node is mechanically a standard remote node that happens to be on the same machine. See `docs/docker-hub-architecture.md` for the decisions.

This document covers every change needed to go from the current state (Docker hub smoke-tested, bare-metal vibr8 running) to the new architecture. Rollback tag: `pre-hub-refactor` (commit `766aeb5`).

---

## 1. Auth for Local Node

### Problem

Remote nodes authenticate via API keys generated in the hub's Settings UI. For the native node ("hub node"), requiring the user to manually generate a key, copy it, and paste it into the node setup wizard is friction that doesn't need to exist — the node is on the same machine.

### Design

Auto-generate a dedicated API key during hub first-run setup. The key is written to a well-known file that the native node reads on startup.

**Key file:** `$DATA_DIR/vibr8/hub-node-key` (inside the volume mounted at `/home/vibr8/.vibr8/hub-node-key` in the container). Since `$DATA_DIR/vibr8/` maps to `~/.vibr8-hub/vibr8/` on the host, the native node can read it directly from `~/.vibr8-hub/vibr8/hub-node-key`.

### Changes

**`bin/hub-entrypoint.sh`** — in `_first_run_setup()`, after signing secret generation:

```bash
# ── Generate hub-node API key ──────────────────────────────────
HUB_NODE_KEY_FILE="$VIBR8_DIR/hub-node-key"
if [ ! -f "$HUB_NODE_KEY_FILE" ]; then
    python3 -c "
import sys, json
sys.path.insert(0, '/app')
from server.node_registry import NodeRegistry
reg = NodeRegistry()
raw_key, entry = reg.generate_api_key('hub-node')
with open('$HUB_NODE_KEY_FILE', 'w') as f:
    f.write(raw_key)
print(f'[hub] Generated hub-node API key (id={entry.id})')
"
    chmod 600 "$HUB_NODE_KEY_FILE"
fi
```

This generates the key via the existing `NodeRegistry.generate_api_key()` method (`server/node_registry.py:253-267`), which stores the bcrypt hash in `nodes.json`. The raw key is written to the file for the native node to read.

**`install-node.sh`** — add a `--hub-node` flag:

```bash
--hub-node)  hub_node=true; shift ;;
```

When `--hub-node` is set:
- Default hub URL: `wss://host.docker.internal:3456` (Docker's host-gateway alias) or `wss://localhost:$PORT`
- Read API key from `~/.vibr8-hub/vibr8/hub-node-key` instead of prompting
- Set node name to `"hub-node"` by default
- Auto-set `--skip-ssl-verify` (see section 2)

**`bin/vibr8-hub`** — add `--native-node` flag (see section 4)

### Files

| File | Change |
|------|--------|
| `bin/hub-entrypoint.sh` | Generate API key in `_first_run_setup()` |
| `server/node_registry.py` | No change — `generate_api_key()` already exists |
| `install-node.sh` | Add `--hub-node` flag, auto-read key file |

---

## 2. SSL Trust for All Nodes

### Problem

The hub generates a self-signed SSL certificate by default. When any remote node connects via `wss://`, Python's `aiohttp.ClientSession` uses the system certificate store and rejects the self-signed cert. There is **zero** SSL verification code in `vibr8_node/node_agent.py` today — `aiohttp` defaults to strict verification.

This affects every node connecting to a self-signed hub, not just the hub's native node.

### Design

Two mechanisms:

1. **`--skip-ssl-verify` flag** on `install-node.sh` and `vibr8-node setup` — stores `"ssl_verify": false` in `~/.vibr8-node/config.json`. Quick, works everywhere.
2. **Cert pinning** (future enhancement) — during setup, the node fetches the hub's cert fingerprint and stores it. Connections verify against the pinned fingerprint instead of the system CA store. More secure but more complex.

Start with `--skip-ssl-verify` only.

### Changes

**`vibr8_node/node_agent.py`** — add `ssl_verify` parameter:

```python
# In __init__:
self.ssl_verify = ssl_verify  # from config

# In _register() and _connect_tunnel():
ssl_ctx = None
if not self.ssl_verify:
    import ssl
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

# _register():
async with aiohttp.ClientSession() as session:
    connector = aiohttp.TCPConnector(ssl=ssl_ctx) if ssl_ctx else None
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.post(url, json={...}) as resp:

# _connect_tunnel():
async with aiohttp.ClientSession() as session:
    async with session.ws_connect(ws_url, heartbeat=45, ssl=ssl_ctx) as ws:
```

**`vibr8_node/__main__.py`** — read `ssl_verify` from config:

```python
ssl_verify = config.get("ssl_verify", True)
# Pass to NodeAgent constructor
```

**`install-node.sh`** — add `--skip-ssl-verify` flag:

```bash
--skip-ssl-verify)  ssl_verify=false; shift ;;
```

When set, the config.json output includes `"ssl_verify": false`:

```json
{
  "hub_url": "wss://...",
  "api_key": "sk-node-...",
  "name": "...",
  "work_dir": "/code",
  "ssl_verify": false
}
```

Also update the embedded setup wizard (`setup.py`) to ask about SSL verification when the hub URL uses `wss://`:

```
Hub URL: wss://192.168.1.50:3456
  Skip SSL verification? (for self-signed certs) [y/N]: y
```

### Files

| File | Change |
|------|--------|
| `vibr8_node/node_agent.py:170-176` | Add `ssl` param to `ws_connect()` and `session.post()` |
| `vibr8_node/node_agent.py:126-128` | Add `ssl` param to registration POST |
| `vibr8_node/__main__.py` | Read `ssl_verify` from config, pass to NodeAgent |
| `install-node.sh` | Add `--skip-ssl-verify` flag, write to config |
| `install-node.sh` (setup wizard) | Add SSL verification prompt |

---

## 3. Hub Node Identity in UI

### Problem

The native node should appear as "hub node" (or user-chosen name) in the frontend node dropdown, distinguishable from generic remote nodes. Currently, the node dropdown is a plain `<select>` in `Sidebar.tsx:695-719` that shows `n.name` and `(offline)` status.

### Design

Add an `isHubNode` boolean to the node's capabilities. The hub identifies this during registration based on:
- The API key matching the auto-generated hub-node key, OR
- A `"hubNode": true` field in the registration capabilities

The frontend uses this to show a label/icon distinguishing the hub node.

### Changes

**`server/node_registry.py`** — add `is_hub_node` to `RegisteredNode`:

```python
@dataclass
class RegisteredNode:
    ...
    is_hub_node: bool = False
```

In `register()`, detect hub-node key:

```python
# After validation, check if this is the hub-node key
hub_node_key_path = Path("~/.vibr8/hub-node-key").expanduser()
if hub_node_key_path.exists():
    hub_key = hub_node_key_path.read_text().strip()
    if api_key == hub_key:
        node.is_hub_node = True
```

In `to_api_dict()`, include it:

```python
def to_api_dict(self) -> dict:
    return {
        ...
        "isHubNode": self.is_hub_node,
    }
```

**`web/src/types.ts`** — extend `NodeInfo`:

```typescript
export interface NodeInfo {
  ...
  isHubNode?: boolean;
}
```

**`web/src/components/Sidebar.tsx`** — in the node dropdown, show a visual indicator:

```tsx
<option key={n.id} value={n.id} disabled={n.status === "offline"}>
  {n.name}
  {n.isHubNode ? " (hub)" : ""}
  {n.status === "offline" ? " (offline)" : ""}
</option>
```

### Files

| File | Change |
|------|--------|
| `server/node_registry.py` | Add `is_hub_node` field, detect during registration, include in `to_api_dict()` |
| `server/node_registry.py` | Persist/load `is_hub_node` in `_save()`/`_load()` |
| `web/src/types.ts` | Add `isHubNode?: boolean` to `NodeInfo` |
| `web/src/components/Sidebar.tsx:695-719` | Show "(hub)" label for hub node |

---

## 4. Single-Command Orchestration

### Problem

Users shouldn't need to separately start the Docker container and the native node. A single `vibr8-hub run --native-node` should do both.

### Design

Add a `--native-node` flag to `vibr8-hub run` that:
1. Starts the Docker container (as normal)
2. Waits for the hub to be healthy (API responds)
3. Installs the native node if not already installed (runs `install-node.sh --hub-node --non-interactive`)
4. Starts the native node (`vibr8-node run` in background or via systemd/launchd)

### Changes

**`bin/vibr8-hub`** — in `cmd_run()`, add `--native-node` handling:

```bash
--native-node)  native_node=true; shift ;;
```

After container startup and credential output:

```bash
if $native_node; then
    _info "Setting up native node ..."

    # Wait for hub to be healthy
    local hub_url="https://localhost:${port}"
    if $external_ssl; then hub_url="http://localhost:${port}"; fi
    for _i in $(seq 1 30); do
        curl -sk "${hub_url}/api/nodes" >/dev/null 2>&1 && break
        sleep 1
    done

    # Read the auto-generated hub-node API key
    local key_file="$data_dir/vibr8/hub-node-key"
    if [ ! -f "$key_file" ]; then
        _warn "Hub-node API key not found at $key_file"
        _warn "Start the hub first, then run: vibr8-hub setup-node"
    else
        local hub_key
        hub_key="$(cat "$key_file")"

        # Install node if not present
        if ! command -v vibr8-node >/dev/null 2>&1; then
            _info "Installing native node ..."
            "$REPO_ROOT/install-node.sh" \
                --hub-node \
                --non-interactive \
                --hub "wss://localhost:${port}" \
                --api-key "$hub_key" \
                --skip-ssl-verify \
                --no-service
        fi

        # Start the node
        _info "Starting native node ..."
        vibr8-node run &
        NATIVE_NODE_PID=$!
        echo "$NATIVE_NODE_PID" > "$data_dir/native-node.pid"
    fi
fi
```

Also persist `NATIVE_NODE=true` in `config.env` so `vibr8-hub start` and `vibr8-hub update` know to restart the native node too.

**`bin/vibr8-hub`** — update `cmd_stop()`:

```bash
# Stop native node if running
local pid_file="$data_dir/native-node.pid"
if [ -f "$pid_file" ]; then
    local pid
    pid="$(cat "$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
        _info "Stopping native node (PID $pid) ..."
        kill "$pid"
    fi
    rm -f "$pid_file"
fi
```

Similarly update `cmd_start()` and `cmd_restart()`.

**Alternative:** Instead of managing the native node PID directly, use the platform's service manager (systemd/launchd). The `--native-node` flag during `run` installs the service, and `vibr8-hub stop/start` calls `systemctl --user stop/start vibr8-node`. This is more robust for production use.

### Files

| File | Change |
|------|--------|
| `bin/vibr8-hub` | Add `--native-node` to `cmd_run()`, lifecycle in `cmd_stop/start/restart` |
| `bin/vibr8-hub` | Persist `NATIVE_NODE` in `config.env` |

---

## 5. Docker Image Changes

### Decision

The Docker image stays the same. No components are removed for "native node mode." The virtual desktop, CLI launcher, and Ring0 are all present in the image but simply don't do meaningful work when a native node handles those responsibilities.

### Rationale

- **Virtual desktop (Xvfb + XFCE):** Controlled by `VIRTUAL_DISPLAY` env var. If unset, `_start_virtual_display()` returns immediately (line 152-153 of `hub-entrypoint.sh`). Already a no-op when not configured.
- **CLI launcher:** Always initialized in `server/main.py`. When a native node is active and sessions are created with that node's ID, they route to the node via tunnel. The local launcher just sits idle — no extra resource cost.
- **Ring0:** Auto-launches if `ring0.json` has `enabled: true`. In the hub container, Ring0 is enabled for the container's own local node, but voice transcripts route to the native node's Ring0 when it's the active node. The container's Ring0 serves as a fallback if no native node is connected.
- **The lite image** (`--no-remote-desktop`) already strips the desktop stack. It's for headless deployments that don't need any desktop at all.

### No code changes needed

The `--native-node` flag on `vibr8-hub run` could optionally skip `VIRTUAL_DISPLAY` (since the host desktop is used instead), but this is an optimization, not a requirement. The container's virtual display is harmless when a native node is active — it just consumes ~50MB of RAM for Xvfb.

**Optional optimization** for `bin/vibr8-hub cmd_run()`:

```bash
# Skip virtual display when using native node for desktop
if $native_node; then
    # Don't pass VIRTUAL_DISPLAY — native node streams host desktop
    :
elif ! $no_remote_desktop; then
    args+=(-e "VIRTUAL_DISPLAY=$display")
fi
```

---

## 6. install-node.sh Changes

### New flag: `--hub-node`

Combines several flags into a single "install as the hub's native node" mode:

```bash
--hub-node)
    hub_node=true
    ssl_verify=false  # Hub uses self-signed cert by default
    shift ;;
```

Behavior when `--hub-node` is set:

1. **Hub URL default:** `wss://localhost:${VIBR8_HUB_PORT:-3456}` (instead of prompting or requiring `--hub`)
2. **API key source:** Read from `~/.vibr8-hub/vibr8/hub-node-key` (instead of prompting or requiring `--api-key`)
3. **Node name default:** `"hub-node"` (instead of hostname)
4. **Work dir default:** Current directory (same as normal)
5. **SSL verify:** Disabled by default (hub uses self-signed cert)
6. **Service installation:** Normal — install systemd/launchd service as usual

### Updated config output

The generated `~/.vibr8-node/config.json` for a hub node:

```json
{
  "hub_url": "wss://localhost:3456",
  "api_key": "sk-node-...",
  "name": "hub-node",
  "work_dir": "/home/user/code",
  "ssl_verify": false,
  "hub_node": true
}
```

The `hub_node: true` flag lets the node agent know it's the hub's native node (for future use — e.g., special reconnection behavior, or shorter backoff since it's local).

### Desktop deps

The hub-node use case almost always wants desktop streaming. Consider making `--hub-node` imply desktop deps are installed (equivalent to not passing `--no-desktop`).

### Files

| File | Change |
|------|--------|
| `install-node.sh` | Add `--hub-node` flag with auto-defaults |
| `install-node.sh` | Add `--skip-ssl-verify` flag |
| `install-node.sh` (setup wizard) | SSL verification prompt |
| `install-node.sh` (non-interactive config) | Write `ssl_verify` and `hub_node` fields |

---

## 7. Migration Steps

### From bare-metal vibr8 to Docker hub + native node

**Prerequisites:**
- Docker installed on the host
- Current bare-metal vibr8 running at `~/.vibr8/`
- Git repo at current state (tagged `pre-hub-refactor`)

### Sequence

```
Step 1: Build the Docker hub image
Step 2: Stop bare-metal vibr8
Step 3: Migrate data
Step 4: Start Docker hub
Step 5: Install + start native node
Step 6: Verify
```

#### Step 1: Build

```bash
cd /mntc/code/vibr8
bin/vibr8-hub build
```

#### Step 2: Stop bare-metal

```bash
# Stop the running vibr8 server
# (however it's currently managed — tmux, systemd, manual process)
pkill -f "python -m server.main" || true
```

#### Step 3: Migrate data

Copy bare-metal config into the Docker volume structure:

```bash
DATA_DIR=~/.vibr8-hub
mkdir -p "$DATA_DIR"/{vibr8,claude,certs,cache/huggingface,cache/torch,voice}

# Core config
cp ~/.vibr8/users.json      "$DATA_DIR/vibr8/"
cp ~/.vibr8/secret.key       "$DATA_DIR/vibr8/"
cp ~/.vibr8/ring0.json       "$DATA_DIR/vibr8/"
cp ~/.vibr8/nodes.json       "$DATA_DIR/vibr8/" 2>/dev/null || true
cp ~/.vibr8/ice-servers.json "$DATA_DIR/vibr8/" 2>/dev/null || true

# Ring0 events config
cp ~/.vibr8/ring0-events.json5 "$DATA_DIR/vibr8/" 2>/dev/null || true

# Environment sets
cp -r ~/.vibr8/envs/         "$DATA_DIR/vibr8/" 2>/dev/null || true

# Session data
cp -r ~/.vibr8/sessions/     "$DATA_DIR/vibr8/" 2>/dev/null || true

# Ring0 persistent state (memory, tasks, queue)
cp -r ~/.vibr8/ring0/        "$DATA_DIR/vibr8/" 2>/dev/null || true

# Device tokens
cp ~/.vibr8/device-tokens.json "$DATA_DIR/vibr8/" 2>/dev/null || true

# Voice logs (large — optional)
cp -r ~/.vibr8/data/voice/   "$DATA_DIR/voice/" 2>/dev/null || true

# Model caches (large — optional, will re-download if missing)
cp -r ~/.cache/huggingface/  "$DATA_DIR/cache/huggingface/" 2>/dev/null || true
cp -r ~/.cache/torch/        "$DATA_DIR/cache/torch/" 2>/dev/null || true
```

**Future: `vibr8-hub migrate` command** — automates the above:

```bash
bin/vibr8-hub migrate [--from ~/.vibr8] [--include-voice] [--include-models]
```

Reads from `--from` (default `~/.vibr8/`), copies to `$DATA_DIR`. Dry-run by default, `--execute` to actually copy. Reports what was found and what was migrated.

#### Step 4: Start Docker hub

```bash
# Same port as bare-metal so existing bookmarks/configs work
bin/vibr8-hub run --port 3456 --native-node --openai-key sk-...
```

Since `users.json` already exists from migration, first-run setup is skipped. Existing credentials work.

#### Step 5: Native node starts automatically

With `--native-node`, the hub CLI handles node installation and startup (see section 4).

If doing manually:

```bash
# The hub-node key was generated during container first-run
cat ~/.vibr8-hub/vibr8/hub-node-key

# Install and configure
./install-node.sh --hub-node --non-interactive
vibr8-node run
```

#### Step 6: Verify

```bash
# Hub is running
bin/vibr8-hub status

# Node appears in UI
curl -sk https://localhost:3456/api/nodes | python3 -m json.tool

# Open browser, confirm:
# - Login works with existing credentials
# - Sessions list is populated
# - Node dropdown shows "hub-node (hub)"
# - Can create a session on the hub node
# - Voice/WebRTC works
# - Ring0 responds (if enabled)
```

### Edge cases

- **Port conflict:** If bare-metal was on 3456 and Docker tries the same port, make sure bare-metal is fully stopped first.
- **sessions/ data:** Session state files reference CLI PIDs that won't exist after migration. The server handles this gracefully — sessions show as offline and relaunch when a browser connects.
- **ring0.json `sessionId`:** The Ring0 session ID from bare-metal won't match a running CLI process. Ring0 will create a new session on first enable. The old session ID is harmless.
- **nodes.json:** Existing remote nodes in `nodes.json` will carry over. They'll need to reconnect to the new hub URL if it changed. The native hub node is a new registration.

---

## 8. Rollback Plan

### Git rollback

```bash
git checkout pre-hub-refactor
```

This restores all code to the pre-refactor state. The Docker hub files already exist at this tag (they were built before tagging), so the only rollback needed is for the native-node integration changes.

### Runtime rollback

#### Docker hub → bare-metal

```bash
# 1. Stop Docker hub + native node
bin/vibr8-hub stop

# 2. Copy data back (reverse migration)
cp ~/.vibr8-hub/vibr8/users.json      ~/.vibr8/
cp ~/.vibr8-hub/vibr8/secret.key       ~/.vibr8/
cp ~/.vibr8-hub/vibr8/ring0.json       ~/.vibr8/
cp -r ~/.vibr8-hub/vibr8/sessions/     ~/.vibr8/
cp -r ~/.vibr8-hub/vibr8/envs/         ~/.vibr8/
cp -r ~/.vibr8-hub/vibr8/ring0/        ~/.vibr8/
# ... etc

# 3. Start bare-metal
cd /mntc/code/vibr8
git checkout pre-hub-refactor
make dev
```

#### Partial rollback (keep Docker, remove native node)

```bash
# Stop and uninstall native node
vibr8-node stop
systemctl --user disable vibr8-node 2>/dev/null || true
rm -rf ~/.vibr8-node

# Hub continues running with virtual desktop
# Re-enable virtual display if it was disabled:
bin/vibr8-hub destroy --keep-data
bin/vibr8-hub run --port 3456  # without --native-node
```

#### Nuclear option

```bash
bin/vibr8-hub destroy          # Removes container AND data volumes
rm -rf ~/.vibr8-hub            # Remove all hub data
rm -rf ~/.vibr8-node           # Remove node installation
git checkout pre-hub-refactor  # Restore code
# Bare-metal ~/.vibr8/ is untouched if migration used cp (not mv)
```

### Key safety note

The migration in step 7 uses **`cp`** (copy), not `mv` (move). The original `~/.vibr8/` directory is preserved throughout. Rollback to bare-metal is always possible by just restarting the old server pointing at the original data.

---

## 9. Other Small Gaps

### STT trust_repo fix

**File:** `server/stt.py:123` (approximate — find `torch.hub.load()`)

```python
# Before:
model = torch.hub.load(...)

# After:
model = torch.hub.load(..., trust_repo=True)
```

One line. Affects Docker (no TTY) and any headless deployment.

### OPENAI_API_KEY documentation

Already handled by the `--openai-key` flag on `vibr8-hub run` and host `$OPENAI_API_KEY` forwarding. Just needs clear documentation that TTS won't work without it.

### VIBR8_DATA_DIR wiring

**Files to verify:** `server/voice_logger.py`, `server/voice_profiles.py`

The Dockerfile sets `VIBR8_DATA_DIR` pointing to the volume-mounted `/data/voice`. Verify these modules respect the env var. If they hardcode `~/.vibr8/data`, fix them to use `os.environ.get("VIBR8_DATA_DIR", ...)`.

### ADB / Android in Docker

Not supported. Android features require bare-metal or `--privileged` Docker. Document-only.

---

## Implementation Order

Recommended sequence to minimize risk and allow incremental testing:

1. **STT trust_repo fix** — one line, no dependencies, unblocks Docker voice
2. **SSL trust for nodes** — `--skip-ssl-verify` in node agent + install script
3. **Hub node auth** — auto-generated key in entrypoint, `--hub-node` in installer
4. **Hub node identity** — `isHubNode` field in registry + frontend label
5. **Single-command orchestration** — `--native-node` in vibr8-hub CLI
6. **Migration command** — `vibr8-hub migrate`
7. **Verification** — end-to-end test of the full flow

Steps 1-2 are independent and can be done in parallel. Steps 3-4 are independent of each other but both depend on 2. Step 5 depends on 3. Step 6 is independent.

---

## Verification Checklist

After implementation, verify end-to-end:

- [ ] `vibr8-hub build` completes without errors
- [ ] `vibr8-hub run --defaults --native-node` starts both container and node
- [ ] Hub UI accessible at `https://localhost:3456`
- [ ] Login works with auto-generated credentials
- [ ] Node dropdown shows "hub-node (hub)" as online
- [ ] Can create a session on the hub node
- [ ] Session output streams in real-time
- [ ] Voice/WebRTC connects (push-to-talk → STT → response)
- [ ] TTS plays back (requires OPENAI_API_KEY)
- [ ] Ring0 responds to voice commands via the native node
- [ ] Desktop tab shows host desktop stream (not virtual)
- [ ] Computer-use agent can control host desktop
- [ ] `vibr8-hub stop` stops both container and native node
- [ ] `vibr8-hub start` restarts both
- [ ] Migration from bare-metal preserves sessions, users, Ring0 state
- [ ] Rollback to bare-metal works (original `~/.vibr8/` untouched)
- [ ] Remote nodes (non-hub) can connect with `--skip-ssl-verify`
- [ ] Remote nodes without `--skip-ssl-verify` fail cleanly with a clear SSL error message
