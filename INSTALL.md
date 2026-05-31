# vibr8 Installation Guide

vibr8 is a web UI that manages multiple coding-agent backends (Claude Code, Codex, OpenCode, Hermes) in a single interface. It adds real-time voice control via WebRTC (Whisper STT, speaker fingerprinting, local TTS), a computer-use agent that drives desktop GUIs with a vision-language model, remote node orchestration, and a Ring0 meta-agent that supervises sessions hands-free. The fastest way to install it is to use a coding agent.

---

## System Requirements

### Hardware

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| CPU | 4 cores | 8+ cores |
| RAM | 8 GB | 16+ GB |
| Disk | 10 GB (base) | 40+ GB (with ML models) |
| GPU | — | NVIDIA with 8+ GB VRAM |

GPU VRAM breakdown by feature (all features load on demand):

| Feature | Models | VRAM | Disk |
|---------|--------|------|------|
| Speech-to-text (STT) | Whisper large-v3 + distil-large-v3 (fp16, speculative decoding) | ~4.5 GB | ~6 GB |
| Voice activity detection | Silero VAD (via torch.hub) | ~50 MB | ~100 MB |
| Speaker identification | SpeechBrain ECAPA-TDNN | ~100 MB | ~200 MB |
| Target speaker extraction (TSE) | WeSep BSRNN + WeSpeaker ECAPA | ~350 MB | ~500 MB |
| Text-to-speech (local) | Kokoro neural TTS | ~300 MB | ~400 MB |
| Computer-use VLM | UI-TARS 7B (int4 via BitsAndBytes) | ~4–5 GB | ~4 GB |

Voice-only stack: ~5 GB VRAM. Full stack (voice + VLM): ~10–12 GB VRAM.

Total model disk space: ~20–25 GB when all models are downloaded (cached at `~/.cache/huggingface/`).

### Operating System

Ubuntu 22.04+ or any modern Linux distribution. macOS works for the base server and frontend but GPU features (TSE, computer-use) require NVIDIA CUDA.

---

## Step 1: Set Up a Coding Agent

You need one coding agent installed and authenticated. Pick one:

### Option A: Claude Code (recommended)

1. Install Node.js 20+ if you don't have it: https://nodejs.org/en/download/

2. Install the CLI:
   ```bash
   npm install -g @anthropic-ai/claude-code
   ```

3. Authenticate (opens browser):
   ```bash
   claude
   ```

4. Verify it works:
   ```bash
   which claude && echo "OK"
   ```

**Documentation:** https://docs.anthropic.com/en/docs/claude-code

### Option B: Codex

1. Install Node.js 20+ if you don't have it: https://nodejs.org/en/download/

2. Install the CLI:
   ```bash
   npm install -g @openai/codex
   ```

3. Authenticate (requires OpenAI API key):
   ```bash
   codex
   ```

4. Verify it works:
   ```bash
   which codex && echo "OK"
   ```

**Repository:** https://github.com/openai/codex

---

## Step 2: Let the Agent Install vibr8

Give the rest of this document to your coding agent and let it handle the setup.

**With Claude Code:**

```bash
claude -p "Install vibr8 on this machine by following the instructions in this file. Clone the repo, install all dependencies, verify everything works. If this machine has an NVIDIA GPU, install the voice and computer-use extras too. Here are the instructions:" < INSTALL.md
```

Or start an interactive session and paste:

```
Read INSTALL.md and follow the Manual Installation Reference section to set up vibr8 on this machine.
Install all dependencies, clone the repo, and verify everything works.
If this machine has an NVIDIA GPU, install the voice and computer-use extras.
```

**With Codex:**

```bash
codex "Read INSTALL.md in this directory and follow the Manual Installation Reference to set up vibr8. Clone the repo, install deps, verify it works. Install GPU extras if an NVIDIA GPU is present."
```

The agent will work through system packages, Python, uv, Bun, the vibr8 repo, dependencies, and verification. It has all the information it needs in the reference section below.

