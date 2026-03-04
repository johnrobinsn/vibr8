import { useState } from "react";
import { useStore } from "../store.js";
import { startWebRTC, stopWebRTC, setAudioInOnly } from "../webrtc.js";
import { cancelReconnect, manualReconnect } from "../ws.js";


export function TopBar() {
  const [audioError, setAudioError] = useState<string | null>(null);
  const currentSessionId = useStore((s) => s.currentSessionId);
  const cliConnected = useStore((s) => s.cliConnected);
  const sessionStatus = useStore((s) => s.sessionStatus);
  const sidebarOpen = useStore((s) => s.sidebarOpen);
  const setSidebarOpen = useStore((s) => s.setSidebarOpen);
  const taskPanelOpen = useStore((s) => s.taskPanelOpen);
  const setTaskPanelOpen = useStore((s) => s.setTaskPanelOpen);
  const activeTab = useStore((s) => s.activeTab);
  const setActiveTab = useStore((s) => s.setActiveTab);
  const connectionStatus = useStore((s) => s.connectionStatus);
  const reconnecting = useStore((s) => s.reconnecting);
  const reconnectGaveUp = useStore((s) => s.reconnectGaveUp);
  const audioMode = useStore((s) => s.audioMode);
  const webrtcStatus = useStore((s) => s.webrtcStatus);
  const sdkSessions = useStore((s) => s.sdkSessions);

  const isTerminalSession = currentSessionId
    ? sdkSessions.find((x) => x.sessionId === currentSessionId)?.backendType === "terminal"
    : false;
  const isConnected = currentSessionId ? (cliConnected.get(currentSessionId) ?? false) : false;
  const connStatus = currentSessionId ? (connectionStatus.get(currentSessionId) ?? "disconnected") : "disconnected";
  const isCliDisconnected = connStatus === "connected" && !isConnected;
  const isReconnecting = currentSessionId ? (reconnecting.get(currentSessionId) ?? false) : false;
  const hasGaveUp = currentSessionId ? (reconnectGaveUp.get(currentSessionId) ?? false) : false;
  const status = currentSessionId ? (sessionStatus.get(currentSessionId) ?? null) : null;
  const currentAudioMode = currentSessionId ? (audioMode.get(currentSessionId) ?? "off") : "off";
  const rtcStatus = currentSessionId ? (webrtcStatus.get(currentSessionId) ?? null) : null;

  async function handleAudioCycle() {
    if (!currentSessionId) return;
    setAudioError(null);
    if (currentAudioMode === "off") {
      try {
        await startWebRTC(currentSessionId);
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error("[webrtc] Failed to start:", msg);
        setAudioError(msg);
        setTimeout(() => setAudioError(null), 5000);
      }
    } else if (currentAudioMode === "in_out") {
      setAudioInOnly(currentSessionId);
    } else {
      stopWebRTC(currentSessionId);
    }
  }

  return (
    <header className="relative shrink-0 flex items-center justify-between px-2 sm:px-4 py-2 sm:py-2.5 bg-cc-card border-b border-cc-border">
      <div className="flex items-center gap-3">
        {/* Sidebar toggle */}
        <button
          onClick={() => setSidebarOpen(!sidebarOpen)}
          className="flex items-center justify-center w-7 h-7 rounded-lg text-cc-muted hover:text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer"
        >
          <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
            <path fillRule="evenodd" d="M3 5a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zM3 10a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zM3 15a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1z" clipRule="evenodd" />
          </svg>
        </button>

        {/* Connection status (not shown for terminal sessions) */}
        {currentSessionId && !isTerminalSession && (
          <div className="flex items-center gap-1.5">
            {isConnected ? (
              <>
                <span className="w-1.5 h-1.5 rounded-full bg-cc-success" />
                <span className="text-[11px] text-cc-muted hidden sm:inline">Connected</span>
              </>
            ) : isCliDisconnected ? (
              <>
                <span className="w-1.5 h-1.5 rounded-full bg-cc-warning animate-pulse" />
                <span className="text-[11px] text-cc-warning font-medium hidden sm:inline">Waiting for CLI…</span>
              </>
            ) : isReconnecting ? (
              <>
                <span className="w-1.5 h-1.5 rounded-full bg-cc-warning animate-pulse" />
                <span className="text-[11px] text-cc-warning font-medium hidden sm:inline">Reconnecting…</span>
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
            ) : (
              <>
                <span className="w-1.5 h-1.5 rounded-full bg-cc-muted opacity-40" />
                <button
                  onClick={() => manualReconnect(currentSessionId)}
                  className="text-[11px] text-cc-warning hover:text-cc-warning/80 font-medium cursor-pointer hidden sm:inline"
                >
                  Reconnect
                </button>
              </>
            )}
          </div>
        )}
      </div>

      {/* Right side (not shown for terminal sessions) */}
      {currentSessionId && !isTerminalSession && (
        <div className="flex items-center gap-1.5 sm:gap-3 text-[12px] text-cc-muted">
          {status === "compacting" && (
            <span className="text-cc-warning font-medium animate-pulse">Compacting...</span>
          )}

          {status === "running" && (
            <div className="flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full bg-cc-primary animate-[pulse-dot_1s_ease-in-out_infinite]" />
              <span className="text-cc-primary font-medium">Thinking</span>
            </div>
          )}

          {/* Chat / Editor tab toggle */}
          <div className="flex items-center bg-cc-hover rounded-lg p-0.5">
            <button
              onClick={() => setActiveTab("chat")}
              className={`px-2.5 py-1 rounded-md text-[11px] font-medium transition-colors cursor-pointer ${
                activeTab === "chat"
                  ? "bg-cc-card text-cc-fg shadow-sm"
                  : "text-cc-muted hover:text-cc-fg"
              }`}
            >
              Chat
            </button>
            <button
              onClick={() => setActiveTab("editor")}
              className={`px-2.5 py-1 rounded-md text-[11px] font-medium transition-colors cursor-pointer ${
                activeTab === "editor"
                  ? "bg-cc-card text-cc-fg shadow-sm"
                  : "text-cc-muted hover:text-cc-fg"
              }`}
            >
              Editor
            </button>
          </div>

          {/* Audio cycle: off → in+out → in-only → off */}
          <button
            onClick={handleAudioCycle}
            disabled={!isConnected}
            className={`flex items-center justify-center w-7 h-7 rounded-lg transition-colors ${
              !isConnected
                ? "text-cc-muted opacity-30 cursor-not-allowed"
                : currentAudioMode === "in_out"
                ? "text-cc-success bg-cc-success/10 hover:bg-cc-success/20 cursor-pointer"
                : currentAudioMode === "in_only"
                ? "text-cc-warning bg-cc-warning/10 hover:bg-cc-warning/20 cursor-pointer"
                : "text-cc-muted hover:text-cc-fg hover:bg-cc-hover cursor-pointer"
            }`}
            title={
              !isConnected
                ? "Connect to enable audio"
                : currentAudioMode === "in_out"
                ? "Audio in+out — click for mic only"
                : currentAudioMode === "in_only"
                ? "Mic only — click to disable audio"
                : "Enable audio"
            }
          >
            {currentAudioMode === "in_out" ? (
              /* Speaker icon (green) — in+out mode */
              <svg viewBox="0 0 24 24" fill="currentColor" className="w-4 h-4">
                <path d="M13.5 4.06c0-1.336-1.616-2.005-2.56-1.06l-4.5 4.5H4.508c-1.141 0-2.318.664-2.66 1.905A9.76 9.76 0 001.5 12c0 .898.121 1.768.35 2.595.341 1.24 1.518 1.905 2.659 1.905h1.93l4.5 4.5c.945.945 2.561.276 2.561-1.06V4.06zM18.584 5.106a.75.75 0 011.06 0c3.808 3.807 3.808 9.98 0 13.788a.75.75 0 01-1.06-1.06 8.25 8.25 0 000-11.668.75.75 0 010-1.06z" />
                <path d="M15.932 7.757a.75.75 0 011.061 0 6 6 0 010 8.486.75.75 0 01-1.06-1.061 4.5 4.5 0 000-6.364.75.75 0 010-1.06z" />
              </svg>
            ) : currentAudioMode === "in_only" ? (
              /* Mic icon (amber) — in-only mode */
              <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
                <path d="M7 4a3 3 0 016 0v4a3 3 0 01-6 0V4z" />
                <path d="M5.5 9.643a.75.75 0 00-1.5 0V10c0 3.06 2.29 5.585 5.25 5.954V17.5h-1.5a.75.75 0 000 1.5h4.5a.75.75 0 000-1.5h-1.5v-1.546A6.001 6.001 0 0016 10v-.357a.75.75 0 00-1.5 0V10a4.5 4.5 0 01-9 0v-.357z" />
              </svg>
            ) : (
              /* Muted mic icon (gray) — off */
              <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
                <path d="M7 4a3 3 0 016 0v4a3 3 0 01-6 0V4z" opacity="0.5" />
                <path d="M5.5 9.643a.75.75 0 00-1.5 0V10c0 3.06 2.29 5.585 5.25 5.954V17.5h-1.5a.75.75 0 000 1.5h4.5a.75.75 0 000-1.5h-1.5v-1.546A6.001 6.001 0 0016 10v-.357a.75.75 0 00-1.5 0V10a4.5 4.5 0 01-9 0v-.357z" opacity="0.5" />
                <path d="M3.707 2.293a1 1 0 00-1.414 1.414l14 14a1 1 0 001.414-1.414l-14-14z" />
              </svg>
            )}
          </button>

          <button
            onClick={() => setTaskPanelOpen(!taskPanelOpen)}
            className={`flex items-center justify-center w-7 h-7 rounded-lg transition-colors cursor-pointer ${
              taskPanelOpen
                ? "text-cc-primary bg-cc-active"
                : "text-cc-muted hover:text-cc-fg hover:bg-cc-hover"
            }`}
            title="Toggle session panel"
          >
            <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
              <path fillRule="evenodd" d="M6 2a2 2 0 00-2 2v12a2 2 0 002 2h8a2 2 0 002-2V4a2 2 0 00-2-2H6zm1 3a1 1 0 000 2h6a1 1 0 100-2H7zm0 4a1 1 0 000 2h6a1 1 0 100-2H7zm0 4a1 1 0 000 2h4a1 1 0 100-2H7z" clipRule="evenodd" />
            </svg>
          </button>
        </div>
      )}
      {audioError && (
        <span className="absolute right-4 top-full mt-1 text-[11px] text-cc-error bg-cc-card border border-cc-error/30 rounded-md px-2 py-1 shadow-lg z-50 max-w-xs truncate">
          {audioError}
        </span>
      )}
    </header>
  );
}
