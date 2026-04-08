import { useState, useEffect, useCallback, useRef } from "react";
import { api, type VoiceProfile } from "../api.js";
import { useStore } from "../store.js";
import { startPlaygroundWebRTC, stopPlaygroundWebRTC, sendPlaygroundParams } from "../webrtc.js";

const DEFAULT_PARAMS: Omit<VoiceProfile, "id" | "name" | "user" | "isActive" | "createdAt" | "updatedAt"> = {
  micGain: 1.0,
  vadThresholdDb: -30,
  sileroVadThreshold: 0.4,
  eouThreshold: 0.15,
  eouMaxRetries: 3,
  minSegmentDuration: 0.4,
  promptTimeoutMs: 1500,
};

interface SliderDef {
  key: keyof typeof DEFAULT_PARAMS;
  label: string;
  min: number;
  max: number;
  step: number;
  unit?: string;
}

const SLIDERS: SliderDef[] = [
  { key: "micGain", label: "Mic Gain", min: 0.1, max: 5.0, step: 0.1, unit: "x" },
  { key: "vadThresholdDb", label: "VAD Threshold", min: -50, max: -10, step: 1, unit: "dB" },
  { key: "sileroVadThreshold", label: "Silero VAD", min: 0.0, max: 1.0, step: 0.05 },
  { key: "eouThreshold", label: "EOU Threshold", min: 0.0, max: 1.0, step: 0.01 },
  { key: "eouMaxRetries", label: "EOU Max Retries", min: 1, max: 10, step: 1 },
  { key: "minSegmentDuration", label: "Min Segment", min: 0.1, max: 2.0, step: 0.1, unit: "s" },
  { key: "promptTimeoutMs", label: "Prompt Timeout", min: 500, max: 5000, step: 100, unit: "ms" },
];

