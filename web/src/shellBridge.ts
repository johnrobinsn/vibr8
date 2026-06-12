// Iframe side of the shell ↔ node-iframe postMessage bridge
// (docs/hub-node-contract-v1.md §D). The app must keep working when no
// shell ever answers (standalone or local development).

import { NODE_MODE } from "./nodeMode.js";

interface ShellMessage {
  vibr8?: number;
  type?: string;
  theme?: string;
  value?: string;
}

function applyTheme(theme: string | undefined): void {
  if (theme === "dark" || theme === "light") {
    document.documentElement.classList.toggle("dark", theme === "dark");
    localStorage.setItem("cc-dark-mode", theme === "dark" ? "true" : "false");
  }
}

export function initShellBridge(): void {
  if (!NODE_MODE || window.parent === window) return;

  window.addEventListener("message", (e: MessageEvent<ShellMessage>) => {
    const d = e.data;
    if (!d || d.vibr8 !== 1 || !d.type) return;
    if (d.type === "hello_ack") applyTheme(d.theme);
    else if (d.type === "theme") applyTheme(d.value);
    // focus/blur/voice_state: accepted and ignored for now (additive contract)
  });

  window.parent.postMessage(
    { vibr8: 1, type: "hello", protocolVersion: 1, capabilities: [] },
    "*",
  );
}
