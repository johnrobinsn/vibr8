# vibr8 node examples

Two standalone reference implementations of the vibr8 hub–node contract
(see `docs/hub-node-contract-v1.md`). They depend on nothing in this
repo — only `aiohttp` and the Python standard library — and are meant
to be read end-to-end as a template for writing your own node.

|  | file | contract flags | dependencies |
|---|---|---|---|
| `hello-node/` | `hello_node.py` (~200 lines) | `ui/v1` | `aiohttp` |
| `chat-node/` | `chat_node.py` (~300 lines) | `ui/v1`, `events/v1` | `aiohttp` + any OpenAI-compatible chat API |

Neither example uses Ring0, sessions, CLIs, or any other vibr8 feature.
They exist purely to demonstrate: **register → tunnel → vend UI → talk
to your own vended API/WS**.

## Before you run either

You need a running vibr8 hub and an API key for the node. Against a
local dev hub (see the parent repo's `dev-launch.sh`):

```bash
# Mint an API key against the hub's node registry. Replace VIBR8_HUB_DATA_DIR
# with your hub's data dir if you're not running dev-launch.sh.
VIBR8_HUB_DATA_DIR=~/.vibr8-dev uv run python - <<'PY'
from server.node_registry import NodeRegistry
key, _ = NodeRegistry().generate_api_key(name="my-example-node")
print(key)
PY
```

Copy the printed `sk-node-...` key.

If your hub is on a different host, replace `ws://127.0.0.1:4456` in the
run commands below with your hub's WebSocket URL.

## Running

```bash
# Hello world node
cd examples/hello-node
uv run --with aiohttp python hello_node.py \
  --hub ws://127.0.0.1:4456 \
  --api-key sk-node-... \
  --name hello \
  --port 4470

# Chat node (needs an LLM endpoint; defaults to OpenAI, but any
# OpenAI-compatible endpoint works — Anthropic /v1, Ollama, LM Studio,
# vLLM, groq, together.ai, etc.)
cd examples/chat-node
LLM_API_KEY=sk-... \
LLM_URL=https://api.openai.com/v1/chat/completions \
LLM_MODEL=gpt-4o-mini \
  uv run --with aiohttp python chat_node.py \
    --hub ws://127.0.0.1:4456 \
    --api-key sk-node-... \
    --name chat \
    --port 4471
```

Then open the hub in your browser, pick the node from the picker in
the shell strip, and the vended UI will render inside the iframe.

## What each example teaches

**`hello-node`** — the minimum viable node. Registers, opens the
tunnel, answers `http_request` / `ws_open` / `ws_data` / `ws_close`
from the hub against a local aiohttp server. Its UI has a "Ping" button
(REST) and a shared counter (WebSocket). Read this first to internalize
the plumbing.

**`chat-node`** — adds voice via `events/v1`. Receives `transcript`
messages from the hub and treats them the same as text typed into the
UI; emits `speak` for TTS and `busy` while the LLM is thinking. Ring0,
session persistence, permission prompts — none of that is here. When
you want your own node with its own persona, its own tools, its own
UI, start from this file.

## The narrow surface

A node must:

1. `POST /api/nodes/register` with `{name, apiKey, capabilities: {protocolVersion, contract, ...}}`.
2. Open a WSS tunnel at `/ws/node/{id}?apiKey=...`, NDJSON both ways.
3. Handle four hub → node message types for `ui/v1`: `http_request`,
   `ws_open`, `ws_data`, `ws_close`. That's all the plumbing there is.
4. If you flag `events/v1`, handle `transcript` and optionally emit
   `speak` / `busy` / `attention`.

Everything else — the frontend framework, storage, backends you spawn,
the shape of your `/api/*` — is your call. The hub is a router.
