import { test, expect, type APIRequestContext } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Where the test backend writes its logs (set in playwright.config.ts).
const SERVER_LOG = path.resolve(__dirname, "../../.e2e-home/server.log");

async function readServerLog(): Promise<string> {
  try {
    return await fs.promises.readFile(SERVER_LOG, "utf-8");
  } catch {
    return "";
  }
}

async function waitForLog(
  pattern: RegExp,
  timeoutMs = 15_000,
): Promise<string | null> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const log = await readServerLog();
    const m = log.match(pattern);
    if (m) return m[0];
    await new Promise((r) => setTimeout(r, 100));
  }
  return null;
}

async function createSession(
  request: APIRequestContext,
  name: string,
): Promise<string> {
  const resp = await request.post("/api/sessions/create", {
    data: { name, cwd: "/tmp", backendType: "claude" },
  });
  expect(resp.ok(), `create_session failed: ${resp.status()}`).toBeTruthy();
  const body = await resp.json();
  expect(body.sessionId, "no sessionId in create response").toBeTruthy();
  return body.sessionId as string;
}

test.describe("browser → bridge end-to-end (Playwright smoke)", () => {
  test("user_message sent via real browser WS reaches server's WsBridge", async ({
    page,
    request,
  }) => {
    const uniqueText = `e2e smoke ${Date.now()}`;
    const sessionId = await createSession(request, `e2e-${Date.now()}`);

    // Load the app so the page lives at the right origin (Vite proxy
    // is configured for /ws and /api). Don't rely on the React UI being
    // in any particular state — this test exercises the WS plumbing
    // (Vite ws-proxy → aiohttp → WsBridge), not the Composer.
    await page.goto("/");

    // Open the same WebSocket the Composer would, from inside the
    // browser context. This exercises the real:
    //   browser TCP → Vite dev /ws proxy → aiohttp /ws/browser → WsBridge
    // path. The matching pytest smoke covers the aiohttp → WsBridge
    // half without the Vite proxy; this one adds proxy coverage.
    const sendResult = await page.evaluate(
      async ({ sid, text }) => {
        const clientId = `e2e-client-${Date.now()}`;
        const url = `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/browser/${sid}?clientId=${clientId}`;
        return new Promise<{ ok: boolean; readyState: number; err?: string }>(
          (resolve) => {
            const ws = new WebSocket(url);
            const timeout = setTimeout(
              () =>
                resolve({
                  ok: false,
                  readyState: ws.readyState,
                  err: "open timeout",
                }),
              5_000,
            );
            ws.onopen = () => {
              clearTimeout(timeout);
              try {
                ws.send(JSON.stringify({ type: "user_message", content: text }));
                resolve({ ok: true, readyState: ws.readyState });
              } catch (e) {
                resolve({
                  ok: false,
                  readyState: ws.readyState,
                  err: String(e),
                });
              }
            };
            ws.onerror = () => {
              clearTimeout(timeout);
              resolve({
                ok: false,
                readyState: ws.readyState,
                err: "ws error",
              });
            };
          },
        );
      },
      { sid: sessionId, text: uniqueText },
    );

    expect(sendResult.ok, `WS send failed: ${JSON.stringify(sendResult)}`).toBeTruthy();

    // Assert the server logged the user_message. Green = the entire
    // browser→Vite→aiohttp→WsBridge path is intact. Red = regression
    // somewhere in that chain.
    const escaped = uniqueText.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const found = await waitForLog(
      new RegExp(`user_message received '${escaped}`),
    );
    expect(
      found,
      `server.log did not record the user_message within 15s. ` +
        `Last 2 KB of log:\n${(await readServerLog()).slice(-2048)}`,
    ).not.toBeNull();
  });
});
