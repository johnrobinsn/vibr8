"""Artifact storage — persistent curated content items shared by Ring0/sessions.

The metadata index (title, type, source, etc.) lives in
``~/.vibr8/artifacts.json``. Content bodies are stored as separate files under
``~/.vibr8/artifacts/<id>`` so an artifact's payload (a 10 MB audio file, a
PDF, a large markdown report) never has to ride inline through the
MCP/websocket transport — clients fetch it from
``GET /api/artifacts/<id>/content`` instead.

Legacy artifacts (created before this change) had their content inlined in
``artifacts.json``; they're served from the same content endpoint via a
fallback path, no migration step required.
"""

from __future__ import annotations

import base64
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

_ARTIFACTS_PATH = Path.home() / ".vibr8" / "artifacts.json"
_CONTENT_DIR = Path.home() / ".vibr8" / "artifacts"

# Types whose `content` field is base64-encoded bytes (rather than text).
# `download` is binary too, but it's served with Content-Disposition: attachment
# (see server/routes.py) so the browser saves it rather than rendering inline.
_BINARY_TYPES = {"audio", "image", "pdf", "download"}

# Types meant to be downloaded rather than rendered inline.
_DOWNLOAD_TYPES = {"download"}


def is_download_type(artifact_type: str) -> bool:
    return artifact_type in _DOWNLOAD_TYPES


# ── Index I/O ────────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    if _ARTIFACTS_PATH.exists():
        try:
            return json.loads(_ARTIFACTS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save(artifacts: list[dict]) -> None:
    _ARTIFACTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _ARTIFACTS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(artifacts, indent=2), encoding="utf-8")
    tmp.rename(_ARTIFACTS_PATH)


# ── Content I/O ──────────────────────────────────────────────────────────────

def _content_path(artifact_id: str) -> Path:
    return _CONTENT_DIR / artifact_id


def _content_url(artifact_id: str) -> str:
    return f"/api/artifacts/{artifact_id}/content"


def _mime_for(artifact: dict) -> str:
    """Best-effort MIME type from `type` + filename extension."""
    typ = artifact.get("type", "")
    filename = (artifact.get("filename") or "").lower()
    ext = filename.rsplit(".", 1)[-1] if "." in filename else ""

    if typ == "audio":
        return {
            "mp3": "audio/mpeg",
            "ogg": "audio/ogg", "oga": "audio/ogg",
            "wav": "audio/wav", "wave": "audio/wav",
            "m4a": "audio/mp4",
            "flac": "audio/flac",
            "webm": "audio/webm",
        }.get(ext, "audio/wav")
    if typ == "image":
        return {
            "png": "image/png",
            "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
            "svg": "image/svg+xml",
        }.get(ext, "image/png")
    if typ == "pdf":
        return "application/pdf"
    if typ == "html":
        return "text/html; charset=utf-8"
    if typ == "markdown":
        return "text/markdown; charset=utf-8"
    if typ == "file":
        return "text/plain; charset=utf-8"
    if typ == "download":
        # Pick the MIME the browser (and Android, for APKs) expects so the
        # native install / save dialog fires correctly. Fall back to
        # octet-stream for anything we don't know.
        return {
            "apk": "application/vnd.android.package-archive",
            "zip": "application/zip",
            "tar": "application/x-tar",
            "gz": "application/gzip", "tgz": "application/gzip",
            "bz2": "application/x-bzip2",
            "xz": "application/x-xz",
            "7z": "application/x-7z-compressed",
            "dmg": "application/x-apple-diskimage",
            "iso": "application/x-iso9660-image",
            "deb": "application/vnd.debian.binary-package",
            "rpm": "application/x-rpm",
            "exe": "application/octet-stream",
            "msi": "application/octet-stream",
            "bin": "application/octet-stream",
        }.get(ext, "application/octet-stream")
    return "application/octet-stream"


def _write_content(artifact_id: str, artifact_type: str, raw_content: str) -> int:
    """Write the raw `content` field to disk in its canonical form.

    Binary types come in as base64 strings; we decode them so the file holds
    real bytes and `<audio>`/`<img>`/`<iframe>`/`<a download>` can stream them
    directly without per-request decoding. Text types are written as UTF-8.
    Returns the number of bytes written.
    """
    _CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    path = _content_path(artifact_id)
    if artifact_type in _BINARY_TYPES:
        try:
            decoded = base64.b64decode(raw_content)
        except Exception:
            # Caller sent something that isn't valid base64 — keep it as raw
            # bytes so we don't lose data, but the rendered output may be junk.
            decoded = raw_content.encode("utf-8", errors="replace") if isinstance(raw_content, str) else b""
        path.write_bytes(decoded)
        return len(decoded)
    text = raw_content if isinstance(raw_content, str) else ""
    encoded = text.encode("utf-8")
    path.write_bytes(encoded)
    return len(encoded)


