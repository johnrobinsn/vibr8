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

export function PushedContentView({
  content,
  onBack,
  backLabel = "Home",
  renderDesktop,
}: {
  content: { type: string; content: string; filename?: string; nodeId?: string };
  onBack: () => void;
  backLabel?: string;
  renderDesktop?: (props: { onHome: () => void; nodeId?: string }) => React.ReactNode;
}) {
  const [imgError, setImgError] = useState(false);
  const [imgLoaded, setImgLoaded] = useState(false);

  useEffect(() => {
    setImgError(false);
    setImgLoaded(false);
  }, [content.content]);

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
    let url = content.content.trim();
    if (url && !url.startsWith("data:") && !url.startsWith("http")) {
      url = `data:image/png;base64,${url}`;
    }
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
          <MarkdownContent text={content.content} />
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
          {content.content}
        </pre>
        <BackButton onClick={onBack} label={backLabel} />
      </div>
    );
  }

  if (content.type === "pdf") {
    return (
      <div className="h-full flex flex-col">
        <iframe
          src={content.content}
          className="flex-1 w-full border-0"
          title="PDF Viewer"
        />
        <BackButton onClick={onBack} label={backLabel} />
      </div>
    );
  }

  if (content.type === "html") {
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
    let url = content.content.trim();
    if (url && !url.startsWith("data:") && !url.startsWith("http")) {
      const ext = content.filename?.toLowerCase().split(".").pop();
      const mime = ext === "mp3" ? "audio/mpeg"
                 : ext === "ogg" || ext === "oga" ? "audio/ogg"
                 : "audio/wav";
      url = `data:${mime};base64,${url}`;
    }
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
