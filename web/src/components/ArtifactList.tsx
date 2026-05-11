import { useState, useEffect, useCallback } from "react";
import { useStore } from "../store.js";
import { api } from "../api.js";
import type { Artifact } from "../types.js";

const TYPE_ICONS: Record<string, string> = {
  markdown: "M4 4h16v12H4V4zm2 2v8h3l3-4 3 4h3V6H6z",
  image: "M4 4h16v12H4V4zm7 3a2 2 0 11-4 0 2 2 0 014 0zm-5 7l3-3 2 2 4-4 4 4v1H6z",
  file: "M6 2h8l4 4v12a2 2 0 01-2 2H6a2 2 0 01-2-2V4a2 2 0 012-2zm7 0v5h5",
  pdf: "M6 2h8l4 4v12a2 2 0 01-2 2H6a2 2 0 01-2-2V4a2 2 0 012-2zm7 0v5h5M9 13h6M9 16h4",
  html: "M4 4l2 14 6 2 6-2 2-14H4zm5 4h6l-.5 6-2.5 1-2.5-1-.2-2",
  audio: "M9 17V5l10-2v12M9 17a3 3 0 11-6 0 3 3 0 016 0zm10-2a3 3 0 11-6 0 3 3 0 016 0z",
};

function TypeIcon({ type }: { type: string }) {
  const d = TYPE_ICONS[type] || TYPE_ICONS.file;
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} className="w-4 h-4 shrink-0 text-cc-fg-muted">
      <path d={d} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function timeAgo(ts: number): string {
  const seconds = Math.floor((Date.now() - ts * 1000) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export function ArtifactList({ onSelect }: { onSelect: (artifact: Artifact) => void }) {
  const artifacts = useStore((s) => s.artifacts);
  const [filterSessionId, setFilterSessionId] = useState<string | null>(null);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; artifact: Artifact } | null>(null);

  const loadArtifacts = useCallback(() => {
    api.getArtifacts().then((a) => useStore.getState().setArtifacts(a)).catch(() => {});
  }, []);

  useEffect(() => {
    loadArtifacts();
  }, [loadArtifacts]);

  useEffect(() => {
    if (contextMenu) {
      const dismiss = () => setContextMenu(null);
      window.addEventListener("click", dismiss);
      return () => window.removeEventListener("click", dismiss);
    }
  }, [contextMenu]);

  const sessions = Array.from(new Set(artifacts.filter((a) => a.sourceSessionName).map((a) => a.sourceSessionName!)));
  const filtered = filterSessionId
    ? artifacts.filter((a) => a.sourceSessionId === filterSessionId)
    : artifacts;

  async function handleDelete(artifact: Artifact) {
    await api.deleteArtifact(artifact.id).catch(() => {});
    setContextMenu(null);
  }

  if (artifacts.length === 0) {
    return (
      <div className="h-full flex flex-col items-center justify-center text-cc-fg-muted text-sm px-4 text-center gap-2">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} className="w-8 h-8 opacity-40">
          <path d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        <div>No artifacts yet</div>
        <div className="text-xs opacity-60">Ring0 can save content here for quick reference</div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      {sessions.length > 1 && (
        <div className="shrink-0 px-3 py-2 border-b border-cc-border">
          <select
            value={filterSessionId ?? ""}
            onChange={(e) => setFilterSessionId(e.target.value || null)}
            className="w-full px-2 py-1 text-xs bg-cc-bg border border-cc-border rounded text-cc-fg"
          >
            <option value="">All sessions</option>
            {sessions.map((name) => {
              const a = artifacts.find((a) => a.sourceSessionName === name);
              return (
                <option key={a?.sourceSessionId ?? name} value={a?.sourceSessionId ?? ""}>
                  {name}
                </option>
              );
            })}
          </select>
        </div>
      )}
      <div className="flex-1 overflow-auto">
        {filtered.map((artifact) => (
          <button
            key={artifact.id}
            onClick={() => onSelect(artifact)}
            onContextMenu={(e) => {
              e.preventDefault();
              setContextMenu({ x: e.clientX, y: e.clientY, artifact });
            }}
            className="w-full text-left px-3 py-2.5 border-b border-cc-border hover:bg-cc-bg-hover transition-colors group"
          >
            <div className="flex items-start gap-2">
              <TypeIcon type={artifact.type} />
              <div className="min-w-0 flex-1">
                <div className="text-sm text-cc-fg truncate">{artifact.title}</div>
                <div className="flex items-center gap-2 text-[10px] text-cc-fg-muted mt-0.5">
                  {artifact.sourceSessionName && <span className="truncate">{artifact.sourceSessionName}</span>}
                  <span>{timeAgo(artifact.createdAt)}</span>
                </div>
              </div>
            </div>
          </button>
        ))}
      </div>
      {contextMenu && (
        <div
          className="fixed z-50 bg-cc-bg border border-cc-border rounded shadow-lg py-1 min-w-[120px]"
          style={{ left: contextMenu.x, top: contextMenu.y }}
        >
          <button
            onClick={() => handleDelete(contextMenu.artifact)}
            className="w-full text-left px-3 py-1.5 text-xs text-red-400 hover:bg-cc-bg-hover"
          >
            Delete
          </button>
        </div>
      )}
    </div>
  );
}