export function VoiceProfiles() {
  const [profiles, setProfiles] = useState<VoiceProfile[]>([]);
  const [editing, setEditing] = useState<VoiceProfile | null>(null);
  const [editName, setEditName] = useState("");
  const [editParams, setEditParams] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);

  const playgroundActive = useStore((s) => s.playgroundActive);
  const playgroundRmsDb = useStore((s) => s.playgroundRmsDb);
  const playgroundVadActive = useStore((s) => s.playgroundVadActive);
  const playgroundSegments = useStore((s) => s.playgroundSegments);
  const playgroundSessionId = useStore((s) => s.playgroundSessionId);

  const loadProfiles = useCallback(async () => {
    try {
      const list = await api.listVoiceProfiles();
      setProfiles(list);
    } catch {
      // ignore
    }
    setLoading(false);
  }, []);

  useEffect(() => { loadProfiles(); }, [loadProfiles]);

  function startEdit(profile?: VoiceProfile) {
    if (profile) {
      setEditing(profile);
      setEditName(profile.name);
      setEditParams({
        micGain: profile.micGain,
        vadThresholdDb: profile.vadThresholdDb,
        sileroVadThreshold: profile.sileroVadThreshold,
        eouThreshold: profile.eouThreshold,
        eouMaxRetries: profile.eouMaxRetries,
        minSegmentDuration: profile.minSegmentDuration,
        promptTimeoutMs: profile.promptTimeoutMs ?? 1500,
      });
    } else {
      setEditing({ id: null } as VoiceProfile);
      setEditName("New Profile");
      setEditParams({ ...DEFAULT_PARAMS });
    }
  }

  async function saveProfile() {
    if (!editing) return;
    const data = { name: editName, ...editParams };
    try {
      if (editing.id) {
        await api.updateVoiceProfile(editing.id, data);
      } else {
        await api.createVoiceProfile(data);
      }
      setEditing(null);
      loadProfiles();
    } catch (err) {
      console.error("Failed to save profile:", err);
    }
  }

  async function deleteProfile(id: string) {
    try {
      await api.deleteVoiceProfile(id);
      loadProfiles();
    } catch {
      // ignore
    }
  }

  async function activateProfile(id: string) {
    try {
      await api.activateVoiceProfile(id);
      loadProfiles();
    } catch {
      // ignore
    }
  }

  // Send params to playground when sliders change
  const sendParamsTimeoutRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  function handleParamChange(key: string, value: number) {
    const next = { ...editParams, [key]: value };
    setEditParams(next);
    // Debounce sending to backend
    clearTimeout(sendParamsTimeoutRef.current);
    sendParamsTimeoutRef.current = setTimeout(() => {
      if (playgroundActive) {
        sendPlaygroundParams(next);
      }
    }, 100);
  }

  async function togglePlayground() {
    if (playgroundActive) {
      await stopPlaygroundWebRTC();
    } else {
      const activeProfile = profiles.find((p) => p.isActive);
      await startPlaygroundWebRTC(activeProfile?.id ?? undefined);
    }
  }

  // Mic level as percentage (map -60dB..0dB → 0..100)
  const levelPct = Math.max(0, Math.min(100, ((playgroundRmsDb + 60) / 60) * 100));

  if (loading) {
    return <div className="p-6 text-cc-muted">Loading...</div>;
  }

  return (
    <div className="p-6 max-w-3xl mx-auto space-y-6">
      {/* Profile list */}
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Profiles</h2>
        <div className="flex items-center gap-2">
          {profiles.some((p) => p.isActive) && (
            <button
              onClick={async () => {
                await api.deactivateVoiceProfiles();
                loadProfiles();
              }}
              className="px-3 py-1.5 text-sm font-medium rounded-lg bg-cc-hover text-cc-muted hover:text-cc-fg transition-colors cursor-pointer"
            >
              Use Defaults
            </button>
          )}
          <button
            onClick={() => startEdit()}
            className="px-3 py-1.5 text-sm font-medium rounded-lg bg-cc-primary text-white hover:bg-cc-primary-hover transition-colors cursor-pointer"
          >
            New Profile
          </button>
        </div>
      </div>

      {profiles.length === 0 && !editing && (
        <p className="text-sm text-cc-muted">No voice profiles yet. The default STT parameters will be used.</p>
      )}

      <div className="space-y-2">
        {profiles.map((p) => (
          <div
            key={p.id}
            className="flex items-center gap-3 p-3 rounded-xl bg-cc-sidebar border border-cc-border"
          >
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="font-medium text-sm truncate">{p.name}</span>
                {p.isActive && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-green-500/15 text-green-600 dark:text-green-400 font-medium">
                    Active
                  </span>
                )}
              </div>
              <p className="text-[11px] text-cc-muted mt-0.5">
                Gain {p.micGain}x | VAD {p.vadThresholdDb}dB | EOU {p.eouThreshold}
              </p>
            </div>
            <div className="flex items-center gap-1">
              {!p.isActive && (
                <button
                  onClick={() => activateProfile(p.id!)}
                  className="p-1.5 rounded-lg hover:bg-cc-hover text-cc-muted hover:text-green-500 transition-colors cursor-pointer"
                  title="Set as active"
                >
                  <svg viewBox="0 0 16 16" fill="currentColor" className="w-4 h-4">
                    <path d="M8 1a7 7 0 110 14A7 7 0 018 1zm3.22 4.72a.75.75 0 00-1.06-1.06L7 7.94 5.84 6.78a.75.75 0 00-1.06 1.06l1.75 1.75a.75.75 0 001.06 0l3.63-3.87z" />
                  </svg>
                </button>
              )}
              <button
                onClick={() => startEdit(p)}
                className="p-1.5 rounded-lg hover:bg-cc-hover text-cc-muted hover:text-cc-fg transition-colors cursor-pointer"
                title="Edit"
              >
                <svg viewBox="0 0 16 16" fill="currentColor" className="w-4 h-4">
                  <path d="M11.013 1.427a1.75 1.75 0 012.474 0l1.086 1.086a1.75 1.75 0 010 2.474l-8.61 8.61c-.21.21-.47.364-.756.445l-3.251.93a.75.75 0 01-.927-.928l.929-3.25a1.75 1.75 0 01.445-.758l8.61-8.61zm1.414 1.06a.25.25 0 00-.354 0L3.463 11.098a.25.25 0 00-.064.108l-.564 1.97 1.97-.564a.25.25 0 00.108-.064l8.61-8.61a.25.25 0 000-.354L12.427 2.488z" />
                </svg>
              </button>
              <button
                onClick={() => deleteProfile(p.id!)}
                className="p-1.5 rounded-lg hover:bg-cc-hover text-cc-muted hover:text-red-400 transition-colors cursor-pointer"
                title="Delete"
              >
                <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-4 h-4">
                  <path d="M4 4l8 8M12 4l-8 8" />
                </svg>
              </button>
            </div>
          </div>
        ))}
      </div>

      {/* Profile editor */}
      {editing && (
        <div className="p-4 rounded-xl bg-cc-sidebar border border-cc-border space-y-4">
          <div>
            <label className="block text-xs font-medium text-cc-muted mb-1">Profile Name</label>
            <input
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
              className="w-full px-3 py-2 text-sm rounded-lg bg-cc-bg border border-cc-border text-cc-fg outline-none focus:border-cc-primary/50"
            />
          </div>

          {SLIDERS.map((s) => (
            <div key={s.key}>
              <div className="flex justify-between text-xs mb-1">
                <span className="font-medium text-cc-muted">{s.label}</span>
                <span className="text-cc-fg">
                  {s.step >= 1 ? editParams[s.key] : editParams[s.key]?.toFixed(s.step < 0.1 ? 2 : 1)}
                  {s.unit ? ` ${s.unit}` : ""}
                </span>
              </div>
              <input
                type="range"
                min={s.min}
                max={s.max}
                step={s.step}
                value={editParams[s.key] ?? DEFAULT_PARAMS[s.key]}
                onChange={(e) => handleParamChange(s.key, parseFloat(e.target.value))}
                className="w-full accent-cc-primary"
              />
            </div>
          ))}

          <div className="flex gap-2 pt-2">
            <button
              onClick={saveProfile}
              className="px-4 py-2 text-sm font-medium rounded-lg bg-cc-primary text-white hover:bg-cc-primary-hover transition-colors cursor-pointer"
            >
              {editing.id ? "Save" : "Create"}
            </button>
            <button
              onClick={() => setEditing(null)}
              className="px-4 py-2 text-sm font-medium rounded-lg bg-cc-hover text-cc-muted hover:text-cc-fg transition-colors cursor-pointer"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Playground */}
      <div className="border-t border-cc-border pt-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Live Playground</h2>
          <button
            onClick={togglePlayground}
            className={`px-4 py-2 text-sm font-medium rounded-lg transition-colors cursor-pointer ${
              playgroundActive
                ? "bg-red-500/10 text-red-500 hover:bg-red-500/20"
                : "bg-cc-primary text-white hover:bg-cc-primary-hover"
            }`}
          >
            {playgroundActive ? "Stop" : "Start Playground"}
          </button>
        </div>

        {/* Mic level — only when active */}
        {playgroundActive && (
          <div>
            <div className="flex items-center gap-2 mb-1">
              <span className="text-xs font-medium text-cc-muted">Mic Level</span>
              <span
                className={`w-2 h-2 rounded-full transition-colors ${
                  playgroundVadActive ? "bg-green-500" : "bg-cc-muted opacity-40"
                }`}
                title={playgroundVadActive ? "Voice detected" : "Silent"}
              />
            </div>
            <div className="h-3 rounded-full bg-cc-border overflow-hidden">
              <div
                className={`h-full rounded-full transition-all duration-75 ${
                  playgroundVadActive ? "bg-green-500" : "bg-cc-primary"
                }`}
                style={{ width: `${levelPct}%` }}
              />
            </div>
            <div className="text-[10px] text-cc-muted mt-0.5 text-right">
              {playgroundRmsDb.toFixed(0)} dB
            </div>
          </div>
        )}

        {/* Segment feed — visible as long as there are segments (persists after stop) */}
        {(playgroundActive || playgroundSegments.length > 0) && (
          <div className="mt-4">
            <h3 className="text-xs font-medium text-cc-muted mb-2">Detected Segments</h3>
            {playgroundSegments.length === 0 ? (
              <p className="text-sm text-cc-muted">Speak into your microphone...</p>
            ) : (
              <div className="space-y-1.5 max-h-80 overflow-y-auto">
                {playgroundSegments.map((seg, i) => (
                  <div key={i} className="flex items-start gap-2 p-2 rounded-lg bg-cc-bg border border-cc-border">
                    <PlayButton segmentId={seg.segmentId ?? null} timeBegin={seg.timeBegin} timeEnd={seg.timeEnd} />
                    <div className="flex-1 min-w-0">
                      <p className="text-sm">{seg.transcript}</p>
                      <p className="text-[10px] text-cc-muted">
                        {new Date(seg.timestamp).toLocaleTimeString()} | {(seg.timeEnd - seg.timeBegin).toFixed(1)}s
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function PlayButton({ segmentId, timeBegin, timeEnd }: { segmentId: string | null; timeBegin: number; timeEnd: number }) {
  const [playing, setPlaying] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  function toggle() {
    if (!segmentId) return;
    if (playing && audioRef.current) {
      audioRef.current.pause();
      setPlaying(false);
      return;
    }
    const audio = new Audio(`/api/voice/logs/${segmentId}/audio`);
    audioRef.current = audio;
    audio.onended = () => setPlaying(false);
    audio.play().catch(() => setPlaying(false));
    setPlaying(true);
  }

  if (!segmentId) {
    return (
      <div className="w-7 h-7 rounded-full bg-cc-border flex items-center justify-center shrink-0">
        <svg viewBox="0 0 16 16" fill="currentColor" className="w-3 h-3 text-cc-muted">
          <path d="M6 3.5v9l6-4.5z" />
        </svg>
      </div>
    );
  }

  return (
    <button
      onClick={toggle}
      className="w-7 h-7 rounded-full bg-cc-primary/10 hover:bg-cc-primary/20 flex items-center justify-center shrink-0 cursor-pointer transition-colors"
    >
      <svg viewBox="0 0 16 16" fill="currentColor" className="w-3 h-3 text-cc-primary">
        {playing ? (
          <path d="M5 3h2v10H5zM9 3h2v10H9z" />
        ) : (
          <path d="M6 3.5v9l6-4.5z" />
        )}
      </svg>
    </button>
  );
}
