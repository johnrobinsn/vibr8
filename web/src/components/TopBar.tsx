import { useStore } from "../store.js";
import { api } from "../api.js";
import { cancelReconnect, manualReconnect } from "../ws.js";


export function TopBar() {
  const currentSessionId = useStore((s) => s.currentSessionId);
  const cliConnected = useStore((s) => s.cliConnected);
  const sessionStatus = useStore((s) => s.sessionStatus);
  const sidebarOpen = useStore((s) => s.sidebarOpen);
  const setSidebarOpen = useStore((s) => s.setSidebarOpen);
  const taskPanelOpen = useStore((s) => s.taskPanelOpen);
  const setTaskPanelOpen = useStore((s) => s.setTaskPanelOpen);
  const activeTab = useStore((s) => s.activeTab);
  const setActiveTab = useStore((s) => s.setActiveTab);
  const activeView = useStore((s) => s.activeView);
  const connectionStatus = useStore((s) => s.connectionStatus);
  const reconnecting = useStore((s) => s.reconnecting);
  const reconnectGaveUp = useStore((s) => s.reconnectGaveUp);
  const webrtcStatus = useStore((s) => s.webrtcStatus);
  const sdkSessions = useStore((s) => s.sdkSessions);
  const sessionNames = useStore((s) => s.sessionNames);
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
  const sessionName = currentSessionId ? (sessionNames.get(currentSessionId) ?? null) : null;

  // Derive mobile status line state
  const isDisconnected = !isConnected && !isReconnecting && !isCliDisconnected;
  const isTroubled = isCliDisconnected || isReconnecting || (isDisconnected && !hasGaveUp && connStatus !== "disconnected");

  return (
    <header className="relative shrink-0 flex items-center justify-between px-2 sm:px-4 py-2 sm:py-2.5 bg-cc-card border-b border-cc-border">
      {/* ── Left side ── */}
      <div className="flex items-center gap-2 sm:gap-3 min-w-0">
        {/* Sidebar toggle */}
        <button
          onClick={() => setSidebarOpen(!sidebarOpen)}
          aria-label={sidebarOpen ? "Close sidebar (Ctrl+Alt+S)" : "Open sidebar (Ctrl+Alt+S)"}
          title="Toggle sidebar (Ctrl+Alt+S)"
          className="flex items-center justify-center w-7 h-7 rounded-lg text-cc-muted hover:text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer shrink-0"
        >
          <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
            <path fillRule="evenodd" d="M3 5a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zM3 10a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zM3 15a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1z" clipRule="evenodd" />
          </svg>
        </button>

        {/* Desktop reconnect controls (no dot/text — status line handles visual) */}
        {currentSessionId && !isTerminalSession && (
          <div className="hidden sm:flex items-center gap-1.5">
            {isReconnecting ? (
              <>
                <span className="text-[11px] text-cc-warning font-medium">Reconnecting…</span>
                <button
                  onClick={() => cancelReconnect(currentSessionId)}
                  className="w-4 h-4 flex items-center justify-center rounded text-cc-muted hover:text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer"
                  title="Cancel reconnection"
                >
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" className="w-2.5 h-2.5">
                    <path d="M4 4l8 8M12 4l-8 8" />
                  </svg>
                </button>
              </>
            ) : (!isConnected && !isCliDisconnected) ? (
              <button
                onClick={() => manualReconnect(currentSessionId)}
                className="text-[11px] text-cc-warning hover:text-cc-warning/80 font-medium cursor-pointer"
              >
                Reconnect
              </button>
            ) : null}
          </div>
        )}

        {/* Session name — inline on mobile (avoids camera notch), hidden on desktop (centered instead) */}
        {activeView === "desktop" ? (
          <span className="sm:hidden text-sm text-cc-fg font-semibold truncate max-w-[35vw]">
            Desktop
          </span>
        ) : currentSessionId && sessionName ? (
          <span className="sm:hidden text-sm text-cc-fg font-semibold truncate max-w-[35vw]">
            {sessionName}
          </span>
        ) : null}
      </div>

      {/* ── Center — session name (desktop only) ── */}
      {activeView === "desktop" ? (
        <div className="hidden sm:block absolute left-1/2 -translate-x-1/2 max-w-[40%] pointer-events-none">
          <span className="text-sm text-cc-fg font-semibold truncate block">Desktop</span>
        </div>
      ) : currentSessionId ? (
        <div className="hidden sm:block absolute left-1/2 -translate-x-1/2 max-w-[40%] pointer-events-none">
          {sessionName && (
            <span className="text-sm text-cc-fg font-semibold truncate block">
              {sessionName}
            </span>
          )}
        </div>
      ) : null}

      {/* ── Right side ── */}
      {currentSessionId && !isTerminalSession && (
        <div className="flex items-center gap-1.5 sm:gap-3 text-[12px] text-cc-muted pr-5 sm:pr-0">
          {/* Chat / Editor tab toggle */}
          <div className="flex items-center bg-cc-hover rounded-lg p-0.5">
            <button
              onClick={() => setActiveTab("chat")}
              className={`px-1.5 sm:px-2.5 py-1 rounded-md text-[11px] font-medium transition-colors cursor-pointer ${
                activeTab === "chat"
                  ? "bg-cc-card text-cc-fg shadow-sm"
                  : "text-cc-muted hover:text-cc-fg"
              }`}
            >
              <span className="sm:hidden font-mono-code text-[10px]">&gt;_</span>
              <span className="hidden sm:inline">Chat</span>
            </button>
            <button
              onClick={() => setActiveTab("editor")}
              className={`px-1.5 sm:px-2.5 py-1 rounded-md text-[11px] font-medium transition-colors cursor-pointer ${
                activeTab === "editor"
                  ? "bg-cc-card text-cc-fg shadow-sm"
                  : "text-cc-muted hover:text-cc-fg"
              }`}
            >
              <svg viewBox="0 0 16 16" fill="currentColor" className="w-3.5 h-3.5 sm:hidden">
                <path d="M4 1.5A1.5 1.5 0 002.5 3v10A1.5 1.5 0 004 14.5h8a1.5 1.5 0 001.5-1.5V5.621a1.5 1.5 0 00-.44-1.06l-2.12-2.122A1.5 1.5 0 009.878 2H4z" />
              </svg>
              <span className="hidden sm:inline">Editor</span>
            </button>
          </div>

          {/* Voice (audio cycle, guard, voice-mode chip) lives in the hub
              shell strip, not the per-node UI (contract §B: audio never
              enters the iframe). */}

          {/* Session panel toggle — desktop only */}
          <button
            onClick={() => setTaskPanelOpen(!taskPanelOpen)}
            className={`hidden sm:flex items-center justify-center w-7 h-7 rounded-lg transition-colors cursor-pointer ${
              taskPanelOpen
                ? "text-cc-primary bg-cc-active"
                : "text-cc-muted hover:text-cc-fg hover:bg-cc-hover"
            }`}
            aria-label={taskPanelOpen ? "Close session panel" : "Open session panel"}
            title="Toggle session panel"
          >
            <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
              <path fillRule="evenodd" d="M6 2a2 2 0 00-2 2v12a2 2 0 002 2h8a2 2 0 002-2V4a2 2 0 00-2-2H6zm1 3a1 1 0 000 2h6a1 1 0 100-2H7zm0 4a1 1 0 000 2h6a1 1 0 100-2H7zm0 4a1 1 0 000 2h4a1 1 0 100-2H7z" clipRule="evenodd" />
            </svg>
          </button>

          {/* Viewer pane toggle */}
          <button
            onClick={() => useStore.getState().setViewerPaneOpen(!viewerPaneOpen)}
            className={`relative flex items-center justify-center w-7 h-7 rounded-lg transition-colors cursor-pointer ${
              viewerPaneOpen
                ? "text-cc-primary bg-cc-active"
                : "text-cc-muted hover:text-cc-fg hover:bg-cc-hover"
            }`}
            aria-label={viewerPaneOpen ? "Close viewer pane (Ctrl+Alt+V)" : "Open viewer pane (Ctrl+Alt+V)"}
            title="Toggle viewer pane (Ctrl+Alt+V)"
          >
            <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
              <path fillRule="evenodd" d="M2 4.25A2.25 2.25 0 014.25 2h11.5A2.25 2.25 0 0118 4.25v11.5A2.25 2.25 0 0115.75 18H4.25A2.25 2.25 0 012 15.75V4.25zM4.25 3.5a.75.75 0 00-.75.75v11.5c0 .414.336.75.75.75H12v-13H4.25zM13.5 3.5v13h2.25a.75.75 0 00.75-.75V4.25a.75.75 0 00-.75-.75H13.5z" clipRule="evenodd" />
            </svg>
            {!viewerPaneOpen && viewerPaneHasContent && (
              <span className="absolute top-0.5 right-0.5 w-2 h-2 rounded-full bg-cc-primary" />
            )}
          </button>
        </div>
      )}

      {/* ── Status line (bottom edge) ── */}
      {currentSessionId && !isTerminalSession && (
        <StatusLine
          isConnected={isConnected}
          isThinking={status === "running"}
          isCompacting={status === "compacting"}
          isDisconnected={isCliDisconnected || isReconnecting || hasGaveUp || connStatus === "disconnected"}
        />
      )}

    </header>
  );
}

// ── Status line (bottom edge of top bar) ─────────────────────────────────────

function StatusLine({ isConnected, isThinking, isCompacting, isDisconnected }: {
  isConnected: boolean;
  isThinking: boolean;
  isCompacting: boolean;
  isDisconnected: boolean;
}) {
  // Priority: disconnected > compacting > thinking > connected
  if (isDisconnected) {
    return <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-cc-error opacity-70 animate-pulse" />;
  }
  if (isCompacting) {
    return <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-cc-warning opacity-60 animate-pulse" />;
  }
  if (isThinking) {
    return <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-blue-400 opacity-50 animate-[pulse-dot_1.5s_ease-in-out_infinite]" />;
  }
  if (isConnected) {
    return <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-cc-success opacity-40" />;
  }
  return null;
}

