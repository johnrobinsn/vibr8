# Docker Hub — Remaining Gaps for Bare-Metal Replacement

## Context

The Docker hub infrastructure is built and smoke-tested (Dockerfiles, entrypoint, CLI manager, SSL, auto-generated creds). This document covers the remaining gaps discovered during smoke testing and migration audit for replacing the current bare-metal vibr8 installation with the Docker hub.

## Critical

### 1. STT model preload crashes in Docker

`torch.hub.load()` in `server/stt.py:123` prompts for interactive trust confirmation on first load. In a container with no TTY, this throws `EOFError` and STT never initializes — no voice at all.

**Fix:** Add `trust_repo=True` to the call. One line.

### 2. OPENAI_API_KEY for TTS

`server/tts.py` loads `OPENAI_API_KEY` via `python-dotenv` from a `.env` file at the repo root. In Docker, no `.env` exists at `/app/`. The `--openai-key` flag and host `$OPENAI_API_KEY` forwarding already work, but this needs to be documented clearly — TTS won't work without it.

## Medium

### 3. Host desktop streaming

The Docker hub has a virtual Xvfb desktop but can't stream the host's actual desktop. On macOS and Windows, Docker runs in a VM with no access to the host display at all. On Linux with X11 it's theoretically possible but fragile.

**Portable solution:** Run the existing native node agent on the host alongside the Docker hub. The node streams the host desktop to the hub via WebRTC. No new code — just a documented pattern:

```bash
vibr8-hub run                          # Docker: server, Ring0, voice, ML
vibr8-node install-native              # Native: host desktop streaming
```

A `--host-desktop` convenience flag (auto-launching a native node alongside the container) could be a future enhancement.

### 4. Data migration from bare-metal

Users replacing a bare-metal install need to migrate `~/.vibr8/` data into the Docker volume structure at `~/.vibr8-hub/vibr8/`.

**Fix:** Add a `vibr8-hub migrate` command:

```bash
vibr8-hub migrate [--from ~/.vibr8]
```

Copies: `users.json`, `secret.key`, `device-tokens.json`, `ring0.json`, `ring0-events.json5`, `ice-servers.json`, `nodes.json`, `envs/`, `ring0/` (memory, tasks, queue), `sessions/`, `data/voice/`.

## Low / Document Only

### 5. ADB/Android not available in Docker

Android device control via scrcpy requires the ADB binary and USB device access. Not installed in the Docker image, and USB passthrough requires `--privileged` or `--device` flags.

Android features require either running the hub bare-metal, or adding `--privileged` and installing ADB (not recommended for most users).

### 6. VIBR8_DATA_DIR wiring

Voice data path uses `VIBR8_DATA_DIR` env var (defaults to `~/.vibr8/data`). The Dockerfile sets it to `/data/voice` which is volume-mounted. Should verify `voice_logger.py` and `voice_profiles.py` write to the right place in Docker.
