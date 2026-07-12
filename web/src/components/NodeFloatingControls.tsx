// Floating chrome for the node-vended UI. The old TopBar is gone —
// the hub shell strip carries the title, and everything else here
// overlays the content area so we don't burn a horizontal strip on
// per-node chrome:
//
//   • status line: a thin animated bar at the very top edge, encoding
//     connection / thinking / compacting / disconnected priorities.
//   • floating hamburger: opens the sidebar (session list). Sits at
//     top-left above whatever content is behind it.
//   • reconnect chip: appears next to the hamburger only when the
//     current session's CLI needs the user (disconnected, reconnecting,
//     or gave-up).
//   • floating viewer-pane toggle: at top-right, carries a small dot
//     when the pane has unseen content.

import { useStore } from "../store.js";
import { cancelReconnect, manualReconnect } from "../ws.js";

export function NodeFloatingControls() {
  const currentSessionId = useStore((s) => s.currentSessionId);
  const cliConnected = useStore((s) => s.cliConnected);
  const sessionStatus = useStore((s) => s.sessionStatus);
  const sidebarOpen = useStore((s) => s.sidebarOpen);
  const setSidebarOpen = useStore((s) => s.setSidebarOpen);
  const connectionStatus = useStore((s) => s.connectionStatus);
  const reconnecting = useStore((s) => s.reconnecting);
  const reconnectGaveUp = useStore((s) => s.reconnectGaveUp);
  const sdkSessions = useStore((s) => s.sdkSessions);
  const viewerPaneOpen = useStore((s) => s.viewerPaneOpen);
  const viewerPaneHasContent = useStore((s) => s.viewerPaneContent !== null);

  const isTerminalSession = currentSessionId
    ? sdkSessions.find((x) => x.sessionId === currentSessionId)?.backendType === "terminal"
    : false;
  const isConnected = currentSessionId ? (cliConnected.get(currentSessionId) ?? false) : false;
  const connStatus = currentSessionId ? (connectionStatus.get(currentSessionId) ?? "disconnected") : "disconnected";
  const isCliDisconnected = connStatus === "connected" && !isConnected;
  const isReconnecting = currentSessionId ? (reconnecting.get(currentSessionId) ?? false) : false;
  const hasGaveUp = currentSessionId ? (reconnectGaveUp.get(currentSessionId) ?? false) : false;
  const status = currentSessionId ? (sessionStatus.get(currentSessionId) ?? null) : null;

  const isDisconnectedLine =
    isCliDisconnected || isReconnecting || hasGaveUp || connStatus === "disconnected";
  const showReconnectChip =
    !!currentSessionId && !isTerminalSession && (isReconnecting || hasGaveUp || (!isConnected && !isCliDisconnected));
  const showViewerToggle = !!currentSessionId && !isTerminalSession;

  return (
    <>
      {/* Status line — top edge of the content area */}
      {currentSessionId && !isTerminalSession && (
        <StatusLine
          isConnected={isConnected}
          isThinking={status === "running"}
          isCompacting={status === "compacting"}
          isDisconnected={isDisconnectedLine}
        />
      )}

      {/* Top-left cluster: hamburger + reconnect chip */}
      <div className="absolute top-2 left-2 flex items-center gap-1.5 z-20">
        <button
          onClick={() => setSidebarOpen(!sidebarOpen)}
          aria-label={sidebarOpen ? "Close sidebar (Ctrl+Alt+S)" : "Open sidebar (Ctrl+Alt+S)"}
          title="Toggle sidebar (Ctrl+Alt+S)"
          className="flex items-center justify-center w-8 h-8 rounded-lg bg-cc-bg/80 backdrop-blur-sm border border-cc-border text-cc-muted hover:text-cc-fg hover:bg-cc-bg transition-colors cursor-pointer shadow-sm"
        >
          <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
            <path fillRule="evenodd" d="M3 5a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zM3 10a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zM3 15a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1z" clipRule="evenodd" />
          </svg>
        </button>

        {showReconnectChip && (
          <div className="flex items-center gap-1 h-8 pl-2 pr-1 rounded-lg bg-cc-bg/80 backdrop-blur-sm border border-cc-warning/40 shadow-sm">
            {isReconnecting ? (
              <>
                <span className="text-[11px] text-cc-warning font-medium">Reconnecting…</span>
                <button
                  onClick={() => currentSessionId && cancelReconnect(currentSessionId)}
                  className="w-5 h-5 flex items-center justify-center rounded text-cc-muted hover:text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer"
                  title="Cancel reconnection"
                >
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" className="w-2.5 h-2.5">
                    <path d="M4 4l8 8M12 4l-8 8" />
                  </svg>
                </button>
              </>
            ) : (
              <button
                onClick={() => currentSessionId && manualReconnect(currentSessionId)}
                className="px-1 text-[11px] text-cc-warning hover:text-cc-warning/80 font-medium cursor-pointer"
              >
                Reconnect
              </button>
            )}
          </div>
        )}
      </div>

      {/* Top-right: viewer pane toggle */}
      {showViewerToggle && (
        <button
          onClick={() => useStore.getState().setViewerPaneOpen(!viewerPaneOpen)}
          aria-label={viewerPaneOpen ? "Close viewer pane (Ctrl+Alt+V)" : "Open viewer pane (Ctrl+Alt+V)"}
          title="Toggle viewer pane (Ctrl+Alt+V)"
          className={`absolute top-2 right-2 z-20 flex items-center justify-center w-8 h-8 rounded-lg bg-cc-bg/80 backdrop-blur-sm border border-cc-border transition-colors cursor-pointer shadow-sm ${
            viewerPaneOpen
              ? "text-cc-primary"
              : "text-cc-muted hover:text-cc-fg hover:bg-cc-bg"
          }`}
        >
          <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
            <path fillRule="evenodd" d="M2 4.25A2.25 2.25 0 014.25 2h11.5A2.25 2.25 0 0118 4.25v11.5A2.25 2.25 0 0115.75 18H4.25A2.25 2.25 0 012 15.75V4.25zM4.25 3.5a.75.75 0 00-.75.75v11.5c0 .414.336.75.75.75H12v-13H4.25zM13.5 3.5v13h2.25a.75.75 0 00.75-.75V4.25a.75.75 0 00-.75-.75H13.5z" clipRule="evenodd" />
          </svg>
          {!viewerPaneOpen && viewerPaneHasContent && (
            <span className="absolute top-0.5 right-0.5 w-2 h-2 rounded-full bg-cc-primary" />
          )}
        </button>
      )}
    </>
  );
}

// ── Status line ─────────────────────────────────────────────────────────────

function StatusLine({ isConnected, isThinking, isCompacting, isDisconnected }: {
  isConnected: boolean;
  isThinking: boolean;
  isCompacting: boolean;
  isDisconnected: boolean;
}) {
  // Priority: disconnected > compacting > thinking > connected
  if (isDisconnected) {
    return <div className="absolute top-0 left-0 right-0 h-0.5 bg-cc-error opacity-70 animate-pulse z-10" />;
  }
  if (isCompacting) {
    return <div className="absolute top-0 left-0 right-0 h-0.5 bg-cc-warning opacity-60 animate-pulse z-10" />;
  }
  if (isThinking) {
    return <div className="absolute top-0 left-0 right-0 h-0.5 bg-blue-400 opacity-50 animate-[pulse-dot_1.5s_ease-in-out_infinite] z-10" />;
  }
  if (isConnected) {
    return <div className="absolute top-0 left-0 right-0 h-0.5 bg-cc-success opacity-40 z-10" />;
  }
  return null;
}