When it's done, open http://localhost:5174 in your browser.

---

## Manual Installation Reference

Everything below is the complete step-by-step reference. Your coding agent reads this to know what to do. You can also follow it yourself if you prefer a manual install.

---

### 1. System Packages

```bash
sudo apt update
sudo apt install -y git curl build-essential ca-certificates gnupg
```

---

### 2. Python 3.11 or 3.12

vibr8 requires Python >=3.11, <3.13.

```bash
python3 --version
```

If needed:

```bash
sudo apt install -y python3.12 python3.12-venv python3.12-dev
```

---

### 3. Install uv (Python package manager)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc  # or restart shell
uv --version
```

**Documentation:** https://docs.astral.sh/uv/

---

### 4. Install Node.js 20 LTS

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
node --version   # Should print v20.x.x
```

**Documentation:** https://nodejs.org/en/download/

---

### 5. Install Bun

```bash
curl -fsSL https://bun.sh/install | bash
source ~/.bashrc  # or restart shell
bun --version
```

**Documentation:** https://bun.sh/docs/installation

---

### 6. NVIDIA GPU Setup (optional)

Skip this if the machine has no NVIDIA GPU or you only need the base coding-agent UI.

#### NVIDIA Driver

Install version 535 or newer:

```bash
sudo apt install -y nvidia-driver-535
# Reboot required after install
nvidia-smi  # Verify after reboot
```

#### CUDA Toolkit

Install CUDA 12.x from NVIDIA's official repository:

https://developer.nvidia.com/cuda-downloads

Example for Ubuntu 22.04:

```bash
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt update
sudo apt install -y cuda-toolkit-12-4
```

Add to `~/.bashrc`:

```bash
export PATH="/usr/local/cuda/bin:$PATH"
export LD_LIBRARY_PATH="/usr/local/cuda/lib64:$LD_LIBRARY_PATH"
```

Verify:

```bash
nvcc --version
```

PyTorch (installed via the voice/computer-use dependency groups) includes its own CUDA runtime. The system CUDA toolkit is primarily needed for compiling packages like `bitsandbytes`.

---

### 7. Install Coding-Agent CLIs

vibr8 auto-detects which CLIs are on PATH at runtime and greys out unavailable ones in the UI. You need at least one.

#### Claude Code

```bash
npm install -g @anthropic-ai/claude-code
claude  # Authenticate on first run
```

**Documentation:** https://docs.anthropic.com/en/docs/claude-code

#### Codex

```bash
npm install -g @openai/codex
codex  # Authenticate on first run (requires OpenAI API key)
```

**Repository:** https://github.com/openai/codex

#### OpenCode

```bash
go install github.com/sst/opencode@latest
# Or download a binary from the releases page
```

**Repository:** https://github.com/sst/opencode

#### Hermes

```bash
pip install hermes-agent
hermes  # Configure provider/model on first run
```

**Repository:** https://github.com/NousResearch/hermes-agent

---

### 8. Clone and Install vibr8

```bash
git clone https://github.com/johnrobinsn/vibr8.git
cd vibr8
```

#### Base install (no GPU features)

```bash
make install
```

This runs `uv sync` (Python deps) and `cd web && bun install` (frontend deps).

#### With voice processing (STT + TTS + speaker ID)

```bash
uv sync --extra voice
cd web && bun install
```

#### With computer-use VLM

```bash
uv sync --extra computer-use
cd web && bun install
```

#### With remote desktop streaming

```bash
uv sync --extra desktop
cd web && bun install
```

#### Everything (voice + desktop + computer-use)

```bash
uv sync --extra all
cd web && bun install
```

#### Dependency groups reference

| Group | What it adds |
|-------|-------------|
| *(base)* | aiohttp, numpy, bcrypt, mcp, httpx, kokoro (local TTS) |
| `voice` | torch, torchaudio, transformers (<5.0), speechbrain, scipy, onnxruntime, aiortc |
| `desktop` | aiortc, mss, pillow |
| `computer-use` | torch, transformers (<5.0), bitsandbytes, accelerate, mss, pillow |
| `all` | voice + desktop + computer-use |
| `dev` | pytest, pytest-asyncio |

