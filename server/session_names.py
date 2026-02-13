"""
Session name storage.

Stores session names in a JSON file at ~/.companion/session-names.json.
Lazy-loads from disk on first access and persists on every write.
"""

import json
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────

_DEFAULT_PATH = Path.home() / ".companion" / "session-names.json"

# ── Store ────────────────────────────────────────────────────────────────────

_names: dict[str, str] = {}
_loaded = False
_file_path = _DEFAULT_PATH


def _ensure_loaded() -> None:
    global _names, _loaded
    if _loaded:
        return
    try:
        if _file_path.exists():
            raw = _file_path.read_text(encoding="utf-8")
            _names = json.loads(raw)
    except Exception:
        _names = {}
    _loaded = True


def _persist() -> None:
    _file_path.parent.mkdir(parents=True, exist_ok=True)
    _file_path.write_text(json.dumps(_names, indent=2), encoding="utf-8")


# ── Public API ───────────────────────────────────────────────────────────────


def get_name(session_id: str) -> str | None:
    _ensure_loaded()
    return _names.get(session_id)


def set_name(session_id: str, name: str) -> None:
    _ensure_loaded()
    _names[session_id] = name
    _persist()


def get_all_names() -> dict[str, str]:
    _ensure_loaded()
    return dict(_names)


def remove_name(session_id: str) -> None:
    _ensure_loaded()
    _names.pop(session_id, None)
    _persist()


def _reset_for_test(custom_path: str | None = None) -> None:
    """Reset internal state and optionally set a custom file path (for testing)."""
    global _names, _loaded, _file_path
    _names = {}
    _loaded = False
    _file_path = Path(custom_path) if custom_path else _DEFAULT_PATH
