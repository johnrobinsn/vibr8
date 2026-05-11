import { useState, useEffect } from "react";
import { MarkdownContent } from "./MessageBubble.js";

export function BackButton({ onClick, label = "Home" }: { onClick: () => void; label?: string }) {
  return (
    <div className="text-center mt-4 pb-4">
      <button onClick={onClick} className="text-xs text-cc-fg-muted hover:text-cc-fg transition-colors">
        {label}
      </button>
    </div>
  );
}

type ContentLike = {
  type: string;
  content: string;
  contentUrl?: string;
  filename?: string;
  nodeId?: string;
};

const TEXT_TYPES = new Set(["markdown", "html", "file"]);

/**
 * Resolve the text body for markdown/html/file content. When `contentUrl` is
 * set we fetch it (artifact content lives on disk and is served from
 * /api/artifacts/:id/content). Otherwise the inline `content` field is the
 * body and there's no network round-trip.
 *
 * Returns `{ text, loading, error }`. For non-text types, `text` is the
 * inline content unchanged and `loading` is always false.
 */
function useResolvedText(content: ContentLike): { text: string; loading: boolean; error: string | null } {
  const needsFetch = !!content.contentUrl && TEXT_TYPES.has(content.type);
  const [fetched, setFetched] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!needsFetch) {
      setFetched(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setFetched(null);
    setError(null);
    fetch(content.contentUrl!)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.text();
      })
      .then((t) => { if (!cancelled) setFetched(t); })
      .catch((err) => { if (!cancelled) setError(err?.message || String(err)); });
    return () => { cancelled = true; };
  }, [needsFetch, content.contentUrl]);

  if (!needsFetch) return { text: content.content, loading: false, error: null };
  if (error) return { text: "", loading: false, error };
  if (fetched === null) return { text: "", loading: true, error: null };
  return { text: fetched, loading: false, error: null };
}

/** Pick the right URL for a media artifact: prefer the server URL, else
 *  passthrough if the inline content is already a data: or http URL, else
 *  wrap raw base64 in a data URI with a sensible default mime. */
function resolveMediaUrl(content: ContentLike, defaultMime: string): string {
  if (content.contentUrl) return content.contentUrl;
  const c = content.content.trim();
  if (!c) return "";
  if (c.startsWith("data:") || c.startsWith("http")) return c;
  return `data:${defaultMime};base64,${c}`;
}

function audioMimeFromFilename(filename: string | undefined): string {
  const ext = filename?.toLowerCase().split(".").pop();
  if (ext === "mp3") return "audio/mpeg";
  if (ext === "ogg" || ext === "oga") return "audio/ogg";
  return "audio/wav";
}

export function PushedContentView({
  content,
  onBack,
  backLabel = "Home",
  renderDesktop,
}: {
  content: ContentLike;
  onBack: () => void;
  backLabel?: string;
  renderDesktop?: (props: { onHome: () => void; nodeId?: string }) => React.ReactNode;
}) {
  const [imgError, setImgError] = useState(false);
  const [imgLoaded, setImgLoaded] = useState(false);
  const { text, loading: textLoading, error: textError } = useResolvedText(content);

  useEffect(() => {
    setImgError(false);
    setImgLoaded(false);
  }, [content.content, content.contentUrl]);

  if (content.type === "desktop") {
    if (renderDesktop) {
      return <>{renderDesktop({ onHome: onBack, nodeId: content.nodeId })}</>;
    }
    return (
      <div className="h-full flex items-center justify-center text-cc-fg-muted text-sm">
        Desktop streaming is only available on external second screens.
        <BackButton onClick={onBack} label={backLabel} />
      </div>
    );
  }

  if (content.type === "image") {
    const url = resolveMediaUrl(content, "image/png");
    return (
      <div className="h-full flex flex-col items-center justify-center p-8">
        {!imgLoaded && !imgError && (
          <div className="text-cc-fg-muted text-sm mb-4">Loading image…</div>
        )}
        {imgError ? (
          <div className="text-red-400 text-sm">
            Failed to load image
            <div className="text-xs text-cc-fg-muted mt-1 break-all max-w-lg">{url}</div>
          </div>
        ) : (
          <img
            src={url}
            alt=""
            className="max-w-full max-h-[85vh] object-contain rounded"
            onLoad={() => setImgLoaded(true)}
            onError={() => setImgError(true)}
          />
        )}
        <BackButton onClick={onBack} label={backLabel} />
      </div>
    );
  }

  if (content.type === "markdown") {
    return (
      <div className="h-full overflow-auto p-8">
        <div className="max-w-3xl mx-auto">
          {textLoading && <div className="text-cc-fg-muted text-sm">Loading…</div>}
          {textError && <div className="text-red-400 text-sm">Failed to load: {textError}</div>}
          {!textLoading && !textError && <MarkdownContent text={text} />}
        </div>
        <BackButton onClick={onBack} label={backLabel} />
      </div>
    );
  }

  if (content.type === "file") {
    return (
      <div className="h-full flex flex-col">
        {content.filename && (
          <div className="shrink-0 px-4 py-2 bg-cc-code-bg border-b border-cc-border font-mono text-sm text-cc-fg-muted">
            {content.filename}
          </div>
        )}
        <pre className="flex-1 overflow-auto p-4 text-sm font-mono leading-relaxed bg-cc-code-bg text-cc-code-fg whitespace-pre-wrap">
          {textLoading ? "Loading…" : textError ? `Failed to load: ${textError}` : text}
        </pre>
        <BackButton onClick={onBack} label={backLabel} />
      </div>
    );
  }

  if (content.type === "pdf") {
    const url = resolveMediaUrl(content, "application/pdf");
    return (
      <div className="h-full flex flex-col">
        <iframe
          src={url}
          className="flex-1 w-full border-0"
          title="PDF Viewer"
        />
        <BackButton onClick={onBack} label={backLabel} />
      </div>
    );
  }

  if (content.type === "html") {
    // <iframe srcDoc> needs the HTML string; we can't point it at a URL via
    // srcDoc, so for URL-backed HTML artifacts we use src= against the
    // content endpoint instead. The server tags those responses as text/html
    // so the iframe renders correctly.
    if (content.contentUrl) {
      return (
        <div className="h-full flex flex-col">
          <iframe
            src={content.contentUrl}
            className="flex-1 w-full border-0 bg-white"
            title="HTML Content"
            sandbox="allow-scripts allow-same-origin"
          />
          <BackButton onClick={onBack} label={backLabel} />
        </div>
      );
    }
    return (
      <div className="h-full flex flex-col">
        <iframe
          srcDoc={content.content}
          className="flex-1 w-full border-0 bg-white"
          title="HTML Content"
          sandbox="allow-scripts allow-same-origin"
        />
        <BackButton onClick={onBack} label={backLabel} />
      </div>
    );
  }

  if (content.type === "audio") {
    const url = resolveMediaUrl(content, audioMimeFromFilename(content.filename));
    return (
      <div className="h-full flex flex-col items-center justify-center p-8">
        {content.filename && (
          <div className="text-xs text-cc-fg-muted mb-3 font-mono break-all max-w-lg text-center">
            {content.filename}
          </div>
        )}
        <audio controls preload="metadata" src={url} className="w-full max-w-lg" />
        <BackButton onClick={onBack} label={backLabel} />
      </div>
    );
  }

  return (
    <div className="h-full overflow-auto p-8">
      <div className="max-w-3xl mx-auto">
        <pre className="whitespace-pre-wrap text-sm leading-relaxed">{content.content}</pre>
      </div>
      <BackButton onClick={onBack} label={backLabel} />
    </div>
  );
}
