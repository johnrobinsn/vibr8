<p align="center">
  <img src="web/public/logo.svg" alt="vibr8" width="80" />
</p>

<h1 align="center">vibr8</h1>

<p align="center">
  A modality-elastic personal agent — the agent that comes with you across your day.<br/>
  Voice on a walk. Text + screen at your laptop. Full multimodal at your desk.<br/>
  Same agent, same context, adapts to whatever's around you.
</p>

<p align="center">
  <em>Licensed under Apache 2.0 · Built by <a href="https://ringzero.ai">RingZero LLC</a></em>
</p>

---

## What is vibr8?

vibr8 is a **layer** on top of coding-agent CLIs (Claude Code, Codex, OpenCode, Hermes). It doesn't replace them — it wraps them, so the agent you use scales across whatever interface you have available.

**Four design axes:**

- **Modality-elastic** — voice-only when you're walking, voice + car browser in traffic, text + screen at your laptop, full bidirectional audio/video/text/drawing at your desk. Same session, same context, degrades gracefully to whatever channel is available.
- **Personal** — designed for individual use, not enterprise. Self-hosted on your own machine.
- **Self-hostable** — your context and skills live on your computer. No SaaS lock-in. Apache 2.0 licensed.
- **Adaptive** — persistent memory, per-user personalization, and (soon) model-level adaptation. An agent that changes with you, not just remembers you.

The **ring0 meta-agent** runs alongside your coding agent, handles voice I/O, orchestrates transitions between devices, and translates chatty agent output into voice-suitable dialog.

---

## Features

Grouped by experiential area — each maps to one or more of the four axes above.

### Voice-first interaction (modality-elastic)

- **Real-time bidirectional voice** — WebRTC audio with push-to-talk; streaming STT (Whisper + Silero VAD with speculative decoding); streaming TTS (Kokoro local by default, OpenAI cloud optional)
- **Speaker fingerprinting** — multi-embedding voice profiles (one per device/environment); STT gate blocks other voices before transcription
- **Target speaker extraction (TSE)** — WeSep BSRNN model isolates the enrolled speaker's voice from background talkers (TV, family in the room) in noisy environments
- **Voice commands** — guard-word activated hands-free control (guard mode, note mode, node switching, TTS mute)
- **Prompt accumulation** — mid-thought pauses don't split your utterance

### Screens & devices (modality-elastic)

- **Second screen** — push markdown, images, PDFs, HTML, or live session mirrors to paired displays (TVs, tablets, car browsers, iPads)
- **Code viewer** — syntax-highlighted file viewing (CodeMirror)
- **Artifacts + viewer pane** — persistent content items (summaries, plans, reports) surface in a resizable side panel, separate from the chat transcript

### Layer over agentic harnesses (layer identity)

- **Multiple coding-agent backends** — Claude Code, Codex (app-server), OpenCode, Hermes (ACP) in one UI. Each session picks its backend, model, and permission mode independently.
- **Multi-session management** — launch, monitor, and switch between any number of concurrent sessions across all backends
- **Live streaming** — real-time agent output over WebSocket (NDJSON protocol)
- **Git integration** — per-session branch tracking, worktree isolation, ahead/behind counts, diff stats
- **Environment sets** — named sets of environment variables

### Ring0 meta-agent (orchestration)

- **Voice-controlled supervisor** — manages sessions, permissions, second screens, artifacts, and client devices via MCP tools
- **Voice-optimized dialog** — translates chatty agent output into voice-suitable dialog so voice-only control of coding agents actually works
- **Cross-session bridge** — the meta-layer between your underlying coding agent and the modality-elastic surfaces

### Self-hostable, personal, and adaptive

- **Runs on your own hardware** — local desktop or remote nodes
- **Remote nodes** — Docker, EC2, macOS via outbound WebSocket tunnel (no SSH or local network access required); any backend can run on any node
- **Local-first defaults** — Kokoro TTS + Whisper STT run locally; cloud services are optional, not required
- **Computer-use agent** — vision-language model (UI-TARS) controls desktop GUIs autonomously (screenshots, clicks, typing, scrolling)
- **Auto-reconnect** — connection resilience with timeout, cancel, and manual retry
- **Dark / light mode** — persisted theme preference

