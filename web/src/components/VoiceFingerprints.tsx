import { useState, useEffect, useCallback, useRef } from "react";
import { api, type SpeakerFingerprint } from "../api.js";
import { useStore } from "../store.js";
import { startEnrollmentWs, stopEnrollmentWs } from "../webrtc.js";

interface EnrollmentSegment {
  transcript: string;
  embedding: number[];
  scores: { id: string; name: string; similarity: number }[];
}

const ENROLLMENT_PASSAGES = [
  "The quick brown fox jumps over the lazy dog while the bright sun shines warmly above the vast open meadow.",
  "She sells seashells by the seashore, and the shells she sells are surely seashells, I'm quite sure.",
  "Peter Piper picked a peck of pickled peppers, but how many pickled peppers did Peter Piper actually pick?",
  "A journey of a thousand miles begins with a single step, though the path may wind through unexpected places.",
  "The old oak tree stood silently at the edge of the village, its gnarled branches reaching toward the fading sky.",
];

export function VoiceFingerprints() {
  const [fingerprints, setFingerprints] = useState<SpeakerFingerprint[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Enrollment state
  const [enrolling, setEnrolling] = useState(false);
  const [enrollMode, setEnrollMode] = useState<"new" | "add-voiceprint">("new");
  const [enrollTargetProfile, setEnrollTargetProfile] = useState<SpeakerFingerprint | null>(null);
  const [enrollName, setEnrollName] = useState("");
  const [enrollLabel, setEnrollLabel] = useState("");
  const [enrollSegments, setEnrollSegments] = useState<EnrollmentSegment[]>([]);
  const [enrollVadActive, setEnrollVadActive] = useState(false);
  const [enrollRmsDb, setEnrollRmsDb] = useState(-60);
  const enrollSegmentsRef = useRef<EnrollmentSegment[]>([]);

  // Test state
  const [testing, setTesting] = useState(false);
  const [testScores, setTestScores] = useState<{ id: string; name: string; similarity: number; bestVoiceprint?: string }[]>([]);
  const [testHeard, setTestHeard] = useState(false);
  const testSilenceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const audioActive = useStore((s) => s.audioActive);

  const loadData = useCallback(async () => {
    try {
      setFingerprints(await api.listFingerprints());
    } catch {
      // ignore
    }
    setLoading(false);
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  useEffect(() => {
    return () => { stopEnrollmentWs(); };
  }, []);

  function startEnrollment(mode: "new" | "add-voiceprint", targetProfile?: SpeakerFingerprint) {
    if (!audioActive) {
      alert("Voice must be active to enroll. Enable the microphone first.");
      return;
    }
    setEnrolling(true);
    setEnrollMode(mode);
    setEnrollTargetProfile(targetProfile ?? null);
    setEnrollName(mode === "add-voiceprint" && targetProfile ? targetProfile.name : "");
    setEnrollLabel("");
    setEnrollSegments([]);
    enrollSegmentsRef.current = [];
    setEnrollVadActive(false);
    setEnrollRmsDb(-60);

    startEnrollmentWs((data: unknown) => {
      const msg = data as Record<string, unknown>;
      if (msg.type === "voice_level") {
        setEnrollRmsDb(msg.rmsDb as number);
      } else if (msg.type === "voice_activity") {
        setEnrollVadActive(msg.active as boolean);
      } else if (msg.type === "enrollment_segment") {
        const seg: EnrollmentSegment = {
          transcript: msg.transcript as string,
          embedding: msg.embedding as number[],
          scores: msg.scores as { id: string; name: string; similarity: number }[],
        };
        enrollSegmentsRef.current = [...enrollSegmentsRef.current, seg];
        setEnrollSegments(enrollSegmentsRef.current);
      }
    });
  }

  function cancelEnrollment() {
    stopEnrollmentWs();
    setEnrolling(false);
    setEnrollSegments([]);
    enrollSegmentsRef.current = [];
  }

  async function saveEnrollment() {
    if (enrollSegments.length === 0) return;

    const dim = enrollSegments[0].embedding.length;
    const centroid = new Array(dim).fill(0);
    for (const seg of enrollSegments) {
      for (let i = 0; i < dim; i++) centroid[i] += seg.embedding[i];
    }
    for (let i = 0; i < dim; i++) centroid[i] /= enrollSegments.length;
    const norm = Math.sqrt(centroid.reduce((sum: number, v: number) => sum + v * v, 0)) + 1e-12;
    for (let i = 0; i < dim; i++) centroid[i] /= norm;

    const label = enrollLabel.trim() || "Default";

    try {
      if (enrollMode === "add-voiceprint" && enrollTargetProfile) {
        await api.addEmbedding(enrollTargetProfile.id, { embedding: centroid, label });
      } else {
        const name = enrollName.trim() || "Unnamed";
        await api.createFingerprint({ name, embedding: centroid, label });
      }
      cancelEnrollment();
      loadData();
    } catch {
      alert("Failed to save");
    }
  }

  async function deleteFingerprint(id: string) {
    try {
      await api.deleteFingerprint(id);
      loadData();
    } catch {
      // ignore
    }
  }

  async function removeEmbedding(profileId: string, embId: string) {
    try {
      await api.removeEmbedding(profileId, embId);
      loadData();
    } catch {
      // ignore
    }
  }

  function startTest() {
    if (!audioActive) {
      alert("Voice must be active to test. Enable the microphone first.");
      return;
    }
    setTesting(true);
    setTestHeard(false);
    setTestScores(fingerprints.map((fp) => ({ id: fp.id, name: fp.name, similarity: 0 })));

    startEnrollmentWs((data: unknown) => {
      const msg = data as Record<string, unknown>;
      if (msg.type === "enrollment_segment") {
        setTestHeard(true);
        const scores = (msg.scores as { id: string; name: string; similarity: number; bestVoiceprint?: string }[])
          .slice()
          .sort((a, b) => b.similarity - a.similarity);
        setTestScores(scores);
        if (testSilenceTimer.current) clearTimeout(testSilenceTimer.current);
        testSilenceTimer.current = setTimeout(() => {
          setTestScores((prev) => prev.map((s) => ({ ...s, similarity: 0 })));
        }, 3000);
      }
    });
  }

  function stopTest() {
    stopEnrollmentWs();
    if (testSilenceTimer.current) clearTimeout(testSilenceTimer.current);
    setTesting(false);
    setTestScores([]);
  }

  if (loading) {
    return <div className="p-6 text-cc-muted">Loading...</div>;
  }

  return (
    <div className="p-6 max-w-2xl space-y-6">
      {/* Profile List */}
      <section>
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-sm font-semibold text-cc-fg">Speaker Profiles</h2>
          <div className="flex gap-2">
            {!testing && !enrolling && fingerprints.length > 0 && (
              <button
                onClick={startTest}
                className="px-3 py-1.5 text-xs rounded-lg bg-cc-input border border-cc-border text-cc-fg hover:bg-cc-hover cursor-pointer"
              >
                Test
              </button>
            )}
            {!enrolling && !testing && (
              <button
                onClick={() => startEnrollment("new")}
                className="px-3 py-1.5 text-xs rounded-lg bg-cc-primary text-white hover:opacity-90 cursor-pointer"
              >
                New Profile
              </button>
            )}
          </div>
        </div>

        {fingerprints.length === 0 && !enrolling && (
          <p className="text-xs text-cc-muted">No profiles yet. Create one to enable speaker gating.</p>
        )}

        {fingerprints.map((fp) => {
          const isExpanded = expandedId === fp.id;
          const isActive = useStore.getState().activeSpeakerName === fp.name;
          return (
            <div key={fp.id} className="mb-1.5">
              <div
                className="flex items-center justify-between py-2 px-3 rounded-lg bg-cc-input border border-cc-border cursor-pointer hover:bg-cc-hover"
                onClick={() => setExpandedId(isExpanded ? null : fp.id)}
              >
                <div className="flex items-center gap-2">
                  <span className="text-xs text-cc-muted">{isExpanded ? "▼" : "▶"}</span>
                  <span className="text-sm text-cc-fg">{fp.name}</span>
                  <span className="text-xs text-cc-muted">
                    {fp.embeddingCount} voiceprint{fp.embeddingCount !== 1 ? "s" : ""}
                  </span>
                  {isActive && (
                    <span className="text-xs text-green-500">active</span>
                  )}
                </div>
                <div className="flex gap-2" onClick={(e) => e.stopPropagation()}>
                  {!enrolling && !testing && (
                    <button
                      onClick={() => startEnrollment("add-voiceprint", fp)}
                      className="text-xs text-cc-primary hover:opacity-80 cursor-pointer"
                    >
                      + Voiceprint
                    </button>
                  )}
                  <button
                    onClick={() => deleteFingerprint(fp.id)}
                    className="text-xs text-red-400 hover:text-red-300 cursor-pointer"
                  >
                    Delete
                  </button>
                </div>
              </div>

              {isExpanded && (
                <div className="ml-6 mt-1 space-y-1">
                  {fp.embeddingLabels.map((label, i) => (
                    <div key={fp.embeddingIds[i] || i} className="flex items-center justify-between py-1 px-2 text-xs text-cc-muted">
                      <span>{label || "Default"}</span>
                      {fp.embeddingCount > 1 && fp.embeddingIds[i] && (
                        <button
                          onClick={() => removeEmbedding(fp.id, fp.embeddingIds[i])}
                          className="text-red-400 hover:text-red-300 cursor-pointer"
                        >
                          Remove
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </section>

      {/* Enrollment Mode */}
      {enrolling && (
        <section className="p-4 rounded-lg border border-cc-border bg-cc-input">
          <h3 className="text-sm font-semibold text-cc-fg mb-2">
            {enrollMode === "add-voiceprint" ? `Add Voiceprint to "${enrollTargetProfile?.name}"` : "New Speaker Profile"}
          </h3>
          <p className="text-xs text-cc-muted mb-3">
            Read the passages below aloud at your normal speaking pace. Each confirmed segment
            captures a voice sample. All 5 passages give the best fingerprint quality.
          </p>

          {enrollMode === "new" && (
            <input
              type="text"
              placeholder="Speaker name (e.g., John)"
              value={enrollName}
              onChange={(e) => setEnrollName(e.target.value)}
              className="w-full px-3 py-2 rounded-lg bg-cc-bg border border-cc-border text-cc-fg text-sm mb-3"
            />
          )}

          <input
            type="text"
            placeholder="Voiceprint label (e.g., Pixel Buds, Desktop Mic)"
            value={enrollLabel}
            onChange={(e) => setEnrollLabel(e.target.value)}
            className="w-full px-3 py-2 rounded-lg bg-cc-bg border border-cc-border text-cc-fg text-sm mb-3"
          />

          {/* Enrollment passages */}
          <div className="mb-3 space-y-2">
            {ENROLLMENT_PASSAGES.map((passage, i) => {
              const done = i < enrollSegments.length;
              const current = i === enrollSegments.length;
              return (
                <div
                  key={i}
                  className={`p-2.5 rounded-lg text-sm leading-relaxed border ${
                    done
                      ? "border-green-500/30 bg-green-500/5 text-cc-muted line-through"
                      : current
                        ? "border-cc-primary/50 bg-cc-primary/5 text-cc-fg"
                        : "border-cc-border bg-cc-bg text-cc-muted/60"
                  }`}
                >
                  <span className="text-xs font-mono text-cc-muted mr-2">{i + 1}.</span>
                  {passage}
                </div>
              );
            })}
          </div>

          {/* Voice activity indicator */}
          <div className="flex items-center gap-2 mb-3">
            <div className={`w-3 h-3 rounded-full ${enrollVadActive ? "bg-green-500" : "bg-cc-border"}`} />
            <span className="text-xs text-cc-muted">
              {enrollVadActive ? "Speaking..." : "Waiting for voice"}
            </span>
            <span className="text-xs text-cc-muted ml-auto">
              {Math.round(enrollRmsDb)} dB
            </span>
          </div>

          {/* Segment count */}
          <div className="text-xs text-cc-muted mb-3">
            Segments captured: {enrollSegments.length}
            {enrollSegments.length > 0 && (
              <span className="ml-2 text-cc-fg">
                &ldquo;{enrollSegments[enrollSegments.length - 1].transcript.slice(0, 50)}&rdquo;
              </span>
            )}
          </div>

          {/* Similarity scores against existing profiles */}
          {enrollSegments.length > 0 && enrollSegments[enrollSegments.length - 1].scores.length > 0 && (
            <div className="mb-3">
              <span className="text-xs text-cc-muted">Similarity to existing:</span>
              {enrollSegments[enrollSegments.length - 1].scores.map((s) => (
                <div key={s.id} className="flex items-center gap-2 mt-1">
                  <span className="text-xs text-cc-fg w-24 truncate">{s.name}</span>
                  <div className="flex-1 h-1.5 bg-cc-border rounded-full overflow-hidden">
                    <div
                      className={`h-full rounded-full ${s.similarity >= useStore.getState().speakerGateThreshold ? "bg-green-500" : "bg-red-400"}`}
                      style={{ width: `${Math.max(0, Math.min(100, s.similarity * 100))}%` }}
                    />
                  </div>
                  <span className={`text-xs ${s.similarity >= useStore.getState().speakerGateThreshold ? "text-green-500" : "text-red-400"}`}>
                    {s.similarity.toFixed(3)}
                  </span>
                </div>
              ))}
            </div>
          )}

          <div className="flex gap-2">
            <button
              onClick={cancelEnrollment}
              className="px-3 py-1.5 text-xs rounded-lg bg-cc-bg border border-cc-border text-cc-fg hover:bg-cc-hover cursor-pointer"
            >
              Cancel
            </button>
            <button
              onClick={saveEnrollment}
              disabled={enrollSegments.length === 0}
              className="px-3 py-1.5 text-xs rounded-lg bg-cc-primary text-white hover:opacity-90 disabled:opacity-50 cursor-pointer disabled:cursor-not-allowed"
            >
              Save ({enrollSegments.length} segments)
            </button>
          </div>
        </section>
      )}

      {/* Test Mode */}
      {testing && (
        <section className="p-4 rounded-lg border border-cc-border bg-cc-input">
          <h3 className="text-sm font-semibold text-cc-fg mb-2">Testing</h3>
          <p className="text-xs text-cc-muted mb-3">
            Speak to see which identity and voiceprint best matches your voice.
          </p>

          {!testHeard && (
            <p className="text-xs text-cc-muted mb-3">Waiting for voice segment...</p>
          )}

          <div className="mb-3 space-y-1.5">
            {testScores.map((s, i) => {
              const isBest = i === 0 && s.similarity > 0;
              return (
                <div key={s.id} className={`flex items-center gap-2 ${isBest ? "px-2 py-1.5 -mx-2 rounded-lg bg-green-500/10 border border-green-500/20" : ""}`}>
                  <div className="w-28 min-w-0">
                    <span className={`text-xs truncate block ${isBest ? "text-green-400 font-semibold" : "text-cc-fg"}`}>{s.name}</span>
                    {s.bestVoiceprint && s.similarity > 0 && (
                      <span className={`text-[10px] truncate block ${isBest ? "text-green-400/70" : "text-cc-muted"}`}>{s.bestVoiceprint}</span>
                    )}
                  </div>
                  <div className="flex-1 h-2 bg-cc-border rounded-full overflow-hidden">
                    <div
                      className={`h-full rounded-full transition-all ${isBest ? "bg-green-500" : s.similarity > 0 ? "bg-cc-muted/40" : ""}`}
                      style={{ width: `${Math.max(0, Math.min(100, s.similarity * 100))}%` }}
                    />
                  </div>
                  <span className={`text-xs font-mono ${isBest ? "text-green-400" : "text-cc-muted"}`}>
                    {s.similarity > 0 ? s.similarity.toFixed(3) : ""}
                  </span>
                </div>
              );
            })}
          </div>

          <button
            onClick={stopTest}
            className="px-3 py-1.5 text-xs rounded-lg bg-cc-bg border border-cc-border text-cc-fg hover:bg-cc-hover cursor-pointer"
          >
            Stop Test
          </button>
        </section>
      )}
    </div>
  );
}