**Important:** The `transformers>=4.46,<5.0` constraint is intentional. Transformers 5.x breaks Whisper speculative decoding. Do not upgrade past 4.x.

---

### 9. Run vibr8

#### Development mode

```bash
make dev
```

Starts:
- **Backend** on port 3456 (Python/aiohttp)
- **Frontend** on port 5174 (Vite dev server, proxies `/api` and `/ws` to the backend)

Open http://localhost:5174 in your browser.

#### Production mode

```bash
make build
NODE_ENV=production uv run python -m server.main
```

The app is available at http://localhost:3456 (single port).

#### All make commands

| Command | Description |
|---------|-------------|
| `make dev` | Run backend + frontend together |
| `make dev-api` | Backend only (port 3456) |
| `make dev-frontend` | Frontend only (port 5174, proxies to backend) |
| `make build` | Build frontend for production |
| `make test` | Run all tests |
| `make test-py` | Python tests (pytest) |
| `make test-frontend` | Frontend tests (vitest) |

---

### 10. Configuration

#### Authentication

Create at least one user before running an Internet-accessible server. If no users exist, vibr8 refuses to start unless `VIBR8_ALLOW_NO_AUTH=1` is set. Explicit no-auth mode binds to loopback unless `VIBR8_ALLOW_PUBLIC_NO_AUTH=1` is also set.

> **Caveat:** loopback bind blocks direct external connections, but it does **not** protect against a reverse proxy (nginx, Caddy, autossh tunnel terminator, etc.) running on the same host that forwards traffic to localhost. If you put any proxy in front of vibr8, always run with auth enabled regardless of bind host.

```bash
uv run python -m server.manage_users add <username>    # Add user (prompts for password)
uv run python -m server.manage_users list              # List users
uv run python -m server.manage_users remove <username>  # Remove user
```

#### HTTPS / TLS (optional)

Required for WebRTC voice on non-localhost. Browsers block `getUserMedia()` over plain HTTP on non-localhost origins.

Place certificate files in the `certs/` directory:

```bash
mkdir -p certs
cp /path/to/key.pem certs/key.pem
cp /path/to/cert.pem certs/cert.pem
```

The server auto-detects these files on startup and enables HTTPS.

For self-signed certs (development/testing):

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:4096 -keyout certs/key.pem -out certs/cert.pem \
  -days 365 -nodes -subj '/CN=localhost'