def _copy_from_file(artifact_id: str, source_path: str) -> int:
    """Copy a local file into the artifact store. Used by `sourceFilePath` to
    avoid the base64 round-trip on large binary payloads (e.g. APKs). Returns
    the size of the copied file. Raises on failure."""
    src = Path(source_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"sourceFilePath does not exist: {source_path}")
    _CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    dst = _content_path(artifact_id)
    shutil.copyfile(src, dst)
    return dst.stat().st_size


# ── Public API ───────────────────────────────────────────────────────────────

def list_artifacts(session_id: str | None = None) -> list[dict]:
    """Return metadata only — never the inline `content` field for new
    artifacts (clients fetch payload via ``contentUrl``). Legacy artifacts
    keep their inline content for backwards compat with older clients.
    """
    artifacts = _load()
    if session_id:
        artifacts = [a for a in artifacts if a.get("sourceSessionId") == session_id]

    out: list[dict] = []
    for a in artifacts:
        if a.get("contentUrl"):
            # New format: drop inline content from list responses
            slim = {k: v for k, v in a.items() if k != "content"}
            out.append(slim)
        else:
            # Legacy: keep inline content but advertise a URL too — the route
            # falls back to the inline blob, so clients can use the URL path
            # uniformly going forward.
            with_url = dict(a)
            with_url["contentUrl"] = _content_url(a["id"])
            out.append(with_url)
    out.sort(key=lambda a: a.get("createdAt", 0), reverse=True)
    return out


def get_artifact(artifact_id: str) -> dict | None:
    for a in _load():
        if a["id"] == artifact_id:
            return a
    return None


def create_artifact(username: str, data: dict) -> dict:
    artifact_id = str(uuid.uuid4())
    artifact_type = data.get("type", "markdown")
    source_file_path = data.get("sourceFilePath")
    filename = data.get("filename")

    if source_file_path:
        # Server-local file path — copy bytes directly, skip the base64
        # round-trip. The caller (Ring0 / a session MCP) is co-located with
        # the vibr8 server so this just hits the local filesystem.
        size = _copy_from_file(artifact_id, source_file_path)
        if not filename:
            filename = Path(source_file_path).name
    else:
        raw_content = data.get("content", "")
        size = _write_content(artifact_id, artifact_type, raw_content)

    artifact = {
        "id": artifact_id,
        "title": data.get("title", "Untitled"),
        "type": artifact_type,
        "contentUrl": _content_url(artifact_id),
        "size": size,
        "sourceSessionId": data.get("sourceSessionId"),
        "sourceSessionName": data.get("sourceSessionName"),
        "createdAt": time.time(),
        "filename": filename,
        "username": username,
    }
    artifacts = _load()
    artifacts.append(artifact)
    _save(artifacts)
    return artifact


def delete_artifact(artifact_id: str) -> bool:
    path = _content_path(artifact_id)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass

    artifacts = _load()
    before = len(artifacts)
    artifacts = [a for a in artifacts if a["id"] != artifact_id]
    if len(artifacts) == before:
        return False
    _save(artifacts)
    return True


def read_content(artifact_id: str) -> Optional[tuple[bytes, str, str | None]]:
    """Return (bytes, mime, filename) for an artifact, or None if not found.

    Prefers the on-disk content file; falls back to the inline ``content``
    field on legacy artifacts.
    """
    artifact = get_artifact(artifact_id)
    if not artifact:
        return None
    mime = _mime_for(artifact)
    filename = artifact.get("filename")

    path = _content_path(artifact_id)
    if path.exists():
        return path.read_bytes(), mime, filename

    inline = artifact.get("content", "")
    if not inline:
        # No on-disk file and no inline content. Empty body is a valid
        # response for an existing artifact (e.g. a deliberately-empty note).
        return b"", mime, filename

    if artifact.get("type") in _BINARY_TYPES:
        try:
            return base64.b64decode(inline), mime, filename
        except Exception:
            return None
    return inline.encode("utf-8"), mime, filename
