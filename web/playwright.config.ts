import { defineConfig, devices } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Isolated HOME so the test never reads/writes your real ~/.vibr8.
// Empty users.json => AuthManager.enabled = false => no login wall.
const TEST_HOME = path.resolve(__dirname, ".e2e-home");
fs.mkdirSync(TEST_HOME, { recursive: true });

// Use non-default ports so a live dev server on 3456/5174 doesn't
// collide with the test fixture.
const TEST_BACKEND_PORT = "13456";
const TEST_FRONTEND_PORT = "15174";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],

  use: {
    baseURL: `http://127.0.0.1:${TEST_FRONTEND_PORT}`,
    trace: "retain-on-failure",
    actionTimeout: 10_000,
  },

  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],

  webServer: [
    {
      command: "uv run python -m server.main",
      cwd: path.resolve(__dirname, ".."),
      url: `http://127.0.0.1:${TEST_BACKEND_PORT}/api/auth/me`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        HOME: TEST_HOME,
        PORT: TEST_BACKEND_PORT,
        // Legacy in-process mode skips the self-node subprocess spawn,
        // keeping the test boot deterministic and fast.
        VIBR8_DISABLE_SELF_NODE: "1",
        // Skip GPU model preloads (~30s combined) — the test doesn't
        // exercise voice / TTS, just the browser→WsBridge WS path.
        VIBR8_FAST_STARTUP: "1",
        // Force plain HTTP so the test fixture matches Vite proxy
        // settings (VITE_DISABLE_HTTPS=1) regardless of whether certs/
        // exists for the user's regular dev runs.
        VIBR8_DISABLE_TLS: "1",
        // Explicitly opt into no-auth mode for the test fixture: HOME
        // points at an empty tmpdir so users.json doesn't exist, and
        // post-PR #4 the server otherwise refuses to start. The default
        // bind host without VIBR8_ALLOW_PUBLIC_NO_AUTH is loopback, which
        // is exactly what the test wants.
        VIBR8_ALLOW_NO_AUTH: "1",
        VIBR8_LOG_FILE: path.join(TEST_HOME, "server.log"),
        // Stub `claude` first on PATH so launcher.spawn() succeeds
        // without invoking the real CLI (which would cost API calls and
        // need the version pin maintained).
        PATH: `${path.resolve(__dirname, "tests/e2e/stubs")}:${process.env.PATH ?? ""}`,
      },
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      command: `bun run dev -- --port ${TEST_FRONTEND_PORT} --host 127.0.0.1`,
      cwd: __dirname,
      url: `http://127.0.0.1:${TEST_FRONTEND_PORT}`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        VITE_BACKEND_HOST: "127.0.0.1",
        VITE_BACKEND_PORT: TEST_BACKEND_PORT,
        VITE_DISABLE_HTTPS: "1",
      },
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
});
