"""Persistent session storage backed by JSON files on disk.

Originally ported from The Vibe Companion (session-store.ts).
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .session_types import (
    BrowserIncomingMessage,
    PermissionRequest,
    SessionState,
)

logger = logging.getLogger(__name__)

# ─── Serializable session shape ─────────────────────────────────────────────


@dataclass
class PersistedSession:
    id: str
    state: SessionState
    messageHistory: List[BrowserIncomingMessage]
    pendingMessages: List[str]
    pendingPermissions: List[Tuple[str, PermissionRequest]]
    archived: Optional[bool] = None
    name: Optional[str] = None
    lastPromptedAt: Optional[float] = None  # ms since epoch
    associatedNodeId: Optional[str] = None  # For sessions on host targeting an Android node

    # -- Serialization helpers ------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id,
            "state": self.state,
            "messageHistory": self.messageHistory,
            "pendingMessages": self.pendingMessages,
            "pendingPermissions": [list(pair) for pair in self.pendingPermissions],
        }
        if self.archived is not None:
            d["archived"] = self.archived
        if self.name is not None:
            d["name"] = self.name
        if self.lastPromptedAt is not None:
            d["lastPromptedAt"] = self.lastPromptedAt
        if self.associatedNodeId is not None:
            d["associatedNodeId"] = self.associatedNodeId
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PersistedSession:
        return cls(
            id=data["id"],
            state=data["state"],
            messageHistory=data.get("messageHistory", []),
            pendingMessages=data.get("pendingMessages", []),
            pendingPermissions=[
                (pair[0], pair[1])
                for pair in data.get("pendingPermissions", [])
            ],
            archived=data.get("archived"),
            name=data.get("name"),
            lastPromptedAt=data.get("lastPromptedAt"),
            associatedNodeId=data.get("associatedNodeId"),
        )


# ─── Store ──────────────────────────────────────────────────────────────────

DEFAULT_DIR = Path.home() / ".vibr8" / "sessions"
ARCHIVE_DIR = Path.home() / ".vibr8" / "archives"


class SessionStore:
    def __init__(self, directory: Optional[str] = None) -> None:
        self._dir = Path(directory) if directory else DEFAULT_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._debounce_timers: Dict[str, asyncio.TimerHandle] = {}

    # -- Private helpers ------------------------------------------------------

    def _file_path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.json"

    # -- Public API -----------------------------------------------------------

    def save(self, session: PersistedSession) -> None:
        """Debounced write -- batches rapid changes (e.g. multiple stream events)."""
        existing = self._debounce_timers.pop(session.id, None)
        if existing is not None:
            existing.cancel()

        loop = asyncio.get_event_loop()
        handle = loop.call_later(0.15, self.save_sync, session)
        self._debounce_timers[session.id] = handle

    def has_pending_saves(self) -> set[str]:
        """Return session IDs with pending debounced saves."""
        return set(self._debounce_timers.keys())

    def cancel_pending(self) -> None:
        """Cancel all pending debounce timers (call after flushing externally)."""
        for handle in self._debounce_timers.values():
            handle.cancel()
        self._debounce_timers.clear()

    def save_sync(self, session: PersistedSession) -> None:
        """Immediate write -- use for critical state changes.

        Uses atomic write (temp file + rename) so a full-disk scenario
        cannot truncate the existing file to 0 bytes.
        """
        # Clean up timer reference if we were called by the debounce callback.
        self._debounce_timers.pop(session.id, None)
        target = self._file_path(session.id)
        try:
            data = json.dumps(session.to_dict())
            tmp = target.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(target)
        except Exception:
            logger.exception("Failed to save session %s", session.id)

    def load(self, session_id: str) -> Optional[PersistedSession]:
        """Load a single session from disk."""
        try:
            raw = self._file_path(session_id).read_text(encoding="utf-8")
            return PersistedSession.from_dict(json.loads(raw))
        except Exception:
            return None

    def load_all(self) -> List[PersistedSession]:
        """Load all sessions from disk."""
        sessions: List[PersistedSession] = []
        try:
            for file in self._dir.iterdir():
                if file.suffix == ".json" and file.name != "launcher.json":
                    try:
                        raw = file.read_text(encoding="utf-8")
                        sessions.append(PersistedSession.from_dict(json.loads(raw)))
                    except Exception:
                        # Skip corrupt files
                        pass
        except Exception:
            # Dir doesn't exist yet
            pass
        return sessions

    def set_archived(self, session_id: str, archived: bool) -> bool:
        """Set the archived flag on a persisted session."""
        session = self.load(session_id)
        if session is None:
            return False
        session.archived = archived
        self.save_sync(session)
        return True

    def remove(self, session_id: str) -> None:
        """Remove a session file from disk."""
        handle = self._debounce_timers.pop(session_id, None)
        if handle is not None:
            handle.cancel()
        try:
            self._file_path(session_id).unlink()
        except Exception:
            # File may not exist
            pass

    def save_launcher(self, data: Any) -> None:
        """Persist launcher state (separate file)."""
        target = self._dir / "launcher.json"
        try:
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(target)
        except Exception:
            logger.exception("Failed to save launcher state")

    def load_launcher(self) -> Any:
        """Load launcher state."""
        try:
            raw = (self._dir / "launcher.json").read_text(encoding="utf-8")
            return json.loads(raw)
        except Exception:
            return None

    @property
    def directory(self) -> Path:
        return self._dir

    # -- Archive API ----------------------------------------------------------

    def archive_messages(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """Append messages to date-organized JSONL archive files."""
        if not messages:
            return
        archive_dir = ARCHIVE_DIR / session_id
        archive_dir.mkdir(parents=True, exist_ok=True)

        # Group messages by date
        by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for msg in messages:
            ts = msg.get("timestamp")
            if ts:
                if ts > 1e12:  # milliseconds → seconds
                    ts = ts / 1000
                date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            else:
                date_str = datetime.now().strftime("%Y-%m-%d")
            by_date[date_str].append(msg)

        # Append to JSONL files
        for date_str, msgs in by_date.items():
            jsonl_path = archive_dir / f"{date_str}.jsonl"
            with open(jsonl_path, "a", encoding="utf-8") as f:
                for msg in msgs:
                    f.write(json.dumps(msg, separators=(",", ":")) + "\n")

        # Update metadata
        self._update_archive_meta(session_id, messages)
        logger.info("[session-store] Archived %d messages for session %s", len(messages), session_id[:8])

    def _update_archive_meta(self, session_id: str, new_messages: List[Dict[str, Any]]) -> None:
        meta_path = ARCHIVE_DIR / session_id / "meta.json"
        meta: Dict[str, Any] = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        prev_count = meta.get("totalArchivedMessages", 0)
        meta["totalArchivedMessages"] = prev_count + len(new_messages)

        # Track timestamp range
        timestamps = [m.get("timestamp", 0) for m in new_messages if m.get("timestamp")]
        if timestamps:
            first = min(timestamps)
            last = max(timestamps)
            existing_first = meta.get("firstArchivedTimestamp")
            meta["firstArchivedTimestamp"] = min(first, existing_first) if existing_first else first
            meta["lastArchivedTimestamp"] = max(last, meta.get("lastArchivedTimestamp", 0))

        # List archive files
        archive_dir = ARCHIVE_DIR / session_id
        meta["archiveFiles"] = sorted(f.name for f in archive_dir.glob("*.jsonl"))

        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def has_archive(self, session_id: str) -> bool:
        return (ARCHIVE_DIR / session_id / "meta.json").exists()

    def get_archive_meta(self, session_id: str) -> Dict[str, Any]:
        meta_path = ARCHIVE_DIR / session_id / "meta.json"
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def load_archive(
        self,
        session_id: str,
        date: Optional[str] = None,
        offset: int = 0,
        limit: int = 100,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Load archived messages with optional date filter and pagination.

        Returns (messages, total_count).
        """
        archive_dir = ARCHIVE_DIR / session_id
        if not archive_dir.exists():
            return [], 0

        if date:
            jsonl_path = archive_dir / f"{date}.jsonl"
            if not jsonl_path.exists():
                return [], 0
            lines = [l for l in jsonl_path.read_text(encoding="utf-8").strip().split("\n") if l]
            total = len(lines)
            selected = lines[offset:offset + limit]
            return [json.loads(line) for line in selected], total

        # Load all files in chronological order
        all_messages: List[Dict[str, Any]] = []
        for f in sorted(archive_dir.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").strip().split("\n"):
                if line:
                    all_messages.append(json.loads(line))
        total = len(all_messages)
        return all_messages[offset:offset + limit], total

    def list_archive_dates(self, session_id: str) -> List[Dict[str, Any]]:
        """List available archive dates with message counts."""
        archive_dir = ARCHIVE_DIR / session_id
        if not archive_dir.exists():
            return []
        result = []
        for f in sorted(archive_dir.glob("*.jsonl")):
            count = sum(1 for line in f.read_text(encoding="utf-8").strip().split("\n") if line)
            result.append({
                "date": f.stem,
                "messageCount": count,
                "sizeBytes": f.stat().st_size,
            })
        return result
