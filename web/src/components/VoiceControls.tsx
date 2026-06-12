// Voice controls — guard toggle, voice-mode chip, audio cycle button.
// Shared between the legacy TopBar (inside a session UI) and the hub
// shell strip (NodeShellFrame): voice always belongs to the hub shell,
// never to a node-vended iframe (contract §B).

import { useState } from "react";
import { useStore } from "../store.js";
import { startWebRTC, stopWebRTC, setAudioInOnly, toggleGuard } from "../webrtc.js";
import { ShieldIcon } from "./ShieldIcon.js";

export function VoiceControls({ disabled = false }: { disabled?: boolean }) {
  const [audioError, setAudioError] = useState<string | null>(null);
  const audioMode = useStore((s) => s.audioMode);
  const audioActive = useStore((s) => s.audioActive);
  const guardEnabled = useStore((s) => s.guardEnabled);
  const voiceMode = useStore((s) => s.voiceMode);
  const webrtcTransport = useStore((s) => s.webrtcTransport);
  const isRelay = webrtcTransport === "relay";

  async function handleAudioCycle() {
    setAudioError(null);
    if (audioMode === "off") {
      try {
        await startWebRTC();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error("[webrtc] Failed to start:", msg);
        stopWebRTC();
        setAudioError(msg);
        setTimeout(() => setAudioError(null), 5000);
      }
    } else if (audioMode === "connecting") {
      stopWebRTC();
    } else if (audioMode === "in_out") {
      setAudioInOnly();
    } else {
      stopWebRTC();
    }
  }

  return (
    <div className="relative flex items-center gap-1.5">
      {/* Guard word toggle — only shown when audio is active */}
      {audioMode !== "off" && audioActive && (
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
      {voiceMode && audioMode !== "off" && (
        <span className="flex items-center gap-1 px-2 h-7 rounded-lg text-xs font-semibold bg-cc-accent/15 text-cc-accent animate-pulse">
          <span className="w-1.5 h-1.5 rounded-full bg-cc-accent" />
          {voiceMode.toUpperCase()}
        </span>
      )}

      {/* Audio cycle: off → connecting → in+out → in-only → off */}
      <button
        onClick={handleAudioCycle}
        disabled={disabled}
        className={`flex items-center justify-center w-7 h-7 rounded-lg transition-colors ${
          disabled
            ? "text-cc-muted opacity-30 cursor-not-allowed"
            : audioMode === "connecting"
            ? "text-cc-success bg-cc-success/10 animate-pulse cursor-pointer"
            : audioMode === "in_out"
            ? isRelay
              ? "text-cc-warning bg-cc-warning/10 hover:bg-cc-warning/20 cursor-pointer"
              : "text-cc-success bg-cc-success/10 hover:bg-cc-success/20 cursor-pointer"
            : audioMode === "in_only"
            ? "text-cc-warning bg-cc-warning/10 hover:bg-cc-warning/20 cursor-pointer"
            : "text-cc-muted hover:text-cc-fg hover:bg-cc-hover cursor-pointer"
        }`}
        title={
          disabled
            ? "Connect to enable audio"
            : audioMode === "connecting"
            ? "Connecting audio…"
            : audioMode === "in_out"
            ? isRelay
              ? "Audio in+out (using TURN relay) — click for mic only"
              : "Audio in+out — click for mic only"
            : audioMode === "in_only"
            ? "Mic only — click to disable audio"
            : "Enable audio"
        }
      >
        {audioMode === "connecting" || audioMode === "in_out" ? (
          <svg viewBox="0 0 24 24" fill="currentColor" className="w-4 h-4">
            <path d="M13.5 4.06c0-1.336-1.616-2.005-2.56-1.06l-4.5 4.5H4.508c-1.141 0-2.318.664-2.66 1.905A9.76 9.76 0 001.5 12c0 .898.121 1.768.35 2.595.341 1.24 1.518 1.905 2.659 1.905h1.93l4.5 4.5c.945.945 2.561.276 2.561-1.06V4.06zM18.584 5.106a.75.75 0 011.06 0c3.808 3.807 3.808 9.98 0 13.788a.75.75 0 01-1.06-1.06 8.25 8.25 0 000-11.668.75.75 0 010-1.06z" />
            <path d="M15.932 7.757a.75.75 0 011.061 0 6 6 0 010 8.486.75.75 0 01-1.06-1.061 4.5 4.5 0 000-6.364.75.75 0 010-1.06z" />
          </svg>
        ) : audioMode === "in_only" ? (
          <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
            <path d="M7 4a3 3 0 016 0v4a3 3 0 01-6 0V4z" />
            <path d="M5.5 9.643a.75.75 0 00-1.5 0V10c0 3.06 2.29 5.585 5.25 5.954V17.5h-1.5a.75.75 0 000 1.5h4.5a.75.75 0 000-1.5h-1.5v-1.546A6.001 6.001 0 0016 10v-.357a.75.75 0 00-1.5 0V10a4.5 4.5 0 01-9 0v-.357z" />
          </svg>
        ) : (
          <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
            <path d="M7 4a3 3 0 016 0v4a3 3 0 01-6 0V4z" opacity="0.5" />
            <path d="M5.5 9.643a.75.75 0 00-1.5 0V10c0 3.06 2.29 5.585 5.25 5.954V17.5h-1.5a.75.75 0 000 1.5h4.5a.75.75 0 000-1.5h-1.5v-1.546A6.001 6.001 0 0016 10v-.357a.75.75 0 00-1.5 0V10a4.5 4.5 0 01-9 0v-.357z" opacity="0.5" />
            <path d="M3.707 2.293a1 1 0 00-1.414 1.414l14 14a1 1 0 001.414-1.414l-14-14z" />
          </svg>
        )}
      </button>

      {audioError && (
        <span className="absolute right-0 top-full mt-1 text-[11px] text-cc-error bg-cc-card border border-cc-error/30 rounded-md px-2 py-1 shadow-lg z-50 max-w-xs truncate">
          {audioError}
        </span>
      )}
    </div>
  );
}
