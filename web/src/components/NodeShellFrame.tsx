// Shell side of the node-vended UI (contract ui/v1 + §D postMessage
// bridge): renders a node's own web UI in an iframe behind the hub's
// /nodes/{id}/ui/ proxy. The strip above the iframe is the hub shell —
// it owns node switching, voice (contract §B: audio never enters the
// iframe), and the desktop viewer (frozen desktop/v1).

import { useEffect, useRef, useState } from "react";
import { useStore } from "../store.js";
import { VoiceControls } from "./VoiceControls.js";
import { DesktopView } from "./DesktopView.js";
import { SettingsPage } from "./SettingsPage.js";

type View = "iframe" | "desktop" | "settings";

export function NodeShellFrame({ nodeId }: { nodeId: string }) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [view, setView] = useState<View>("iframe");
  const [menuOpen, setMenuOpen] = useState(false);
  const [authEnabled, setAuthEnabled] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const nodes = useStore((s) => s.nodes);
  const activeNodeId = useStore((s) => s.activeNodeId);
  const darkMode = useStore((s) => s.darkMode);

  useEffect(() => {
    fetch("/api/auth/me").then((r) => r.json()).then((d) => {
      if (d.authEnabled) setAuthEnabled(true);
    }).catch(() => {});
  }, []);

  // Close the profile menu on any outside click.
  useEffect(() => {
    if (!menuOpen) return;
    function onClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    }
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, [menuOpen]);

  const theme = darkMode ? "dark" : "light";

  // Answer the iframe's hello with the current shell config (§D).
  useEffect(() => {
    function onMessage(e: MessageEvent) {
      const win = frameRef.current?.contentWindow;
      if (!win || e.source !== win) return;
      const d = e.data;
      if (!d || d.vibr8 !== 1 || d.type !== "hello") return;
      win.postMessage(
        { vibr8: 1, type: "hello_ack", protocolVersion: 1, theme },
        "*",
      );
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [theme]);

  // Push theme changes to the iframe.
  useEffect(() => {
    frameRef.current?.contentWindow?.postMessage(
      { vibr8: 1, type: "theme", value: theme },
      "*",
    );
  }, [theme]);

  const supportsDesktop = !!nodes.find((n) => n.id === nodeId)?.contract?.includes("desktop/v1");

  return (
    <div className="h-[100dvh] flex flex-col bg-cc-bg text-cc-fg">
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-cc-border shrink-0">
        <div className="flex items-center gap-1.5 mr-1">
          <img src="/logo.svg" alt="" className="h-[1.2em] w-auto" />
          <span className="text-sm font-semibold text-cc-fg tracking-tight">vibr8</span>
        </div>
        <span className="text-xs text-cc-muted">node</span>
        <select
          value={activeNodeId}
          onChange={(e) => useStore.getState().setActiveNode(e.target.value)}
          className="px-2 py-1 text-xs rounded-lg bg-cc-bg border border-cc-border text-cc-fg cursor-pointer focus:outline-none focus:ring-1 focus:ring-cc-primary"
        >
          {nodes.map((n) => (
            <option key={n.id} value={n.id} disabled={n.status === "offline"}>
              {n.name} {n.status === "offline" ? "(offline)" : ""}
            </option>
          ))}
        </select>

        <div className="flex-1" />

        {/* Desktop viewer (frozen desktop/v1 — a shell client, like UI-TARS) */}
        {supportsDesktop && (
          <button
            onClick={() => setView((v) => (v === "desktop" ? "iframe" : "desktop"))}
            className={`flex items-center justify-center w-7 h-7 rounded-lg transition-colors cursor-pointer ${
              view === "desktop"
                ? "text-cc-primary bg-cc-active"
                : "text-cc-muted hover:text-cc-fg hover:bg-cc-hover"
            }`}
            title={view === "desktop" ? "Back to node UI" : "View node desktop"}
          >
            <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
              <path fillRule="evenodd" d="M2 4.25A2.25 2.25 0 014.25 2h11.5A2.25 2.25 0 0118 4.25v8.5A2.25 2.25 0 0115.75 15H4.25A2.25 2.25 0 012 12.75v-8.5zm2.25-.75a.75.75 0 00-.75.75v8.5c0 .414.336.75.75.75h11.5a.75.75 0 00.75-.75v-8.5a.75.75 0 00-.75-.75H4.25z" clipRule="evenodd" />
              <path d="M7 17.25a.75.75 0 01.75-.75h4.5a.75.75 0 010 1.5h-4.5a.75.75 0 01-.75-.75z" />
            </svg>
          </button>
        )}

        {/* Voice belongs to the shell, never the iframe (contract §B) */}
        <VoiceControls />

        {/* Profile menu — hub-scoped actions (Settings, Sign out) live here,
            not in the per-node sidebar. */}
        <div className="relative" ref={menuRef}>
          <button
            onClick={() => setMenuOpen((o) => !o)}
            className={`flex items-center justify-center w-7 h-7 rounded-lg transition-colors cursor-pointer ${
              menuOpen
                ? "text-cc-primary bg-cc-active"
                : "text-cc-muted hover:text-cc-fg hover:bg-cc-hover"
            }`}
            title="Profile"
          >
            <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
              <path fillRule="evenodd" d="M10 9a3 3 0 100-6 3 3 0 000 6zm-7 9a7 7 0 1114 0H3z" clipRule="evenodd" />
            </svg>
          </button>
          {menuOpen && (
            <div className="absolute right-0 mt-1 w-44 rounded-lg border border-cc-border bg-cc-bg shadow-lg overflow-hidden z-50">
              <button
                onClick={() => { setView("settings"); setMenuOpen(false); }}
                className="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer text-left"
              >
                <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
                  <path fillRule="evenodd" d="M11.49 3.17c-.38-1.56-2.6-1.56-2.98 0a1.532 1.532 0 01-2.286.948c-1.372-.836-2.942.734-2.106 2.106.54.886.061 2.042-.947 2.287-1.561.379-1.561 2.6 0 2.978a1.532 1.532 0 01.947 2.287c-.836 1.372.734 2.942 2.106 2.106a1.532 1.532 0 012.287.947c.379 1.561 2.6 1.561 2.978 0a1.533 1.533 0 012.287-.947c1.372.836 2.942-.734 2.106-2.106a1.533 1.533 0 01.947-2.287c1.561-.379 1.561-2.6 0-2.978a1.532 1.532 0 01-.947-2.287c.836-1.372-.734-2.942-2.106-2.106a1.532 1.532 0 01-2.287-.947zM10 13a3 3 0 100-6 3 3 0 000 6z" clipRule="evenodd" />
                </svg>
                <span>Settings</span>
              </button>
              {authEnabled && (
                <button
                  onClick={() => {
                    setMenuOpen(false);
                    fetch("/api/auth/logout", { method: "POST" }).finally(() => {
                      window.location.reload();
                    });
                  }}
                  className="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-cc-fg hover:text-red-400 hover:bg-cc-hover transition-colors cursor-pointer text-left border-t border-cc-border"
                >
                  <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
                    <path fillRule="evenodd" d="M3 3a1 1 0 00-1 1v12a1 1 0 001 1h5a1 1 0 100-2H4V5h4a1 1 0 100-2H3zm11.293 3.293a1 1 0 011.414 0l3 3a1 1 0 010 1.414l-3 3a1 1 0 01-1.414-1.414L15.586 11H8a1 1 0 110-2h7.586l-1.293-1.293a1 1 0 010-1.414z" clipRule="evenodd" />
                  </svg>
                  <span>Sign out</span>
                </button>
              )}
            </div>
          )}
        </div>
      </div>

      {view === "desktop" ? (
        <div className="flex-1 min-h-0">
          <DesktopView sessionId="" />
        </div>
      ) : view === "settings" ? (
        <div className="flex-1 min-h-0">
          <SettingsPage embedded onClose={() => setView("iframe")} />
        </div>
      ) : (
        <iframe
          ref={frameRef}
          src={`/nodes/${nodeId}/ui/`}
          title={`node ${nodeId}`}
          className="flex-1 w-full border-0"
        />
      )}
    </div>
  );
}
