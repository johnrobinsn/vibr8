# hello-node

Minimum viable vibr8 node. Implements just `ui/v1` — a REST endpoint
and a shared WebSocket counter. That's it.

## Files

- `hello_node.py` — the entire node in one file (~200 lines).
- `ui/index.html` — the vended UI, no build step.

## Run

```bash
uv run --with aiohttp python hello_node.py \
  --hub ws://127.0.0.1:4456 \
  --api-key sk-node-... \
  --name hello \
  --port 4470
```

See `../README.md` for how to mint an API key against a hub.

Once the node registers, open the hub in your browser, pick `hello`
from the node picker, and the UI in `ui/index.html` will render inside
the shell's iframe. The "Ping" button hits `GET /api/ping` (proxied),
and the counter is shared over `WS /ws/counter` (proxied).

## The four handlers you have to write

Everything the hub sends is one of four message types on the tunnel:

| type | when | what to do |
|---|---|---|
| `http_request` | a browser hit `/nodes/{id}/ui/...` or `/nodes/{id}/api/...` | replay it against your local server, return status/headers/body |
| `ws_open` | a browser opened `/nodes/{id}/ws/...` | connect to your local WS, remember the channel |
| `ws_data` | frame on an open channel | forward to your local WS |
| `ws_close` | either side closed | tear down the channel |

Look at `_on_http_request` / `_on_ws_open` / `_on_ws_data` /
`_on_ws_close` in `hello_node.py`. They're each ~10-30 lines. That's
the entire `ui/v1` obligation.

## Testing without the hub

The node's local server runs on `127.0.0.1:{port}` unconditionally.
Open `http://127.0.0.1:4470/ui/` directly and it works — the same code
serves both paths because the UI resolves API/WS URLs relative to
whatever prefix it's mounted under.