```

#### WebRTC ICE Servers (optional)

For WebRTC connections that traverse NAT or firewalls (e.g., accessing vibr8 from a phone on a different network), configure STUN/TURN servers. Create `~/.vibr8/ice-servers.json`:

```json
[
  {
    "urls": "stun:stun.l.google.com:19302"
  },
  {
    "urls": "turn:your-turn-server.example.com:3478",
    "username": "your-username",
    "credential": "your-credential"
  }
]
```

**STUN** (Session Traversal Utilities for NAT) helps peers discover their public IP. Google's public STUN server (`stun:stun.l.google.com:19302`) is free and sufficient for most cases.

**TURN** (Traversal Using Relays around NAT) relays media when direct peer-to-peer fails (symmetric NATs, strict firewalls). You need to run your own TURN server or use a hosted service. Popular open-source options:

- **coturn:** https://github.com/coturn/coturn
- **Pion TURN:** https://github.com/pion/turn

Without ICE server config, WebRTC only works on localhost or when both client and server are on the same LAN.

Alternatively, set the `ICE_SERVERS` environment variable to the same JSON array instead of using the config file.

#### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `3456` | Backend server port |
| `VIBR8_HOST` | `0.0.0.0` with auth, `127.0.0.1` in explicit no-auth mode | Backend bind host |
| `VIBR8_ALLOW_NO_AUTH` | — | Required to start without `~/.vibr8/users.json`; intended for local development only |
| `VIBR8_ALLOW_PUBLIC_NO_AUTH` | — | Also required to bind a no-auth server to a non-loopback host |
| `VIBR8_TRUST_PROXY` | — | Set to `1` only behind a trusted reverse proxy so rate limits use `X-Forwarded-For`/`Forwarded`; IPv6 clients are bucketed by `/64` |
| `VIBR8_DISABLE_SELF_NODE` + `VIBR8_ALLOW_LEGACY_IN_PROCESS` | — | Set both to `1` only for isolated development/tests that need the legacy in-process node path |
| `NODE_ENV` | — | Set to `production` to serve built frontend from `web/dist/` |
| `VIBR8_TTS_ENGINE` | `kokoro` | TTS engine: `kokoro` (local, no API key) or `openai` (cloud) |
| `VIBR8_TTS_VOICE` | `af_sarah` (kokoro) / `echo` (openai) | TTS voice name |
| `VIBR8_TTS_SPEED` | `1.0` | TTS playback speed multiplier |
| `OPENAI_API_KEY` | — | Required only when using OpenAI TTS (`VIBR8_TTS_ENGINE=openai`) |
| `VIBR8_WESEP_DIR` | — | Override location of the WeSep BSRNN checkpoint directory (for TSE) |
| `ICE_SERVERS` | — | JSON array of ICE server configs (alternative to `~/.vibr8/ice-servers.json`) |

#### Configuration Directory

vibr8 stores all persistent data in `~/.vibr8/`. Created automatically on first run.

| Path | Description |
|------|-------------|
| `~/.vibr8/users.json` | User credentials (bcrypt-hashed). Presence enables auth. |
| `~/.vibr8/ring0.json` | Ring0 meta-agent config: `enabled`, `sessionId`, `model` |
| `~/.vibr8/ring0-events.json5` | Ring0 event routing rules |
| `~/.vibr8/nodes.json` | Registered remote nodes |
| `~/.vibr8/artifacts.json` | Persistent artifacts published by sessions / Ring0 |
| `~/.vibr8/envs/` | Environment profiles (named sets of env vars) |
| `~/.vibr8/sessions/` | Persisted session data |
| `~/.vibr8/ice-servers.json` | STUN/TURN server config for WebRTC |
| `~/.vibr8/secret.key` | HMAC signing key for auth tokens (auto-generated) |
| `~/.vibr8/models/` | ML model checkpoints (e.g., WeSep BSRNN for TSE) |
| `~/.vibr8/data/voice/logs/` | Voice recordings and segment logs |
| `~/.vibr8/data/voice/fingerprints/` | Speaker fingerprint profiles |

---

### 11. Target Speaker Extraction — TSE (optional)

TSE isolates your voice from background speakers (TV, family in the room) before Whisper runs transcription. Requires an NVIDIA GPU and the WeSep BSRNN model checkpoint.

#### Get the BSRNN checkpoint

The checkpoint comes from the WeSep project's `bsrnn_ecapa_vox1` pre-trained model:

**WeSep repository:** https://github.com/wenet-e2e/wesep

Download the `avg_model.pt` checkpoint and place it at one of these locations (checked in order):

1. `$VIBR8_WESEP_DIR/avg_model.pt` (env var override)
2. `~/.vibr8/models/wespeaker-bsrnn-vox1/avg_model.pt` (recommended)
3. `~/.wesep/english/avg_model.pt` (WeSep default)

```bash
mkdir -p ~/.vibr8/models/wespeaker-bsrnn-vox1
# Copy or download avg_model.pt into this directory
cp /path/to/avg_model.pt ~/.vibr8/models/wespeaker-bsrnn-vox1/
```

#### Enable TSE

1. Enroll a speaker fingerprint in **Settings → Speaker ID** (captures both SpeechBrain and WeSpeaker embeddings automatically).
2. Flip **Enable TSE** on in the same settings panel.
3. The toggle is disabled if either the GPU or the BSRNN checkpoint is missing.

---

### 12. Pre-Download ML Models (optional)

Models download automatically from Hugging Face on first use, but you can pre-download them to avoid delays.

#### Whisper (speech-to-text)

Two models used together for speculative decoding:

| Model | Hugging Face ID | Size |
|-------|----------------|------|
| Whisper large-v3 (main) | `openai/whisper-large-v3` | ~3 GB |
| distil-large-v3 (assistant) | `distil-whisper/distil-large-v3` | ~1.5 GB |

```bash
uv run python -c "
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
AutoModelForSpeechSeq2Seq.from_pretrained('openai/whisper-large-v3')
AutoProcessor.from_pretrained('openai/whisper-large-v3')
AutoModelForSpeechSeq2Seq.from_pretrained('distil-whisper/distil-large-v3')
"
```

#### Silero VAD (voice activity detection)

| Model | Source | Size |
|-------|--------|------|
| Silero VAD | `snakers4/silero-vad` (torch.hub) | ~100 MB |

```bash
uv run python -c "
import torch
torch.hub.load('snakers4/silero-vad', 'silero_vad', onnx=False)
"
```

#### SpeechBrain ECAPA (speaker identification)

| Model | Source | Size |
|-------|--------|------|
| ECAPA-TDNN | `speechbrain/spkrec-ecapa-voxceleb` | ~200 MB |

```bash
uv run python -c "
from speechbrain.inference.speaker import EncoderClassifier
EncoderClassifier.from_hparams(source='speechbrain/spkrec-ecapa-voxceleb', savedir='/tmp/sb-ecapa-check')
"
```

#### UI-TARS (computer-use VLM)

| Model | Hugging Face ID | Size |
|-------|----------------|------|
| UI-TARS 7B DPO (int4) | `bytedance-research/UI-TARS-7B-DPO` | ~4 GB |

```bash
uv run python -c "
from transformers import AutoModelForCausalLM, AutoProcessor
AutoProcessor.from_pretrained('bytedance-research/UI-TARS-7B-DPO')
AutoModelForCausalLM.from_pretrained('bytedance-research/UI-TARS-7B-DPO')
"
```

#### Kokoro (local TTS)

Kokoro downloads its voice pack on first use:

```bash
uv run python -c "
from kokoro import KPipeline
KPipeline(lang_code='a')
"
```

---

### 13. Docker Hub Deployment

Alternative to bare-metal: run the full vibr8 hub in a single Docker container.

#### Docker prerequisites

1. **Docker Engine:** https://docs.docker.com/engine/install/

2. **NVIDIA Container Toolkit** (for GPU passthrough): https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html

   ```bash
   curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
     sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
   curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
     sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
     sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
   sudo apt update
   sudo apt install -y nvidia-container-toolkit
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo systemctl restart docker
   ```

#### Build and run

```bash
bin/vibr8-hub build

