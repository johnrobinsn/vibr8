/**
 * Build a download target (URL + filename) for any artifact / pushed-content
 * payload, so the ViewerPane header can offer a Download button uniformly
 * across all the renderable types in `ContentRenderer.PushedContentView`.
 *
 * Returns null when the content type isn't a downloadable artifact (e.g.
 * `session` is a live chat mirror, `home` is the default view marker,
 * `desktop` is a live screen stream).
 *
 * For artifacts with `contentUrl` (the post-on-disk storage path), we return
 * the URL directly — the browser anchor `download` attribute handles the
 * save. For legacy inline-only artifacts we wrap the body in a Blob URL so
 * the same anchor pattern works. Callers must `revokeObjectURL` after use.
 */

const NON_DOWNLOADABLE_TYPES = new Set(["session", "home", "desktop"]);

const TEXT_TYPES = new Set(["markdown", "html", "file"]);

const DEFAULT_EXT_BY_TYPE: Record<string, string> = {
  markdown: ".md",
  html: ".html",
  pdf: ".pdf",
  file: ".txt",
  image: ".png",
  audio: ".opus",
  video: ".mp4",
};

const MIME_BY_EXT: Record<string, string> = {
  md: "text/markdown",
  txt: "text/plain",
  html: "text/html",
  pdf: "application/pdf",
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  webp: "image/webp",
  gif: "image/gif",
  svg: "image/svg+xml",
  opus: "audio/opus",
  ogg: "audio/ogg",
  mp3: "audio/mpeg",
  wav: "audio/wav",
  m4a: "audio/mp4",
  mp4: "video/mp4",
  webm: "video/webm",
};

const SANITIZE_RE = /[^A-Za-z0-9._-]+/g;

export interface DownloadTarget {
  /** Browser-friendly URL to put in <a href>. May be a blob: URL. */
  url: string;
  /** Suggested filename — sanitized and extension-bearing. */
  filename: string;
  /** True iff `url` is a blob: URL the caller created; revoke after use. */
  needsRevoke: boolean;
}

export interface DownloadableContent {
  type: string;
  content?: string;
  contentUrl?: string | null;
  filename?: string | null;
  title?: string | null;
}

/**
 * Derive a filename. Priority:
 *   1. `content.filename` (most precise — set by the artifact author).
 *   2. Sanitized `content.title` + extension inferred from `content.type`.
 *   3. Fallback "artifact" + extension.
 *
 * Always ends in an extension matching the content type, so the OS picks the
 * right viewer when the user opens the downloaded file.
 */
export function deriveFilename(content: DownloadableContent): string {
  if (content.filename && content.filename.trim()) {
    return content.filename;
  }
  const ext = DEFAULT_EXT_BY_TYPE[content.type] ?? ".bin";
  const base = (content.title ?? "artifact")
    .trim()
    .replace(SANITIZE_RE, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 80) || "artifact";
  // Avoid double-extension if the title already has one matching ours.
  if (base.toLowerCase().endsWith(ext.toLowerCase())) return base;
  return base + ext;
}

function mimeFromFilename(filename: string, fallback: string): string {
  const ext = filename.toLowerCase().split(".").pop();
  if (ext && MIME_BY_EXT[ext]) return MIME_BY_EXT[ext];
  return fallback;
}

/**
 * Resolve a download target for `content`. Returns null for non-downloadable
 * types or when there's no body and no contentUrl.
 */
export function getDownloadTarget(content: DownloadableContent): DownloadTarget | null {
  if (NON_DOWNLOADABLE_TYPES.has(content.type)) return null;

  const filename = deriveFilename(content);

  if (content.contentUrl) {
    return { url: content.contentUrl, filename, needsRevoke: false };
  }

  // No URL — synthesize a Blob from inline content. Text types use the
  // string body directly; binary types may have been base64'd into the
  // inline content (e.g. legacy image artifacts), in which case we decode.
  if (!content.content) return null;

  if (TEXT_TYPES.has(content.type)) {
    const mime = mimeFromFilename(filename, "text/plain");
    const blob = new Blob([content.content], { type: mime });
    return {
      url: URL.createObjectURL(blob),
      filename,
      needsRevoke: true,
    };
  }

  // Binary: try to decode base64. If the inline content is already a data:
  // URL we can hand it back unchanged.
  if (content.content.startsWith("data:")) {
    return { url: content.content, filename, needsRevoke: false };
  }

  try {
    const binary = atob(content.content);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const mime = mimeFromFilename(filename, "application/octet-stream");
    const blob = new Blob([bytes], { type: mime });
    return {
      url: URL.createObjectURL(blob),
      filename,
      needsRevoke: true,
    };
  } catch {
    // Not base64 either — give up rather than producing a broken download.
    return null;
  }
}