## Prerequisites

- Python 3.11 or 3.12
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Bun](https://bun.sh/) (JavaScript runtime / package manager)
- At least one coding-agent CLI installed and authenticated:
  - [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) — `claude` binary on PATH
  - [Codex CLI](https://github.com/openai/codex) — `codex` binary on PATH
  - [OpenCode CLI](https://github.com/sst/opencode) — `opencode` binary on PATH
  - [Hermes CLI](https://github.com/jasonkneen/hermes) — `hermes` binary on PATH
- Optional: NVIDIA GPU for target speaker extraction (TSE) and computer-use VLM inference

## Quick Start

```bash
# Install dependencies
make install

# Start development servers (backend on :3456, frontend on :5174)
make dev
```

Open [http://localhost:5174](http://localhost:5174) in your browser.

For a complete walkthrough (system packages, GPU setup, ML model requirements, Docker deployment), see **[INSTALL.md](INSTALL.md)**.

## Commands

| Command              | Description                                    |
|----------------------|------------------------------------------------|
| `make install`       | Install Python and frontend dependencies       |
| `make dev`           | Run backend + frontend together                |
| `make dev-api`       | Backend only (port 3456)                       |
| `make dev-frontend`  | Frontend only (port 5174, proxies to backend)  |
| `make build`         | Build frontend for production                  |
| `make test`          | Run all tests                                  |
| `make test-py`       | Python tests (`pytest`)                        |
| `make test-frontend` | Frontend tests (`vitest`)                      |

## Coding Agent Backends

vibr8 can drive four different coding-agent CLIs in a single UI. Each session picks its backend at creation; the wire format the browser sees (messages, tool calls, permission prompts) is normalized regardless of which backend is running.

| Backend | Transport | Notes |
|---|---|---|
| **Claude Code** | WebSocket (`--sdk-url`) | Native protocol; full feature support |
| **Codex** | JSON-RPC over stdio (`codex app-server`) | Sandbox mode auto-selected per host (workspace-write when bwrap user namespaces work, danger-full-access otherwise — vibr8's approval UI is the trust boundary in the latter case) |
| **OpenCode** | NDJSON over stdio | Dynamic model list fetched from `opencode models` (5-min cache) |
| **Hermes** | JSON-RPC over stdio (ACP, `hermes acp`) | Default model read from `~/.hermes/config.yaml`; routes vibr8's MCP through ACP `McpServerStdio` |

A fifth "backend" is the **computer-use** agent — not a coding agent, but uses the same session machinery. See [Computer-Use Agent](#computer-use-agent) below.

`GET /api/backends` reports which backends have their CLI on PATH at runtime; the New Session UI greys out the unavailable ones.

## Text-to-Speech

vibr8 supports two TTS engines, controlled by the `VIBR8_TTS_ENGINE` environment variable:

| Engine | Default | Requirements | Notes |
|--------|---------|-------------|-------|
| **Kokoro** | Yes | Included in base dependencies | Local neural TTS — no API key, no network latency |
| **OpenAI** | No | `OPENAI_API_KEY` | Cloud TTS via `tts-1-hd` — higher quality |

Kokoro is the out-of-box default. Set `VIBR8_TTS_ENGINE=openai` for cloud TTS. If Kokoro is not installed, it falls back to OpenAI automatically.

Additional TTS configuration:

| Variable | Default | Description |
|----------|---------|-------------|
| `VIBR8_TTS_VOICE` | `af_sarah` (Kokoro) / `echo` (OpenAI) | Voice name |
| `VIBR8_TTS_SPEED` | `1.0` | Playback speed multiplier |

## Speaker Fingerprinting

Speaker fingerprinting uses ECAPA-TDNN embeddings (via SpeechBrain) to identify speakers by voice. When a fingerprint is active, the STT pipeline only accepts audio from that speaker — other voices are rejected before transcription.

### Profiles and Multi-Embedding

A profile is one person; each profile can hold **multiple labeled embeddings** captured from different devices and environments (e.g. "MacBook mic", "AirPods", "kitchen", "car"). The gate matches against the best-scoring embedding per profile, so a single profile can cover the room mic *and* a noisy phone mic without false rejections. Profiles are stored under `~/.vibr8/data/voice/fingerprints/{username}/` in a versioned schema (currently v3).

### Setup

1. Go to **Settings → Speaker ID**.
2. Click **Start Enrollment** — speak a few sentences to capture your voice.
3. Save the fingerprint and set it as active.
4. Adjust the similarity threshold (default 0.45) if needed.
5. To cover another device or room, **add embedding** to the same profile rather than creating a new one.

Speaker gates are applied per-user and activate automatically on WebRTC connect.

### Target Speaker Extraction (TSE) — Noisy Environments

When you're in a room with other people talking (family in the background, TV, hallway noise), even a tight similarity gate can struggle: it correctly rejects the background speaker's utterances, but if you and someone else speak simultaneously, the mixed audio fails the gate. TSE fixes this by **isolating** your voice from the mixture before the gate runs.

Pipeline:

```
audio segment
  → SpeechBrain ECAPA embedding → cosine gate (rejects pure background talkers)
  → if TSE on + your voice matched: WeSep BSRNN(audio, WeSpeaker embedding)
                                       → cleaned audio containing only your voice
  → Whisper
```

Each enrollment captures **two** embeddings — SpeechBrain ECAPA (for the gate) and WeSpeaker ECAPA (to condition the BSRNN extractor) — because the two ECAPA training regimes produce incompatible embedding spaces.

#### Requirements

- NVIDIA GPU (CUDA) — TSE runs the BSRNN model per voice segment
- WeSep BSRNN checkpoint (`avg_model.pt`) in one of:
  - `$VIBR8_WESEP_DIR` (env var override)
  - `~/.vibr8/models/wespeaker-bsrnn-vox1/avg_model.pt`
  - `~/.wesep/english/avg_model.pt`

When unavailable, `GET /api/voice/tse/available` returns `{"available": false}` and the UI hides the toggle.

#### Setup

1. Make sure your active fingerprint has a `embedding_wespeaker` field — newly enrolled voiceprints get it automatically. To backfill a pre-existing profile from its stored audio:
   ```bash
   uv run python -m server.scripts.migrate_v3_wespeaker --user <username>
   ```
2. In **Settings → Speaker ID**, flip **Enable TSE** on. Adjust the TSE threshold (default 0.35; lower because cleaned audio scores higher on the gate).
3. The toggle is disabled if either the GPU or the BSRNN checkpoint is missing.

## Remote Nodes

vibr8 supports remote nodes that can host coding-agent sessions and desktop environments. Nodes connect to the hub via outbound WebSocket — no SSH or local network access required.

### How It Works

- Nodes register with the hub using API keys and maintain a persistent WebSocket connection
- Sessions created with a `nodeId` are forwarded to the remote node, which spawns the CLI locally
- Any of the four coding-agent backends (Claude Code, Codex, OpenCode, Hermes) can run on a node; set the default via `--default-backend {claude|codex|opencode|hermes}`
- Computer-use sessions run VLM inference on the hub but target the remote node's desktop for screen capture and input injection
- Each node can run its own Ring0 instance for voice-controlled session management

### Node Installation

```bash
# Interactive install (macOS / Linux)
curl -fsSL https://vibr8.ringzero.ai/install-node | bash

# From local repo
./install-node.sh

# Docker (non-interactive)
./install-node.sh --non-interactive --no-service --source ./

# Add desktop streaming to an existing install
./install-node.sh --desktop-only
```

Docker images are provided for various configurations:

| Dockerfile | Purpose |
|---|---|
| `Dockerfile.node` | Runtime node (no desktop) |
| `Dockerfile.node-gpu` | GPU-enabled variant |
| `Dockerfile.node-gui` | X11 GUI desktop support |

### Running a Node with a Specific Backend

```bash
# Codex as the default backend
uv run python -m vibr8_node \
  --hub wss://your-hub:3456 \
  --name my-codex-node \
  --default-backend codex \
  --api-key <node-api-key>

# Same pattern for opencode or hermes
uv run python -m vibr8_node --hub wss://your-hub:3456 --name my-opencode-node \
  --default-backend opencode --api-key <node-api-key>
```

### Node Management

- **API keys**: Manage node API keys in the Settings page
- **Voice switching**: Say "vibr8 node {name}" to switch the active node
- **Node config**: Stored at `~/.vibr8-node/config.json` on the node

### Pin a browser tab to a single node (`/@node` deeplink)

Any of these URL forms locks the tab to one node — the switcher
dropdown in the shell strip becomes a static label, voice / Ring0
switch attempts refuse rather than silently redirect, and sharing
the URL sends the recipient to the same pinned node:

```
https://your-hub/@blah        # canonical
https://your-hub/n/blah       # equivalent, useful in scripts
https://your-hub/?pin=blah    # legacy query form
```

`blah` matches by exact node name (case-insensitive), then by node
id prefix. If the pinned node isn't registered or is offline, the
shell renders a pin-specific "Node unavailable" card rather than
falling back to a different node — the page updates automatically
once the node comes online. Voice / Ring0 `switch_node` returns an
honest error to the caller instead of pretending to switch. Useful
for dedicating a tab (or a browser profile) to a single machine,
e.g. leaving one tab pinned to your dev laptop and another pinned
to a remote GPU box.

## Docker Hub Deployment

Run the full vibr8 hub (server, Ring0, voice, virtual desktop, computer-use) in a single Docker container.

```bash
# One-liner — auto-generates admin credentials, auto-detects GPU
bin/vibr8-hub run --defaults --gpu

# With explicit credentials and API keys
bin/vibr8-hub run --admin-user john --admin-password s3cret --openai-key sk-...

# Lite mode (no virtual desktop, ~2GB smaller image)
bin/vibr8-hub run --defaults --no-remote-desktop
```

The hub image includes the full virtual display stack (Xvfb, XFCE4, Chrome, VSCode) so it can act as its own local node with computer-use. Data persists in `~/.vibr8-hub/` across restarts and updates.

Run `bin/vibr8-hub help` for the full CLI reference (build, run, stop, start, restart, logs, status, update, backup, restore, warmup, shell, destroy).

## Computer-Use Agent

The computer-use agent uses a vision-language model (UI-TARS 7B) to control desktop GUIs autonomously. It takes screenshots, runs inference, parses actions, and executes them in a loop.

### Supported Actions

`click`, `type`, `scroll`, `press`, `wait`, `finished`

### Execution Modes

| Mode | Behavior |
|---|---|
| AUTO | Execute actions immediately |
| CONFIRM | Always ask user before executing |
| GATED | Auto-execute if parsed cleanly, otherwise ask |

### Controls

The agent panel provides pause/resume, watch/act mode toggle, and an execution mode selector. In CONFIRM/GATED mode, an approval dialog appears before risky actions.

## Ring0 Meta-Agent

Ring0 is a supervisor agent that controls vibr8 via voice. When enabled, all voice input routes to Ring0 instead of the active session. Ring0 exposes MCP tools for:

- **Session management** — Create, list, rename, interrupt, and send messages to sessions across all backends (Claude, Codex, OpenCode, Hermes, computer-use)
- **Permission handling** — View and respond to pending permission requests
- **UI control** — Switch which session the browser displays, set session modes (plan / acceptEdits)
- **Client management** — List connected clients, query device info, send notifications, control audio devices, read/write clipboard
- **Artifacts** — Publish, list, share, and delete persistent content items (summaries, plans, reports) that surface in the viewer pane
- **Second screen** — Pair, list, push content, adjust scale/dark mode/TV-safe margins
- **Screenshot capture** — Capture any client's current view
- **Android control** — Launch apps on connected Android devices
- **Node info** — Query the current node's environment (platform, hostname, containerized, display available)
- **Guard mode** — Enable/disable voice guard mode programmatically

### Ring0 Configuration

Config stored at `~/.vibr8/ring0.json`:

```json
{
  "enabled": true,
  "sessionId": "...",
  "model": "claude-sonnet-4-6"
}
```

Changing the model requires a Ring0 restart (toggle off/on).

### Ring0 Event Rules

Events sent to Ring0 (session state changes, second screen events) are configurable via `~/.vibr8/ring0-events.json5`:

```json5
{
  "rules": [
    // Custom template for state changes, collapsed in UI
    {
      "match": { "type": "session_state_change" },
      "template": "Session ${evt.session} changed: ${evt.transition}",
      "summary": "${evt.session}: ${evt.transition}",
      "ui": "collapsed"
    },
    // Hide second screen events from Ring0 but show in UI
    { "match": { "type": "second_screen_*" }, "send": false },
    // Catch-all
    { "match": { "type": "*" } }
  ]
}
```

Rules are first-match-wins with glob pattern matching. Options: `send` (submit to LLM), `template`/`summary` (text formatting with `${evt.field}` interpolation), `ui` (`"visible"`, `"collapsed"`, `"hidden"`).

## Second Screen

Pair external displays (TVs, tablets, spare monitors) to show content pushed by Ring0 or mirrored from sessions.

### Setup

1. Open `/second-screen` on the display device
2. A pairing code appears on screen
3. Ring0 pairs it: "pair second screen, code 123456"

### Content Types

| Type | Description |
|---|---|
| `markdown` | Rendered markdown |
| `image` | URL or base64-encoded image |
| `file` | Text file viewer |
| `pdf` | URL or base64-encoded PDF |
| `html` | Raw HTML |
| `session` | Live mirror of a session's chat |
| `desktop` | Stream a remote desktop |
| `home` | Return to default Ring0 view |

### Display Settings

- **Scale** — Adjust font size (absolute 0.5–3.0 or relative delta)
- **TV-safe mode** — Add padding for TV bezels (configurable percentage)
- **Dark mode** — Independent dark/light toggle per screen

## Voice Commands

vibr8 supports voice input via WebRTC. Speech is transcribed by Whisper (with speculative decoding via distil-large-v3) + Silero VAD and routed to the active session (or Ring0 if enabled).

### Background Voice via the vibr8 Android App

The companion vibr8 Android app keeps voice working **in the background**: you can switch to other apps on your phone — browse, message, drive with maps up — and still issue voice commands to vibr8. The mic stays connected over WebRTC even when the vibr8 app isn't on screen, so guard-word commands ("vibr8 …"), Ring0 routing, and note mode all keep working while you use the phone for something else. Pair with `vibr8 guard` so it only acts on intentional, guard-prefixed speech.

### Guard Word

The guard word is **"vibr8"** (also recognized as **"vibrate"**). It serves two purposes:

1. **Command prefix** — When followed by a known command, the guard word and command are stripped and the command is executed.
2. **Guard mode gate** — When guard mode is enabled, only transcripts containing the guard word are accepted (others are silently discarded).

If the guard word appears but is **not** followed by a known command, the entire transcript passes through **unmodified**. This allows natural speech like "check the vibr8 session" to arrive intact.

When a command **is** matched, any text spoken *before* the guard word is submitted as separate input first, then the command executes. For example, "finish this up vibr8 guard" submits "finish this up" then enables guard mode.

### Commands

All commands are prefixed with the guard word (e.g., "vibr8 guard").

| Command | Effect |
|---------|--------|
| `done` | End the active voice mode (e.g., note mode). Always recognized, even inside voice modes. |
| `off` | Disconnect audio entirely. |
| `guard` | Enable guard mode — only guard-prefixed speech is accepted. |
| `listen` | Disable guard mode — all speech is accepted. |
| `quiet` | Mute TTS — the agent stops speaking responses aloud. |
| `speak` | Unmute TTS — resume spoken responses. |
| `ring zero on` | Enable Ring0 meta-agent. All voice routes to Ring0. |
| `ring zero off` | Disable Ring0. Voice routes to the active session. |
| `note` | Enter note mode — speech is accumulated silently until "vibr8 done". |
| `node {name}` | Switch the active remote node for Ring0 routing. |

### Escape Sequences

These are treated as commands (guard word is stripped) but produce transformed output:

| Escape | Result | Use case |
|--------|--------|----------|
| `vibr8 vibrate ...` | `vibrate ...` | Say the literal word "vibrate" |
| `vibr8 app ...` | `vibr8 ...` | Say "vibr8" literally when a command would otherwise intercept |

### Note Mode

Say **"vibr8 note"** to enter note mode:

- All speech is silently accumulated (not submitted to any session).
- Ring0's TTS is muted so it doesn't talk over dictation.
- Only **"vibr8 done"** exits — all other speech is captured as fragments.
- Text before "vibr8 done" in the final transcript is added as a last fragment.

On exit, the accumulated note is submitted as a `[voice note]` message. If Ring0 is enabled, a `[note_mode ended]` review message follows so Ring0 can summarize anything that happened during dictation. If audio disconnects during note mode, a `[voice note interrupted]` message is submitted instead.

### Guard Mode

When guard mode is **enabled** (default), transcripts without the guard word are discarded. When **disabled** ("vibr8 listen"), all speech is submitted.

The guard word check is independent of command matching — "check the vibr8 session" passes the guard check (word is present) even though no command matches (text passes through unmodified).

## CLI Tools

### User Management

vibr8 supports password authentication. When enabled, API and WebSocket endpoints require a session cookie except for explicit login and pairing/bootstrap paths.

```bash
# Add a user (prompts for password)
uv run python -m server.manage_users add <username>

# List all users
uv run python -m server.manage_users list

# Remove a user
uv run python -m server.manage_users remove <username>
```

Create at least one user before running an Internet-accessible server. If no users exist, vibr8 refuses to start unless `VIBR8_ALLOW_NO_AUTH=1` is set. Explicit no-auth mode binds to loopback unless `VIBR8_ALLOW_PUBLIC_NO_AUTH=1` is also set.

> **Caveat:** loopback bind blocks direct external connections, but it does **not** protect against a reverse proxy (nginx, Caddy, autossh tunnel terminator, etc.) running on the same host that forwards traffic to localhost. If you put any proxy in front of vibr8, always run with auth enabled regardless of bind host.

### Voice Replay

Debug voice transcription issues by replaying recorded audio segments through the STT pipeline.

```bash
# List recent voice segments for a user
uv run python -m server.voice_replay --user <username> --list

# Replay a specific segment
uv run python -m server.voice_replay --user <username> --segment <segment_id>
```

Voice logs are stored at `~/.vibr8/data/voice/logs/{username}/` with recordings, segments, and an index file.

## Configuration

### Configuration Directory

vibr8 stores configuration in `~/.vibr8/`:

| File / Directory | Description |
|---|---|
| `users.json` | User credentials (bcrypt-hashed). Presence enables auth. |
| `ring0.json` | Ring0 config: `enabled`, `sessionId`, `model` |
| `ring0-events.json5` | Ring0 event routing rules |
| `nodes.json` | Registered remote nodes |
| `artifacts.json` | Persistent artifacts published by sessions / Ring0 |
| `envs/` | Environment profiles (named sets of env vars) |
| `sessions/` | Persisted session data (atomic writes) |
| `worktrees/` | Worktree-to-session mappings |
| `worktrees.json` | Worktree tracking metadata |
| `ice-servers.json` | STUN/TURN server config for WebRTC (optional) |
| `secret.key` | HMAC signing key for auth tokens |
| `models/wespeaker-bsrnn-vox1/` | WeSep BSRNN checkpoint for TSE (optional) |
| `data/voice/logs/` | Voice recordings and segment logs |
| `data/voice/fingerprints/` | Speaker fingerprint v3 profiles (SpeechBrain + WeSpeaker embeddings) |

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `3456` | Backend server port |
| `VIBR8_HOST` | `0.0.0.0` with auth, `127.0.0.1` in explicit no-auth mode | Backend bind host |
| `VIBR8_ALLOW_NO_AUTH` | — | Required to start without `~/.vibr8/users.json`; intended for local development only |
| `VIBR8_ALLOW_PUBLIC_NO_AUTH` | — | Also required to bind a no-auth server to a non-loopback host |
| `VIBR8_TRUST_PROXY` | — | Set to `1` only behind a trusted reverse proxy so rate limits use `X-Forwarded-For`/`Forwarded`; IPv6 clients are bucketed by `/64` |
| `VIBR8_DISABLE_SELF_NODE` + `VIBR8_ALLOW_LEGACY_IN_PROCESS` | — | Set both to `1` only for isolated development/tests that need the legacy in-process node path |
| `NODE_ENV` | — | Set to `production` to serve built frontend |
| `VIBR8_TTS_ENGINE` | `kokoro` | TTS engine: `kokoro` (local) or `openai` (cloud) |
| `VIBR8_TTS_VOICE` | `af_sarah` | TTS voice name |
| `VIBR8_TTS_SPEED` | `1.0` | TTS playback speed |
| `OPENAI_API_KEY` | — | Required when using OpenAI TTS |
| `VIBR8_WESEP_DIR` | — | Override location of the WeSep BSRNN checkpoint (TSE) |

For HTTPS (required for WebRTC on non-localhost), place `key.pem` and `cert.pem` in `certs/`.

## Architecture

```
Browser (React 19 + WebRTC)               Android app (background voice)
    │                                         │
    ├── WebSocket (NDJSON) ──► WsBridge ──► Claude Code CLI subprocess
    │                              ├──────► Codex app-server (JSON-RPC stdio)
    │                              ├──────► OpenCode (NDJSON stdio)
    │                              └──────► Hermes (ACP JSON-RPC stdio)
    │
    ├── RTCPeerConnection ──► WebRTCManager
    │                              ├── AsyncSTT (Whisper + speculative decoding)
    │                              ├── Speaker Gate (SpeechBrain ECAPA, multi-embedding)
    │                              ├── TSE — optional (WeSep BSRNN, GPU-only)
    │                              └── QueuedAudioTrack (Kokoro / OpenAI TTS)
    │
    ├── Viewer Pane ◄── Artifacts (persistent content items)
    │
    └── Second Screen ──► WebSocket (content push, session mirror)

Remote Nodes ──► WebSocket tunnel ──► Hub
    ├── CliLauncher (spawns Claude / Codex / OpenCode / Hermes locally)
    ├── DesktopWebRTC (screen capture + input injection)
    └── Ring0 (optional, local MCP server)

Computer-Use Pipeline:
    UITarsAgent ──► VLM inference (UI-TARS 7B, int4)
                      ──► Action parsing
                      ──► DesktopTarget.inject() (WebRTC data channel)
```

**Backend** — Python/aiohttp with 40+ REST endpoints for sessions, git, filesystem, environments, nodes, artifacts, voice, and WebRTC signaling. Coding-agent sessions spawn either a Claude CLI subprocess (WebSocket sdk-url) or a stdio adapter (`codex_adapter.py`, `opencode_adapter.py`, `hermes_adapter.py`). All four backends emit the same normalized browser-facing message types.

**Frontend** — React 19, TypeScript, Tailwind CSS 4, Zustand state management. Code viewing with CodeMirror, file trees with react-arborist, terminal via xterm.js. Sessions, the viewer pane, and remote desktops share the main pane via resizable splits.

## Tech Stack

**Backend:** Python 3.11+, aiohttp, aiortc, PyTorch, Whisper (with distil-large-v3 speculative decoding), Silero VAD, SpeechBrain (ECAPA gate), WeSpeaker + WeSep BSRNN (vendored, for TSE), Kokoro, Transformers, BitsAndBytes (UI-TARS int4 quantization)

**Adapter protocols:** WebSocket (Claude SDK), Codex app-server JSON-RPC, OpenCode NDJSON, Hermes Agent Client Protocol (ACP)

**Frontend:** React 19, TypeScript, Vite, Tailwind CSS 4, Zustand, CodeMirror, xterm.js, react-arborist

## License

[Apache 2.0](LICENSE)
