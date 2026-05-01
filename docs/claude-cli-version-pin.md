# Claude CLI Version Pin

## Symptom

Every Claude Code session (Ring0 and dev sessions alike) exits immediately with code=1 on spawn. `server.log` shows:

```
ERROR server.cli_launcher: [session:<id>:stderr] Error: --sdk-url rejected:
  host "localhost" is not an approved Anthropic endpoint.
  This flag is reserved for Remote Control worker processes connecting to Anthropic's backend.
INFO  server.cli_launcher: Session <id> exited (code=1)
```

The browser shows sessions stuck "not connected"; the UI accepts prompts but no model response ever arrives because the CLI worker never starts.

## Root Cause

vibr8 launches the Claude Code CLI as a worker subprocess and points it at vibr8's own WebSocket bridge:

```
claude --sdk-url wss://localhost:3456/ws/cli/{session_id} --print --output-format stream-json ...
```

**Claude CLI 2.1.123 introduced a host-allowlist check** that rejects `--sdk-url` for any non-Anthropic host. The flag is now reserved for Remote Control worker processes connecting to Anthropic's backend.

The Claude CLI auto-updates silently in the background, so a working vibr8 install can break on its own with no user-initiated change.

## Workaround — Pin to a Pre-Restriction Version

Older versions remain on disk under `~/.local/share/claude/versions/`. **2.1.119** is a known-working pre-restriction version (others ≤ 2.1.122 are also fine):

```bash
ln -sf ~/.local/share/claude/versions/2.1.119 ~/.local/bin/claude
claude --version  # verify: 2.1.119 (Claude Code)
```

After repointing, refresh the browser. vibr8 auto-relaunches CLI subprocesses on browser-reconnect, so existing sessions recover without a server restart.

If 2.1.119 isn't on disk, list available versions and pick the highest one that's `≤ 2.1.122`:

```bash
ls ~/.local/share/claude/versions/
```

## Reproducing

```bash
# Confirm the failure mode
claude --sdk-url wss://localhost:3456/ws/cli/test --print -p 'hi' 2>&1 | head -5
# 2.1.123+ → "Error: --sdk-url rejected: host \"localhost\" is not an approved Anthropic endpoint."
# 2.1.119  → connects (or fails on something else, but not the host check)
```

## Long-Term Fix

The pin is a holding pattern. Anthropic clearly does not want third-party tools using `--sdk-url`, so any future auto-update could re-break vibr8.

Options worth investigating:

- **Stdio piping** — drive the CLI via stdin/stdout instead of `--sdk-url`. The CLI already supports `--input-format stream-json` and `--output-format stream-json`; the WebSocket is only needed because vibr8 wants to bridge the CLI to a browser, and that bridging can happen in the parent process.
- **Official Agent SDK** — `@anthropic-ai/sdk` / `anthropic` Python SDK. Reimplements session/streaming/tool-use in vibr8's own process; loses CLI-specific features (slash commands, MCP config, agents) unless those are also reimplemented.
- **Self-host the worker endpoint** — stand up a TLS reverse proxy that presents an Anthropic-allowlisted hostname locally. Brittle and probably violates ToS.

Track this as an architectural decision; pinning is fine for now but should not be the long-term answer.

## See Also

- `server/cli_launcher.py` — spawns the CLI subprocess with `--sdk-url`
- `server/ws_bridge.py` — the WebSocket bridge the CLI is pointed at (`/ws/cli/{session_id}`)
