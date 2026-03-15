"""Persistent session storage backed by JSON files on disk.

Originally ported from The Vibe Companion (session-store.ts).
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from dataclasses import dataclass
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
        )


# ─── Store ──────────────────────────────────────────────────────────────────

DEFAULT_DIR = Path.home() / ".vibr8" / "sessions"


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
