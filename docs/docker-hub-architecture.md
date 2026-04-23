# Docker Hub Architecture Decisions

## Overview

The Docker hub runs vibr8's web server and ML inference (STT, VLM) in a container. The host's actual desktop, CLI sessions, and Ring0 are served by a **native node** — a standard vibr8 remote node installed on the host via `install-node.sh`.

The native node is mechanically identical to any remote node. It connects to the hub via outbound WebSocket tunnel. The only difference is that it happens to be on the same machine as the hub.

## Decisions

### Docker as compute head

The Docker container runs:

- aiohttp web server (frontend + API)
- ML inference (Whisper STT, Silero VAD, UI-TARS VLM)
- WebRTC audio (voice input/output)
- Optional virtual desktop (Xvfb + XFCE, for headless environments)

It does **not** run CLI sessions or Ring0 for the host. Those belong to the native node.

### Native node for host desktop

The host runs a full native node via `install-node.sh`. This gives the hub access to:

- Host desktop streaming (screen capture + input injection via WebRTC)
- CLI sessions (Claude Code, Codex — spawned locally on the host)
- Ring0 (local MCP server on the host)

```bash
vibr8-hub run                    # Docker: server, voice, ML
vibr8-node install-native        # Native: host desktop, CLI, Ring0
```

### Ring0 routing

Voice transcripts route to the native node's Ring0 via the WebSocket tunnel, same as any remote node. The voice path is:

```
Browser → Docker (WebRTC → STT → transcript) → native node Ring0 (via tunnel)
```

The extra hop through the tunnel is accepted. It's the same path used for all remote nodes.

### CLI session forwarding

Sessions created with the native node's `nodeId` are forwarded via the tunnel. The native node spawns the CLI locally. This is identical to how remote node sessions work — the native node IS a remote node.

### No auto-fallback

If the native node is offline, it shows as offline in the UI. There is no automatic fallback to the virtual desktop node inside the container. This matches the behavior of any remote node going offline.

### Node identity

The native node appears in the UI with a special label (e.g., "hub node") to distinguish it from generic remote nodes. Mechanically, it is a remote node — the label is purely cosmetic.

### Setup-time choice

Whether to use the virtual desktop (inside Docker) or native node (on the host) is a setup-time decision. There is no runtime toggle to switch between them.

## Open Gaps

### SSL trust with self-signed certs

When the hub uses a self-signed certificate (the default), remote nodes connecting via `wss://` will fail SSL verification. This is **not** hub-specific — it affects all remote nodes connecting to any hub with a self-signed cert.

The fix belongs in the general node installer and connection code:

- An `--insecure` or `--skip-ssl-verify` flag on `install-node.sh`
- Or cert pinning during node setup (node stores the hub's cert fingerprint at install time)

### STT model preload in Docker

`torch.hub.load()` in `server/stt.py` prompts for interactive trust confirmation. In a container with no TTY, this throws `EOFError`. Fix: add `trust_repo=True` to the call.

### Host desktop on non-Linux

On macOS and Windows, Docker runs in a VM — the container cannot access the host display. The native node approach solves this: the node runs natively on the host and streams the desktop to the hub via WebRTC. No special Docker display passthrough needed.

See `docs/docker-hub-gaps.md` for the full gap analysis.
