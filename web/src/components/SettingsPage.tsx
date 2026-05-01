import { useState } from "react";
import { VoiceProfiles } from "./VoiceProfiles.js";
import { VoiceFingerprints } from "./VoiceFingerprints.js";
import { VoiceLogs } from "./VoiceLogs.js";
import { ApiKeys } from "./ApiKeys.js";
import { Devices } from "./DeviceTokens.js";
import { AndroidDevices } from "./AndroidDevices.js";

type Tab = "voice-profiles" | "fingerprints" | "voice-logs" | "api-keys" | "devices" | "android";

export function SettingsPage() {
  // Support deep-linking to a tab via hash fragment: #settings/api-keys
  const initialTab = (): Tab => {
    const hash = window.location.hash;
    const match = hash.match(/^#\/settings\/(.+)$/);
    if (match) {
      const t = match[1] as Tab;
      if (["voice-profiles", "fingerprints", "voice-logs", "api-keys", "devices", "android"].includes(t)) return t;
    }
    return "fingerprints";
  };
  const [tab, setTab] = useState<Tab>(initialTab);

  return (
    <div className="h-[100dvh] flex flex-col bg-cc-bg text-cc-fg">
      {/* Header */}
      <div className="border-b border-cc-border px-6 py-4 flex items-center gap-4">
        <button
          onClick={() => { window.location.hash = ""; }}
          className="p-1.5 rounded-lg hover:bg-cc-hover text-cc-muted hover:text-cc-fg transition-colors cursor-pointer"
          title="Back"
        >
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-5 h-5">
            <path d="M10 3L5 8l5 5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
        <h1 className="text-xl font-semibold">Settings</h1>
      </div>

      {/* Tabs */}
      <div className="border-b border-cc-border px-6">
        <div className="flex gap-1">
          {([
            { id: "fingerprints" as Tab, label: "Speaker ID" },
            { id: "voice-profiles" as Tab, label: "Voice Profiles" },
            { id: "voice-logs" as Tab, label: "Voice Logs" },
            { id: "api-keys" as Tab, label: "API Keys" },
            { id: "devices" as Tab, label: "Devices" },
            { id: "android" as Tab, label: "Android" },
          ]).map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors cursor-pointer ${
                tab === t.id
                  ? "border-cc-primary text-cc-fg"
                  : "border-transparent text-cc-muted hover:text-cc-fg"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {tab === "voice-profiles" && <VoiceProfiles />}
        {tab === "fingerprints" && <VoiceFingerprints />}
        {tab === "voice-logs" && <VoiceLogs />}
        {tab === "api-keys" && <ApiKeys />}
        {tab === "devices" && <Devices />}
        {tab === "android" && <AndroidDevices />}
      </div>
    </div>
  );
}
