import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { api, type VoiceSegment, type VoiceRecording } from "../api.js";

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function VoiceLogs() {
  const [segments, setSegments] = useState<VoiceSegment[]>([]);
  const [recordings, setRecordings] = useState<VoiceRecording[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);

  const load = useCallback(async (q: string) => {
    const [segs, recs] = await Promise.allSettled([
      api.listVoiceLogs({ q, limit: 500 }),
      api.listVoiceRecordings(),
    ]);
    if (segs.status === "fulfilled") setSegments(segs.value);
    if (recs.status === "fulfilled") setRecordings(recs.value);
    setLoading(false);
  }, []);

  useEffect(() => {
    load("");
  }, [load]);

  function handleSearch(value: string) {
    setQuery(value);
    clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setLoading(true);
      load(value);
    }, 300);
  }

  async function handleDelete(id: string) {
    try {
      await api.deleteVoiceLog(id);
      setSegments((prev) => prev.filter((s) => s.id !== id));
    } catch {
      // ignore
    }
  }

  async function handleClearAll() {
    if (!confirm("Delete all voice logs? This cannot be undone.")) return;
    try {
      await api.clearVoiceLogs();
      setSegments([]);
      setRecordings([]);
    } catch {
      // ignore
    }
  }

  // Group segments by recordingId
  const { grouped, orphans } = useMemo(() => {
    const recIds = new Set(recordings.map((r) => r.id));
    const byRecording = new Map<string, VoiceSegment[]>();
    const orphanList: VoiceSegment[] = [];

    for (const seg of segments) {
      if (seg.recordingId && recIds.has(seg.recordingId)) {
        let list = byRecording.get(seg.recordingId);
        if (!list) {
          list = [];
          byRecording.set(seg.recordingId, list);
        }
        list.push(seg);
      } else {
        orphanList.push(seg);
      }
    }

    // Sort segments within each group by timeBegin
    for (const list of byRecording.values()) {
      list.sort((a, b) => a.timeBegin - b.timeBegin);
    }

    return { grouped: byRecording, orphans: orphanList };
  }, [segments, recordings]);

  // Which recordings should auto-expand (have matching segments when searching)
  const expandedFromSearch = useMemo(() => {
    if (!query) return new Set<string>();
    const ids = new Set<string>();
    for (const [recId] of grouped) {
      ids.add(recId);
    }
    return ids;
  }, [query, grouped]);

  const hasContent = segments.length > 0 || recordings.length > 0;

  return (
    <div className="p-6 max-w-3xl mx-auto space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Voice Logs</h2>
        {hasContent && (
          <button
            onClick={handleClearAll}
            className="px-3 py-1.5 text-sm font-medium rounded-lg bg-red-500/10 text-red-500 hover:bg-red-500/20 transition-colors cursor-pointer"
          >
            Clear All
          </button>
        )}
      </div>

      {/* Search */}
      <div className="relative">
        <svg viewBox="0 0 16 16" fill="currentColor" className="w-4 h-4 text-cc-muted absolute left-3 top-1/2 -translate-y-1/2">
          <path d="M11.742 10.344a6.5 6.5 0 10-1.397 1.398h-.001c.03.04.062.078.098.115l3.85 3.85a1 1 0 001.415-1.414l-3.85-3.85a1.007 1.007 0 00-.115-.1zM12 6.5a5.5 5.5 0 11-11 0 5.5 5.5 0 0111 0z" />
        </svg>
        <input
          value={query}
          onChange={(e) => handleSearch(e.target.value)}
          placeholder="Search transcripts..."
          className="w-full pl-9 pr-3 py-2 text-sm rounded-lg bg-cc-sidebar border border-cc-border text-cc-fg outline-none focus:border-cc-primary/50"
        />
      </div>

      {loading ? (
        <p className="text-sm text-cc-muted">Loading...</p>
      ) : !hasContent ? (
        <p className="text-sm text-cc-muted">
          {query ? "No matching segments found." : "No voice segments recorded yet."}
        </p>
      ) : (
        <div className="space-y-3">
          {recordings.map((rec) => {
            const recSegments = grouped.get(rec.id) || [];
            if (query && recSegments.length === 0) return null;
            return (
              <RecordingGroup
                key={rec.id}
                recording={rec}
                segments={recSegments}
                defaultExpanded={expandedFromSearch.has(rec.id)}
                onDeleteSegment={handleDelete}
              />
            );
          })}

          {orphans.length > 0 && (
            <div className="space-y-1.5">
              {recordings.length > 0 && (
                <h3 className="text-xs font-medium text-cc-muted uppercase tracking-wide pt-2">Other</h3>
              )}
              {orphans.map((seg) => (
                <SegmentRow key={seg.id} segment={seg} onDelete={handleDelete} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function RecordingGroup({
  recording,
  segments,
  defaultExpanded,
  onDeleteSegment,
}: {
  recording: VoiceRecording;
  segments: VoiceSegment[];
  defaultExpanded: boolean;
  onDeleteSegment: (id: string) => void;
}) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const isComplete = recording.endedAt != null;
  const date = new Date(recording.startedAt * 1000);
  const sessionLabel = recording.sessionId.startsWith("playground-")
    ? "Playground"
    : recording.sessionId.slice(0, 8);

  // Auto-expand when search matches
  useEffect(() => {
    if (defaultExpanded) setExpanded(true);
  }, [defaultExpanded]);

  function seekTo(time: number) {
    if (audioRef.current && isComplete) {
      audioRef.current.currentTime = time;
      audioRef.current.play().catch(() => {});
    }
  }

  return (
    <div className="rounded-xl border border-cc-border overflow-hidden">
      {/* Header */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-3 px-4 py-3 bg-cc-sidebar hover:bg-cc-hover transition-colors cursor-pointer"
      >
        <svg
          viewBox="0 0 16 16"
          fill="currentColor"
          className={`w-3 h-3 text-cc-muted transition-transform ${expanded ? "rotate-90" : ""}`}
        >
          <path d="M6 3l5 5-5 5z" />
        </svg>
        <div className="flex-1 text-left">
          <span className="text-sm font-medium">
            {date.toLocaleDateString()} {date.toLocaleTimeString()}
          </span>
          <span className="text-xs text-cc-muted ml-2">{sessionLabel}</span>
        </div>
        <span className="text-xs text-cc-muted">
          {isComplete ? formatTime(recording.duration) : "In progress"}
        </span>
        <span className="text-xs text-cc-muted">
          {segments.length} segment{segments.length !== 1 ? "s" : ""}
        </span>
      </button>

      {expanded && (
        <div className="border-t border-cc-border">
          {/* Recording player */}
          <div className="px-4 py-3 bg-cc-bg/50">
            {isComplete ? (
              <audio
                ref={audioRef}
                controls
                preload="none"
                src={`/api/voice/recordings/${recording.id}/audio`}
                className="w-full h-8 [&::-webkit-media-controls-panel]:bg-cc-sidebar"
              />
            ) : (
              <div className="flex items-center gap-2 text-sm text-cc-muted">
                <button
                  disabled
                  className="w-8 h-8 rounded-full bg-cc-primary/5 flex items-center justify-center opacity-50"
                >
                  <svg viewBox="0 0 16 16" fill="currentColor" className="w-3.5 h-3.5 text-cc-muted">
                    <path d="M6 3.5v9l6-4.5z" />
                  </svg>
                </button>
                <span className="italic">Recording in progress...</span>
              </div>
            )}
          </div>

          {/* Segments */}
          {segments.length > 0 && (
            <div className="divide-y divide-cc-border">
              {segments.map((seg) => (
                <RecordingSegmentRow
                  key={seg.id}
                  segment={seg}
                  isRecordingComplete={isComplete}
                  onSeek={seekTo}
                  onDelete={onDeleteSegment}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function RecordingSegmentRow({
  segment,
  isRecordingComplete,
  onSeek,
  onDelete,
}: {
  segment: VoiceSegment;
  isRecordingComplete: boolean;
  onSeek: (time: number) => void;
  onDelete: (id: string) => void;
}) {
  const [playing, setPlaying] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  function togglePlay() {
    if (playing && audioRef.current) {
      audioRef.current.pause();
      setPlaying(false);
      return;
    }
    const audio = new Audio(`/api/voice/logs/${segment.id}/audio`);
    audioRef.current = audio;
    audio.onended = () => setPlaying(false);
    audio.play().catch(() => setPlaying(false));
    setPlaying(true);
  }

  const duration = segment.timeEnd - segment.timeBegin;

  return (
    <div className="flex items-start gap-2 px-4 py-2.5 group">
      {/* Time offset badge */}
      <button
        onClick={() => isRecordingComplete && onSeek(segment.timeBegin)}
        className={`text-[11px] font-mono px-1.5 py-0.5 rounded shrink-0 ${
          isRecordingComplete
            ? "bg-cc-primary/10 text-cc-primary hover:bg-cc-primary/20 cursor-pointer"
            : "bg-cc-hover text-cc-muted"
        }`}
        title={isRecordingComplete ? "Seek recording to this position" : undefined}
        disabled={!isRecordingComplete}
      >
        {formatTime(segment.timeBegin)}
      </button>

      {/* Segment play button */}
      <button
        onClick={togglePlay}
        className="w-6 h-6 rounded-full bg-cc-primary/10 hover:bg-cc-primary/20 flex items-center justify-center shrink-0 cursor-pointer transition-colors"
      >
        <svg viewBox="0 0 16 16" fill="currentColor" className="w-3 h-3 text-cc-primary">
          {playing ? (
            <path d="M5 3h2v10H5zM9 3h2v10H9z" />
          ) : (
            <path d="M6 3.5v9l6-4.5z" />
          )}
        </svg>
      </button>

      <div className="flex-1 min-w-0">
        <p className="text-sm leading-snug">{segment.transcript}</p>
        <span className="text-[11px] text-cc-muted">{formatTime(duration)}</span>
      </div>

      {/* Delete */}
      <button
        onClick={() => onDelete(segment.id)}
        className="p-1 rounded-lg hover:bg-cc-hover text-cc-muted hover:text-red-400 transition-all opacity-0 group-hover:opacity-100 cursor-pointer"
        title="Delete segment"
      >
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-3 h-3">
          <path d="M4 4l8 8M12 4l-8 8" />
        </svg>
      </button>
    </div>
  );
}

// Fallback row for orphan segments (no recording)
function SegmentRow({ segment, onDelete }: { segment: VoiceSegment; onDelete: (id: string) => void }) {
  const [playing, setPlaying] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  function togglePlay() {
    if (playing && audioRef.current) {
      audioRef.current.pause();
      setPlaying(false);
      return;
    }
    const audio = new Audio(`/api/voice/logs/${segment.id}/audio`);
    audioRef.current = audio;
    audio.onended = () => setPlaying(false);
    audio.play().catch(() => setPlaying(false));
    setPlaying(true);
  }

  const duration = segment.timeEnd - segment.timeBegin;
  const date = new Date(segment.createdAt * 1000);

  return (
    <div className="flex items-start gap-2 p-3 rounded-xl bg-cc-sidebar border border-cc-border group">
      <button
        onClick={togglePlay}
        className="w-8 h-8 rounded-full bg-cc-primary/10 hover:bg-cc-primary/20 flex items-center justify-center shrink-0 cursor-pointer transition-colors"
      >
        <svg viewBox="0 0 16 16" fill="currentColor" className="w-3.5 h-3.5 text-cc-primary">
          {playing ? (
            <path d="M5 3h2v10H5zM9 3h2v10H9z" />
          ) : (
            <path d="M6 3.5v9l6-4.5z" />
          )}
        </svg>
      </button>

      <div className="flex-1 min-w-0">
        <p className="text-sm leading-snug">{segment.transcript}</p>
        <div className="flex items-center gap-2 mt-1 text-[11px] text-cc-muted">
          <span>{date.toLocaleDateString()} {date.toLocaleTimeString()}</span>
          <span>{formatTime(duration)}</span>
          {segment.sessionId && (
            <span className="truncate max-w-[120px]" title={segment.sessionId}>
              {segment.sessionId.startsWith("playground-") ? "Playground" : segment.sessionId.slice(0, 8)}
            </span>
          )}
        </div>
      </div>

      <button
        onClick={() => onDelete(segment.id)}
        className="p-1.5 rounded-lg hover:bg-cc-hover text-cc-muted hover:text-red-400 transition-all opacity-0 group-hover:opacity-100 cursor-pointer"
        title="Delete segment"
      >
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-3.5 h-3.5">
          <path d="M4 4l8 8M12 4l-8 8" />
        </svg>
      </button>
    </div>
  );
}
