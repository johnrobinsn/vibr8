// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  deriveFilename,
  getDownloadTarget,
} from "./artifact-download.js";

// Provide a deterministic createObjectURL/revokeObjectURL for blob assertions.
beforeEach(() => {
  let n = 0;
  vi.spyOn(URL, "createObjectURL").mockImplementation(() => `blob:fake-${++n}`);
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
});

describe("deriveFilename", () => {
  it("prefers the explicit filename when set", () => {
    expect(deriveFilename({ type: "markdown", filename: "notes.md", title: "Notes" }))
      .toBe("notes.md");
  });

  it("falls back to sanitized title + type-based extension", () => {
    expect(deriveFilename({ type: "markdown", title: "Meeting Notes — 2026" }))
      .toBe("Meeting_Notes_2026.md");
    expect(deriveFilename({ type: "html", title: "Hello World" }))
      .toBe("Hello_World.html");
    expect(deriveFilename({ type: "pdf", title: "Quarterly Report" }))
      .toBe("Quarterly_Report.pdf");
  });

  it("doesn't double-extension when the title already ends in the type ext", () => {
    expect(deriveFilename({ type: "markdown", title: "readme.md" }))
      .toBe("readme.md");
  });

  it("uses 'artifact' + extension when neither filename nor title is useful", () => {
    expect(deriveFilename({ type: "markdown" })).toBe("artifact.md");
    expect(deriveFilename({ type: "markdown", title: "   " })).toBe("artifact.md");
  });

  it("uses .bin for unknown types so the OS doesn't guess wrong", () => {
    expect(deriveFilename({ type: "weird-new-type", title: "thing" })).toBe("thing.bin");
  });
});

describe("getDownloadTarget", () => {
  it("returns null for non-downloadable types (session, home, desktop)", () => {
    expect(getDownloadTarget({ type: "session", content: "s1" })).toBeNull();
    expect(getDownloadTarget({ type: "home", content: "" })).toBeNull();
    expect(getDownloadTarget({ type: "desktop", content: "" })).toBeNull();
  });

  it("returns the contentUrl directly when available — no blob round-trip", () => {
    const t = getDownloadTarget({
      type: "markdown",
      contentUrl: "/api/artifacts/abc123/content",
      filename: "notes.md",
    });
    expect(t).not.toBeNull();
    expect(t!.url).toBe("/api/artifacts/abc123/content");
    expect(t!.filename).toBe("notes.md");
    expect(t!.needsRevoke).toBe(false);
  });

  it("wraps inline text content in a Blob URL for legacy artifacts", () => {
    const t = getDownloadTarget({
      type: "markdown",
      content: "# Hello\n\nworld",
      title: "Notes",
    });
    expect(t).not.toBeNull();
    expect(t!.url).toMatch(/^blob:/);
    expect(t!.filename).toBe("Notes.md");
    expect(t!.needsRevoke).toBe(true);
  });

  it("passes through data: URLs unchanged for inline binary content", () => {
    const dataUrl = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=";
    const t = getDownloadTarget({
      type: "image",
      content: dataUrl,
      filename: "pixel.png",
    });
    expect(t).not.toBeNull();
    expect(t!.url).toBe(dataUrl);
    expect(t!.needsRevoke).toBe(false);
  });

  it("decodes base64 inline binary content into a Blob URL", () => {
    // 1x1 PNG, base64 (no data: prefix)
    const b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=";
    const t = getDownloadTarget({
      type: "image",
      content: b64,
      filename: "pixel.png",
    });
    expect(t).not.toBeNull();
    expect(t!.url).toMatch(/^blob:/);
    expect(t!.needsRevoke).toBe(true);
  });

  it("returns null when there's no contentUrl AND no content body", () => {
    expect(getDownloadTarget({ type: "markdown" })).toBeNull();
    expect(getDownloadTarget({ type: "markdown", content: "" })).toBeNull();
  });

  it("gives up rather than producing a broken blob for unrecognized binary inline data", () => {
    const t = getDownloadTarget({
      type: "pdf",
      content: "this is not base64 and has no data: prefix !!!@@@@",
      filename: "doc.pdf",
    });
    // atob would throw; we return null so the UI hides the button.
    expect(t).toBeNull();
  });
});