# One-liner — auto-generates admin credentials, auto-detects GPU
bin/vibr8-hub run --defaults --gpu

# With explicit credentials and API keys
bin/vibr8-hub run --admin-user john --admin-password s3cret --openai-key sk-...

# Lite mode (no virtual desktop, ~2 GB smaller image)
bin/vibr8-hub run --defaults --no-remote-desktop
```

The hub image includes: Python server, Ring0, Whisper voice pipeline, virtual display (Xvfb + XFCE4 + Chrome + VSCode), and computer-use VLM. Data persists in `~/.vibr8-hub/`.

#### Management commands

| Command | Description |
|---------|-------------|
| `bin/vibr8-hub help` | Full CLI reference |
| `bin/vibr8-hub status` | Container status |
| `bin/vibr8-hub logs -f` | Follow logs |
| `bin/vibr8-hub shell` | Open bash inside the container |
| `bin/vibr8-hub update` | Rebuild and recreate (preserves data) |
| `bin/vibr8-hub backup` | Backup persistent data |
| `bin/vibr8-hub restore` | Restore from backup |
| `bin/vibr8-hub warmup` | Pre-download ML models |
| `bin/vibr8-hub stop` | Stop the container |
| `bin/vibr8-hub start` | Start a stopped container |
| `bin/vibr8-hub restart` | Restart the container |
| `bin/vibr8-hub destroy` | Remove container (`--keep-data` to preserve volumes) |

---

### 14. Verify Your Installation

Run these checks to confirm each component works.

#### Backend starts

```bash
uv run python -m server.main &
sleep 3

