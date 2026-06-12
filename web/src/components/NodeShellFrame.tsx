// Shell side of the node-vended UI (contract ui/v1 + §D postMessage
// bridge): renders a node's own web UI in an iframe behind the hub's
// /nodes/{id}/ui/ proxy, with a slim strip to switch nodes. The node's
// UI ships with the node, so it is always the node's own commit.

import { useEffect, useRef } from "react";
import { useStore } from "../store.js";

export function NodeShellFrame({ nodeId }: { nodeId: string }) {
  const frameRef = useRef<HTMLIFrameElement>(null);
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

  return (
    <div className="h-[100dvh] flex flex-col bg-cc-bg text-cc-fg">
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-cc-border shrink-0">
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
      </div>
      <iframe
        ref={frameRef}
        src={`/nodes/${nodeId}/ui/`}
        title={`node ${nodeId}`}
        className="flex-1 w-full border-0"
      />
    </div>
  );
}
