import { useState } from "react";
import { useStore } from "../store.js";
import { api } from "../api.js";
import { startWebRTC, stopWebRTC, setAudioInOnly, toggleGuard } from "../webrtc.js";
import { cancelReconnect, manualReconnect } from "../ws.js";
import { ShieldIcon } from "./ShieldIcon.js";


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
  const audioActive = useStore((s) => s.audioActive);
  const webrtcStatus = useStore((s) => s.webrtcStatus);
  const webrtcTransport = useStore((s) => s.webrtcTransport);
  const guardEnabled = useStore((s) => s.guardEnabled);
  const voiceMode = useStore((s) => s.voiceMode);
  const sdkSessions = useStore((s) => s.sdkSessions);
  const sessionNames = useStore((s) => s.sessionNames);
  const activeAudioInputLabel = useStore((s) => s.activeAudioInputLabel);

  const isTerminalSession = currentSessionId
    ? sdkSessions.find((x) => x.sessionId === currentSessionId)?.backendType === "terminal"
    : false;
  const isConnected = currentSessionId ? (cliConnected.get(currentSessionId) ?? false) : false;
  const connStatus = currentSessionId ? (connectionStatus.get(currentSessionId) ?? "disconnected") : "disconnected";
  const isCliDisconnected = connStatus === "connected" && !isConnected;
  const isReconnecting = currentSessionId ? (reconnecting.get(currentSessionId) ?? false) : false;
  const hasGaveUp = currentSessionId ? (reconnectGaveUp.get(currentSessionId) ?? false) : false;
  const status = currentSessionId ? (sessionStatus.get(currentSessionId) ?? null) : null;
  const currentAudioMode = audioMode;
  const isRelay = webrtcTransport === "relay";
  const sessionName = currentSessionId ? (sessionNames.get(currentSessionId) ?? null) : null;

  // Derive mobile status line state
  const isDisconnected = !isConnected && !isReconnecting && !isCliDisconnected;
  const isTroubled = isCliDisconnected || isReconnecting || (isDisconnected && !hasGaveUp && connStatus !== "disconnected");

  async function handleAudioCycle() {
    setAudioError(null);
    if (currentAudioMode === "off") {
      try {
        await startWebRTC();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error("[webrtc] Failed to start:", msg);
        stopWebRTC();
        setAudioError(msg);
        setTimeout(() => setAudioError(null), 5000);
      }
    } else if (currentAudioMode === "connecting") {
      stopWebRTC();
    } else if (currentAudioMode === "in_out") {
      setAudioInOnly();
    } else {
      stopWebRTC();
    }
  }

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
        {currentSessionId && sessionName && (
          <span className="sm:hidden text-sm text-cc-fg font-semibold truncate max-w-[35vw]">
            {sessionName}
          </span>
        )}
      </div>

      {/* ── Center — session name (desktop only) ── */}
      {currentSessionId && (
        <div className="hidden sm:block absolute left-1/2 -translate-x-1/2 max-w-[40%] pointer-events-none">
          {status === "running" || status === "compacting" ? (
            sessionName && (
              <span className="text-sm text-cc-fg font-semibold truncate block">
                {sessionName}
              </span>
            )
          ) : (
            sessionName && (
              <span className="text-sm text-cc-fg font-semibold truncate block">
                {sessionName}
              </span>
            )
          )}
        </div>
      )}

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

          {/* Audio input device indicator — mobile only, left of guard toggle */}
          {currentAudioMode !== "off" && (
            <AudioInputIndicator label={activeAudioInputLabel} audioActive={true} />
          )}

          {/* Guard word toggle — only shown when audio is active */}
          {currentAudioMode !== "off" && audioActive && (
            <button
              onClick={() => toggleGuard()}
              className={`flex items-center justify-center w-7 h-7 rounded-lg transition-colors cursor-pointer ${
                guardEnabled
                  ? "text-cc-warning bg-cc-warning/10 hover:bg-cc-warning/20"
                  : "text-cc-error bg-cc-error/10 animate-pulse"
              }`}
              title={guardEnabled ? 'Guard mode — say "vibrate" to command' : "Listening — click for guard mode"}
            >
              {guardEnabled ? (
                <ShieldIcon className="w-4 h-4" />
              ) : (
                <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
                  <path d="M7 4a3 3 0 016 0v4a3 3 0 01-6 0V4z" />
                  <path d="M5.5 9.643a.75.75 0 00-1.5 0V10c0 3.06 2.29 5.585 5.25 5.954V17.5h-1.5a.75.75 0 000 1.5h4.5a.75.75 0 000-1.5h-1.5v-1.546A6.001 6.001 0 0016 10v-.357a.75.75 0 00-1.5 0V10a4.5 4.5 0 01-9 0v-.357z" />
                </svg>
              )}
            </button>
          )}

          {/* Voice mode indicator (e.g. note mode) */}
          {voiceMode && currentAudioMode !== "off" && (
            <span className="flex items-center gap-1 px-2 h-7 rounded-lg text-xs font-semibold bg-cc-accent/15 text-cc-accent animate-pulse">
              <span className="w-1.5 h-1.5 rounded-full bg-cc-accent" />
              {voiceMode.toUpperCase()}
            </span>
          )}

          {/* Audio cycle: off → connecting → in+out → in-only → off */}
          <button
            onClick={handleAudioCycle}
            disabled={!isConnected}
            className={`flex items-center justify-center w-7 h-7 rounded-lg transition-colors ${
              !isConnected
                ? "text-cc-muted opacity-30 cursor-not-allowed"
                : currentAudioMode === "connecting"
                ? "text-cc-success bg-cc-success/10 animate-pulse cursor-pointer"
                : currentAudioMode === "in_out"
                ? isRelay
                  ? "text-cc-warning bg-cc-warning/10 hover:bg-cc-warning/20 cursor-pointer"
                  : "text-cc-success bg-cc-success/10 hover:bg-cc-success/20 cursor-pointer"
                : currentAudioMode === "in_only"
                ? "text-cc-warning bg-cc-warning/10 hover:bg-cc-warning/20 cursor-pointer"
                : "text-cc-muted hover:text-cc-fg hover:bg-cc-hover cursor-pointer"
            }`}
            title={
              !isConnected
                ? "Connect to enable audio"
                : currentAudioMode === "connecting"
                ? "Connecting audio…"
                : currentAudioMode === "in_out"
                ? isRelay
                  ? "Audio in+out (using TURN relay) — click for mic only"
                  : "Audio in+out — click for mic only"
                : currentAudioMode === "in_only"
                ? "Mic only — click to disable audio"
                : "Enable audio"
            }
          >
            {currentAudioMode === "connecting" || currentAudioMode === "in_out" ? (
              /* Speaker icon — green (direct/connecting) or amber (relay) */
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

      {audioError && (
        <span className="absolute right-4 top-full mt-1 text-[11px] text-cc-error bg-cc-card border border-cc-error/30 rounded-md px-2 py-1 shadow-lg z-50 max-w-xs truncate">
          {audioError}
        </span>
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

// ── Audio input device indicator ─────────────────────────────────────────────

function classifyAudioDevice(label: string): "bluetooth" | "headset" | "speaker" | "phone" {
  const l = label.toLowerCase();
  if (l.includes("bluetooth") || l.includes("hands-free") || l.includes("handsfree")) return "bluetooth";
  if (l.includes("headset") || l.includes("headphone")) return "headset";
  if (l.includes("speakerphone") || l.includes("speaker phone")) return "speaker";
  return "phone";
}

const audioDeviceText: Record<ReturnType<typeof classifyAudioDevice>, string> = {
  bluetooth: "BT",
  headset: "HD",
  speaker: "Spkr",
  phone: "Mic",
};

function AudioInputIndicator({ label, audioActive }: { label: string | null; audioActive: boolean }) {
  const type = label ? classifyAudioDevice(label) : "phone";
  return (
    <div
      className={`sm:hidden flex items-center gap-0.5 px-1 h-5 rounded ${audioActive ? "text-cc-fg" : "text-cc-muted opacity-50"}`}
      title={label || "Audio input"}
    >
      <span className="text-[9px] font-semibold font-mono-code leading-none">{audioDeviceText[type]}</span>
    </div>
  );
}
