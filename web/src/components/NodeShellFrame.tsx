// Shell side of the node-vended UI (contract ui/v1 + §D postMessage
// bridge): renders a node's own web UI in an iframe behind the hub's
// /nodes/{id}/ui/ proxy. The strip above the iframe is the hub shell —
// it owns node switching, voice (contract §B: audio never enters the
// iframe), and the desktop viewer (frozen desktop/v1).

import { useEffect, useRef, useState } from "react";
import { useStore } from "../store.js";
import { VoiceControls } from "./VoiceControls.js";
import { DesktopView } from "./DesktopView.js";

export function NodeShellFrame({ nodeId }: { nodeId: string }) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [showDesktop, setShowDesktop] = useState(false);
  const nodes = useStore((s) => s.nodes);
  const activeNodeId = useStore((s) => s.activeNodeId);
  const darkMode = useStore((s) => s.darkMode);

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
            onClick={() => setShowDesktop((v) => !v)}
            className={`flex items-center justify-center w-7 h-7 rounded-lg transition-colors cursor-pointer ${
              showDesktop
                ? "text-cc-primary bg-cc-active"
                : "text-cc-muted hover:text-cc-fg hover:bg-cc-hover"
            }`}
            title={showDesktop ? "Back to node UI" : "View node desktop"}
          >
            <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
              <path fillRule="evenodd" d="M2 4.25A2.25 2.25 0 014.25 2h11.5A2.25 2.25 0 0118 4.25v8.5A2.25 2.25 0 0115.75 15H4.25A2.25 2.25 0 012 12.75v-8.5zm2.25-.75a.75.75 0 00-.75.75v8.5c0 .414.336.75.75.75h11.5a.75.75 0 00.75-.75v-8.5a.75.75 0 00-.75-.75H4.25z" clipRule="evenodd" />
              <path d="M7 17.25a.75.75 0 01.75-.75h4.5a.75.75 0 010 1.5h-4.5a.75.75 0 01-.75-.75z" />
            </svg>
          </button>
        )}

        {/* Voice belongs to the shell, never the iframe (contract §B) */}
        <VoiceControls />
      </div>

      {showDesktop ? (
        <div className="flex-1 min-h-0">
          <DesktopView sessionId="" />
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
