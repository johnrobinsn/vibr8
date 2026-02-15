.PHONY: dev dev-api dev-frontend build test test-py test-frontend install

# Run both Python backend and Vite frontend
dev:
	@trap 'kill 0' EXIT; \
	uv run python -m server.main & \
	cd web && bun run dev & \
	wait

# Run just the Python backend
dev-api:
	uv run python -m server.main

# Run just the Vite frontend
dev-frontend:
	cd web && bun run dev

# Build frontend for production
build:
	cd web && bun run build

# Run all tests
test: test-py test-frontend

# Run Python tests
test-py:
	uv run pytest server/tests/ -v

# Run frontend tests
test-frontend:
	cd web && bun run test

# Install dependencies
install:
	uv sync
	cd web && bun install
