import { useState, useEffect, useCallback } from "react";
import { useStore } from "../store.js";
import { api, type SpeakerFingerprint } from "../api.js";

export function SpeakerGateSelector() {
  const [fingerprints, setFingerprints] = useState<SpeakerFingerprint[]>([]);
  const audioActive = useStore((s) => s.audioActive);
  const activeSpeakerName = useStore((s) => s.activeSpeakerName);
  const threshold = useStore((s) => s.speakerGateThreshold);
  const clientId = useStore((s) => s.clientId);

  const loadProfiles = useCallback(() => {
    api.listFingerprints().then(setFingerprints).catch(() => {});
  }, []);

  useEffect(() => {
    loadProfiles();
  }, [loadProfiles]);

  // Re-fetch profiles when audio becomes active (profiles may have been created/deleted)
  useEffect(() => {
    if (audioActive) loadProfiles();
  }, [audioActive, loadProfiles]);

  if (fingerprints.length === 0) return null;

  async function onSpeakerChange(name: string | null) {
    useStore.getState().setActiveSpeakerName(name);
    await api.setSpeakerGateByClient(clientId, {
      speakerName: name,
      threshold: useStore.getState().speakerGateThreshold,
    }).catch(() => {});
  }

  async function onThresholdChange(val: number) {
    useStore.getState().setSpeakerGateThreshold(val);
    await api.setSpeakerGateByClient(clientId, {
      speakerName: activeSpeakerName,
      threshold: val,
    }).catch(() => {});
  }

  return (
    <div className="px-3 pt-2 pb-1 space-y-1">
      <div className="flex items-center gap-2">
        <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 text-cc-muted shrink-0">
          <path d="M10 2a3 3 0 00-3 3v4a3 3 0 006 0V5a3 3 0 00-3-3z" />
          <path d="M5.5 9.643a.75.75 0 00-1.5 0V10c0 3.06 2.29 5.585 5.25 5.954V17.5h-1.5a.75.75 0 000 1.5h4.5a.75.75 0 000-1.5h-1.5v-1.546A6.001 6.001 0 0016 10v-.357a.75.75 0 00-1.5 0V10a4.5 4.5 0 01-9 0v-.357z" />
        </svg>
        <select
          value={activeSpeakerName ?? ""}
          onChange={(e) => onSpeakerChange(e.target.value || null)}
          className="flex-1 min-w-0 px-2 py-1 rounded-md bg-cc-bg border border-cc-border text-cc-fg text-xs truncate"
        >
          <option value="" className="bg-cc-bg text-cc-fg">No speaker gate</option>
          {fingerprints.map((fp) => (
            <option key={fp.id} value={fp.name} className="bg-cc-bg text-cc-fg">{fp.name}</option>
          ))}
        </select>
      </div>
      {activeSpeakerName && (
        <div className="flex items-center gap-2 pl-6">
          <input
            type="range"
            min="0.0"
            max="1.0"
            step="0.01"
            value={threshold}
            onChange={(e) => onThresholdChange(parseFloat(e.target.value))}
            className="flex-1 h-1"
          />
          <span className="text-[10px] text-cc-muted w-7 text-right">{threshold.toFixed(2)}</span>
        </div>
      )}
    </div>
  );
}