curl -s http://localhost:3456/api/health
# Expected: {"status": "ok", ...}

curl -s http://localhost:3456/api/backends | python3 -m json.tool
# Expected: list of backends with "available": true/false for each

kill %1
```

#### Frontend builds

```bash
cd web && bun run build
# Expected: outputs to web/dist/ with no errors

cd web && bun run typecheck
# Expected: no TypeScript errors
```

#### Tests pass

```bash
make test
# Runs both Python (pytest) and frontend (vitest) tests
```

#### Coding-agent CLIs are on PATH

```bash
which claude   && echo "Claude Code: OK"
which codex    && echo "Codex: OK"
which opencode && echo "OpenCode: OK"
which hermes   && echo "Hermes: OK"
```

#### GPU is accessible (if applicable)

```bash
uv run python -c "import torch; print(f'CUDA available: {torch.cuda.is_available()}, Device: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"N/A\"}')"
```

#### Voice pipeline loads (if voice extras installed)

```bash
uv run python -c "
from server.stt import STT
print('STT module imported OK')
"
```

#### Full end-to-end smoke test

```bash
make dev &
sleep 5

curl -s http://localhost:3456/api/health
# Expected: {"status": "ok", ...}

curl -s -o /dev/null -w '%{http_code}' http://localhost:5174/
# Expected: 200

curl -s http://localhost:3456/api/sessions
# Expected: {"sessions": []}

curl -s http://localhost:3456/api/backends
# Expected: JSON with detected backends

kill %1
```

Open http://localhost:5174 in your browser. You should see the vibr8 home page with a "New Session" button. Click it, select an available backend, and create a test session.

---

### Troubleshooting

#### `make install` fails on torch/torchaudio

PyTorch wheels are large (~2 GB). If the download times out, install torch separately with a specific CUDA index:

```bash
uv pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
uv sync --extra voice
```

#### `bitsandbytes` fails to install or load

bitsandbytes requires CUDA headers to compile. Ensure the CUDA toolkit is installed (step 6 above) and `nvcc` is on your PATH:

```bash
nvcc --version
```

If it still fails, try installing a pre-built wheel:

```bash
uv pip install bitsandbytes --prefer-binary
```

#### Whisper model download is slow or hangs

Models download from Hugging Face on first use. See the pre-download section above. If Hugging Face is slow, set a mirror:

```bash
export HF_ENDPOINT=https://hf-mirror.com
```

#### WebRTC voice doesn't work on remote access

Browsers require HTTPS for microphone access on non-localhost origins. Set up TLS certificates and configure ICE servers if traversing NAT/firewall (see Configuration section above).

#### Bubblewrap (bwrap) sandbox failures with Codex

On Ubuntu 24.04+, AppArmor restricts unprivileged user namespaces, which prevents Codex from creating bubblewrap sandboxes. vibr8 auto-approves sandbox retry prompts and falls back to non-sandboxed execution. vibr8's own permission approval UI serves as the trust boundary in this case.

#### `transformers` version conflicts

vibr8 pins `transformers>=4.46,<5.0`. If another package pulls in Transformers 5.x, Whisper speculative decoding will break with import errors. Force the correct version:

```bash
uv pip install "transformers>=4.46,<5.0"
```

#### Port 3456 already in use

Another process is using the default port. Either stop it or run vibr8 on a different port:

```bash
PORT=8080 make dev
```

#### `make dev` exits immediately

Check the backend logs for errors. Common causes:
- Missing Python dependencies (run `make install` again)
- Port conflict (see above)
- Missing `web/node_modules/` (run `cd web && bun install`)
