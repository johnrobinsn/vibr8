import { useState } from "react";
import { VoiceProfiles } from "./VoiceProfiles.js";
import { VoiceLogs } from "./VoiceLogs.js";

type Tab = "voice-profiles" | "voice-logs";

export function SettingsPage() {
  const [tab, setTab] = useState<Tab>("voice-profiles");

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
            { id: "voice-profiles" as Tab, label: "Voice Profiles" },
            { id: "voice-logs" as Tab, label: "Voice Logs" },
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
        {tab === "voice-logs" && <VoiceLogs />}
      </div>
    </div>
  );
}
