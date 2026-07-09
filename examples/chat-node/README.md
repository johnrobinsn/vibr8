# chat-node

A vibr8 node that runs an LLM chat. Text or voice → LLM → answer
appears in the UI and gets spoken by the hub. No Ring0, no sessions,
no CLIs — just a turn loop.

## What it demonstrates

- **`ui/v1`** — same plumbing as `hello-node`. See that example first.
- **`events/v1`** — voice input arrives as a `transcript` tunnel
  message (same shape whether the user spoke or typed) and TTS is
  requested by emitting `speak`. The `busy` flag drives hub-side
  thinking indicators.

## Files

- `chat_node.py` — the entire node (~300 lines).
- `ui/index.html` — chat UI, no build step.

## Run

```bash
LLM_API_KEY=sk-... \
LLM_URL=https://api.openai.com/v1/chat/completions \
LLM_MODEL=gpt-4o-mini \
  uv run --with aiohttp python chat_node.py \
    --hub ws://127.0.0.1:4456 \
    --api-key sk-node-... \
    --name chat \
    --port 4471
```

`LLM_URL` accepts any OpenAI-compatible endpoint — swap in your local
Ollama, LM Studio, Groq, together.ai, Anthropic's `/v1`, etc.

See `../README.md` for how to mint the `--api-key` for the hub.

## The turn loop

Everything routes to a single method:

```python
async def _turn(self, user_text: str) -> None:
    self._history.append({"role": "user", "content": user_text})
    await self._push_to_browser({"role": "user", "content": user_text})

    await self._busy(True)             # ← shell shows "thinking"
    reply = await llm_reply(self._history)
    await self._busy(False)

    self._history.append({"role": "assistant", "content": reply})
    await self._push_to_browser({"role": "assistant", "content": reply})
    await self._speak(reply)           # ← hub does TTS
```

Two entry points funnel into it:

- `POST /api/chat` from the browser (user typed).
- `transcript` message on the tunnel (user spoke — the hub already did
  STT and applied guard-word logic, so this is clean text).

That's the whole design.

## What's *not* here (on purpose)

- **Ring0 / meta-agent** — this node doesn't route messages to other
  sessions or fire scheduled tasks. Voice goes straight to the LLM.
- **Per-user history** — the whole conversation is one list on the
  node process. Real deployments would key by client, persist to disk,
  and prune.
- **Streaming** — `llm_reply` is one blocking call. Turning on
  streaming means switching to SSE from the LLM, forwarding partials
  over `/ws/chat`, and emitting `speak` per sentence (or when the
  reply completes). All node-land, not contract.
- **Tools** — no MCP, no function calls. If you want tools, they run
  entirely inside this file — the hub never sees them.

## Testing without voice

Register the node, open the hub, pick `chat` from the picker, and
type. The turn loop is the same as when voice is used; you just don't
exercise the `transcript` path.
