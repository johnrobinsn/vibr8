import { useState, useEffect, useCallback } from "react";
import { useStore } from "../store.js";
import { api, type SpeakerFingerprint } from "../api.js";

export function SpeakerGateSelector() {
  const [fingerprints, setFingerprints] = useState<SpeakerFingerprint[]>([]);
  const [tseAvailable, setTseAvailable] = useState<boolean | null>(null);
  const audioActive = useStore((s) => s.audioActive);
  const activeSpeakerName = useStore((s) => s.activeSpeakerName);
  const threshold = useStore((s) => s.speakerGateThreshold);
  const tseEnabled = useStore((s) => s.speakerGateTseEnabled);
  const clientId = useStore((s) => s.clientId);

  const loadProfiles = useCallback(() => {
    api.listFingerprints().then(setFingerprints).catch(() => {});
  }, []);

  useEffect(() => {
    loadProfiles();
    api.getTseAvailable().then((r) => setTseAvailable(r.available)).catch(() => setTseAvailable(false));
  }, [loadProfiles]);

  // Re-fetch profiles when audio becomes active (profiles may have been created/deleted)
  useEffect(() => {
    if (audioActive) loadProfiles();
  }, [audioActive, loadProfiles]);

  if (fingerprints.length === 0) return null;

  function pushGate(opts: { speakerName: string | null; threshold: number; tseEnabled: boolean }) {
    return api.setSpeakerGateByClient(clientId, {
      speakerName: opts.speakerName,
      threshold: opts.threshold,
      tseEnabled: opts.tseEnabled,
      tseThreshold: useStore.getState().speakerGateTseThreshold,
    }).catch(() => {});
  }

  async function onSpeakerChange(name: string | null) {
    useStore.getState().setActiveSpeakerName(name);
    await pushGate({ speakerName: name, threshold, tseEnabled });
  }

  async function onThresholdChange(val: number) {
    useStore.getState().setSpeakerGateThreshold(val);
    await pushGate({ speakerName: activeSpeakerName, threshold: val, tseEnabled });
  }

  async function onTseToggle(val: boolean) {
    useStore.getState().setSpeakerGateTseEnabled(val);
    await pushGate({ speakerName: activeSpeakerName, threshold, tseEnabled: val });
  }

  const tseDisabled = tseAvailable === false;

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
        <>
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
          <label
            className={`flex items-center gap-2 pl-6 text-[11px] ${tseDisabled ? "text-cc-muted opacity-60 cursor-not-allowed" : "text-cc-fg cursor-pointer"}`}
            title={
              tseAvailable === null
                ? "Checking GPU availability…"
                : tseDisabled
                ? "Target-speaker extraction needs CUDA + the WeSep BSRNN checkpoint."
                : "Clean audio with target-speaker extraction before transcription."
            }
          >
            <input
              type="checkbox"
              checked={tseEnabled && !tseDisabled}
              disabled={tseDisabled}
              onChange={(e) => onTseToggle(e.target.checked)}
              className="w-3 h-3"
            />
            Extract target speaker
          </label>
        </>
      )}
    </div>
  );
}
