.PHONY: dev dev-api dev-frontend build test test-all test-core test-py-core test-py test-desktop test-webrtc test-frontend test-e2e install docker-hub-build docker-hub-run

# Run both Python backend and Vite frontend
dev:
	@trap 'kill 0' EXIT; \
	( while true; do \
		uv run python -m server.main; \
		rc=$$?; \
		if [ $$rc -ne 75 ]; then break; fi; \
		echo "[dev] Server restart requested, relaunching..."; \
		sleep 1; \
	done ) & \
	cd web && bun run dev & \
	wait

# Run just the Python backend
dev-api:
	@while true; do \
		uv run python -m server.main; \
		rc=$$?; \
		if [ $$rc -ne 75 ]; then break; fi; \
		echo "[dev] Server restart requested, relaunching..."; \
		sleep 1; \
	done

# Run just the Vite frontend
dev-frontend:
	cd web && bun run dev

# Build frontend for production
build:
	cd web && bun run build

# Run the default fast test suite
test: test-core

# Run all currently defined default suites. Frontend tests are intentionally
# explicit because they need separate stabilization work before they can be a
# reliable gate.
test-all: test-core test-frontend

# Run Python tests that do not require optional media/ML/device dependencies.
# Safe to run alongside a live vibr8 instance: these tests use isolated ports,
# mocks, or in-memory aiohttp apps rather than the default dev server ports.
test-core: test-py-core

test-py-core:
	uv run --extra dev pytest -v -m "not (desktop or webrtc)" server/tests

# Run all Python tests that do not require frontend tooling. Optional suites
# install their declared extras and remain isolated from the live dev ports.
test-py: test-py-core test-desktop test-webrtc

# Optional desktop/media tests.
test-desktop:
	uv run --extra dev --extra desktop pytest -v \
		server/tests/test_screen_capture.py \
		server/tests/test_video_track.py

# Optional WebRTC peer tests.
test-webrtc:
	uv run --extra dev --extra desktop pytest -v \
		server/tests/test_webrtc_agent_peer.py

# Run frontend tests
test-frontend:
	cd web && bun run test

# Run end-to-end smoke tests (Playwright + real backend + Vite)
# Boots an isolated stack on ports 13456/15174 — safe to run alongside
# a live dev server on 3456/5174.
test-e2e:
	cd web && bun run test:e2e

# Install dependencies
install:
	uv sync
	cd web && bun install

# Docker hub image
docker-hub-build:
	bin/vibr8-hub build

docker-hub-run:
	bin/vibr8-hub run --defaults --gpu
