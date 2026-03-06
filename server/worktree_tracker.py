"""Track worktree-to-session mappings, persisted to ~/.vibr8/worktrees.json."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


# -- Types -------------------------------------------------------------------

@dataclass
class WorktreeMapping:
    sessionId: str
    repoRoot: str
    branch: str
    worktreePath: str
    createdAt: float
    # Actual git branch in the worktree (may differ from `branch` for -wt-N branches)
    actualBranch: Optional[str] = None


# -- Paths -------------------------------------------------------------------

TRACKER_PATH = Path.home() / ".vibr8" / "worktrees.json"


# -- Tracker -----------------------------------------------------------------

class WorktreeTracker:
    def __init__(self) -> None:
        self._mappings: list[WorktreeMapping] = []
        self.load()

    # -- persistence ---------------------------------------------------------

    def load(self) -> list[WorktreeMapping]:
        try:
            if TRACKER_PATH.exists():
                raw = TRACKER_PATH.read_text(encoding="utf-8")
                data = json.loads(raw)
                self._mappings = [WorktreeMapping(**item) for item in data]
        except Exception:
            self._mappings = []
        return list(self._mappings)

    def _save(self) -> None:
        TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            [asdict(m) for m in self._mappings],
            indent=2,
        )
        TRACKER_PATH.write_text(payload, encoding="utf-8")

    # -- public api ----------------------------------------------------------

    def add_mapping(self, mapping: WorktreeMapping) -> None:
        """Add a mapping, replacing any existing one for the same session."""
        self._mappings = [
            m for m in self._mappings if m.sessionId != mapping.sessionId
        ]
        self._mappings.append(mapping)
        self._save()

    def remove_by_session(self, session_id: str) -> Optional[WorktreeMapping]:
        """Remove and return the mapping for *session_id*, or ``None``."""
        for idx, m in enumerate(self._mappings):
            if m.sessionId == session_id:
                removed = self._mappings.pop(idx)
                self._save()
                return removed
        return None

    def get_by_session(self, session_id: str) -> Optional[WorktreeMapping]:
        """Return the mapping for *session_id*, or ``None``."""
        for m in self._mappings:
            if m.sessionId == session_id:
                return m
        return None

    def get_sessions_for_worktree(self, worktree_path: str) -> list[WorktreeMapping]:
        """Return all mappings whose ``worktreePath`` matches."""
        return [m for m in self._mappings if m.worktreePath == worktree_path]

    def get_sessions_for_repo(self, repo_root: str) -> list[WorktreeMapping]:
        """Return all mappings whose ``repoRoot`` matches."""
        return [m for m in self._mappings if m.repoRoot == repo_root]

    def is_worktree_in_use(
        self,
        worktree_path: str,
        exclude_session_id: Optional[str] = None,
    ) -> bool:
        """Check whether any session (other than *exclude_session_id*) uses the worktree."""
        return any(
            m.worktreePath == worktree_path and m.sessionId != exclude_session_id
            for m in self._mappings
        )
