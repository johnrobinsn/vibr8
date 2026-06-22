import { useState } from "react";
import { useStore } from "../store.js";
import { PushedContentView } from "./ContentRenderer.js";
import { ArtifactList } from "./ArtifactList.js";
import { getDownloadTarget } from "../utils/artifact-download.js";
import type { Artifact } from "../types.js";

type View = "list" | "pushed" | "artifact";

export function ViewerPane() {
  const viewerPaneContent = useStore((s) => s.viewerPaneContent);
  const [viewingArtifact, setViewingArtifact] = useState<Artifact | null>(null);

  const view: View = viewerPaneContent ? "pushed" : viewingArtifact ? "artifact" : "list";

  const title =
    view === "pushed"
      ? viewerPaneContent?.filename || "Content"
      : view === "artifact"
        ? viewingArtifact!.title
        : "Artifacts";

  // Build a download target for whichever payload is currently on display.
  // Works for both `artifact` (user-clicked artifact list entry) and
  // `pushed` (Ring0 push via show_on_second_screen) since both eventually
  // funnel through `PushedContentView`. Returns null for non-downloadable
  // types (session mirror, home, live desktop), in which case we hide the
  // button rather than render a broken link.
  const downloadable =
    view === "artifact" && viewingArtifact
      ? getDownloadTarget({
          type: viewingArtifact.type,
          content: viewingArtifact.content,
          contentUrl: viewingArtifact.contentUrl ?? undefined,
          filename: viewingArtifact.filename ?? undefined,
          title: viewingArtifact.title,
        })
      : view === "pushed" && viewerPaneContent
        ? getDownloadTarget({
            type: viewerPaneContent.type,
            content: viewerPaneContent.content,
            contentUrl: viewerPaneContent.contentUrl,
            filename: viewerPaneContent.filename,
            title: viewerPaneContent.filename,
          })
        : null;

  function handleBack() {
    if (view === "pushed") {
      useStore.getState().setViewerPaneContent(null);
    } else {
      setViewingArtifact(null);
    }
  }

  function handleClose() {
    useStore.getState().setViewerPaneOpen(false);
  }

  function handleDownload() {
    if (!downloadable) return;
    const a = document.createElement("a");
    a.href = downloadable.url;
    a.download = downloadable.filename;
    // rel="noopener" is harmless here but matches the broader app pattern
    // of treating user-clicked content links defensively.
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    // Blob URLs are revocable; let the browser hold onto it long enough to
    // start the save dialog (10s is generous and prevents the dialog
    // racing with revoke on very large downloads).
    if (downloadable.needsRevoke) {
      setTimeout(() => URL.revokeObjectURL(downloadable.url), 10_000);
    }
  }

  return (
    <div className="h-full flex flex-col bg-cc-bg">
      {/* Header */}
      <div className="shrink-0 flex items-center gap-2 px-3 py-2 border-b border-cc-border">
        {view !== "list" && (
          <button
            onClick={handleBack}
            className="p-1 rounded hover:bg-cc-bg-hover text-cc-fg-muted hover:text-cc-fg transition-colors"
            title="Back"
          >
            <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
              <path fillRule="evenodd" d="M12.79 5.23a.75.75 0 01-.02 1.06L8.832 10l3.938 3.71a.75.75 0 11-1.04 1.08l-4.5-4.25a.75.75 0 010-1.08l4.5-4.25a.75.75 0 011.06.02z" clipRule="evenodd" />
            </svg>
          </button>
        )}
        <span className="flex-1 text-xs font-medium text-cc-fg truncate">{title}</span>
        {downloadable && (
          <button
            onClick={handleDownload}
            className="p-1 rounded hover:bg-cc-bg-hover text-cc-fg-muted hover:text-cc-fg transition-colors"
            title={`Download ${downloadable.filename}`}
            aria-label="Download"
          >
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.5} className="w-4 h-4">
              <path d="M10 3v10m0 0l-4-4m4 4l4-4M4 17h12" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        )}
        <button
          onClick={handleClose}
          className="p-1 rounded hover:bg-cc-bg-hover text-cc-fg-muted hover:text-cc-fg transition-colors"
          title="Close"
        >
          <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
            <path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z" />
          </svg>
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 min-h-0">
        {view === "pushed" && viewerPaneContent && (
          <PushedContentView
            key={viewerPaneContent._pushId ?? 0}
            content={viewerPaneContent}
            onBack={handleBack}
            backLabel="Back to artifacts"
          />
        )}
        {view === "artifact" && viewingArtifact && (
          <PushedContentView
            content={{
              type: viewingArtifact.type,
              content: viewingArtifact.content,
              contentUrl: viewingArtifact.contentUrl ?? undefined,
              filename: viewingArtifact.filename ?? undefined,
            }}
            onBack={handleBack}
            backLabel="Back to artifacts"
          />
        )}
        {view === "list" && (
          <ArtifactList onSelect={setViewingArtifact} />
        )}
      </div>
    </div>
  );
}
