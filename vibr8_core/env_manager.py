"""Manage environment profiles stored as JSON files in ~/.vibr8/envs/."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path


# ─── Types ──────────────────────────────────────────────────────────────────


@dataclass
class Vibr8Env:
    name: str
    slug: str
    variables: dict[str, str] = field(default_factory=dict)
    createdAt: int = 0  # noqa: N815 – matches JSON on disk
    updatedAt: int = 0  # noqa: N815

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Vibr8Env:
        return cls(
            name=data["name"],
            slug=data["slug"],
            variables=data.get("variables", {}),
            createdAt=data.get("createdAt", 0),
            updatedAt=data.get("updatedAt", 0),
        )


# ─── Paths ──────────────────────────────────────────────────────────────────

VIBR8_DIR = Path.home() / ".vibr8"
ENVS_DIR = VIBR8_DIR / "envs"


def _ensure_dir() -> None:
    ENVS_DIR.mkdir(parents=True, exist_ok=True)


def _file_path(slug: str) -> Path:
    return ENVS_DIR / f"{slug}.json"


# ─── Helpers ────────────────────────────────────────────────────────────────


def slugify(name: str) -> str:
    s = name.lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s)
    s = re.sub(r"^-|-$", "", s)
    return s


def _now_ms() -> int:
    """Return the current time as milliseconds since epoch (matching JS Date.now())."""
    return int(time.time() * 1000)


# ─── CRUD ───────────────────────────────────────────────────────────────────


def list_envs() -> list[Vibr8Env]:
    _ensure_dir()
    try:
        files = [f for f in ENVS_DIR.iterdir() if f.suffix == ".json"]
    except OSError:
        return []

    envs: list[Vibr8Env] = []
    for file in files:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            envs.append(Vibr8Env.from_dict(data))
        except Exception:
            # Skip corrupt files
            pass

    envs.sort(key=lambda e: e.name)
    return envs


def get_env(slug: str) -> Vibr8Env | None:
    _ensure_dir()
    try:
        data = json.loads(_file_path(slug).read_text(encoding="utf-8"))
        return Vibr8Env.from_dict(data)
    except Exception:
        return None


def create_env(
    name: str,
    variables: dict[str, str] | None = None,
) -> Vibr8Env:
    if not name or not name.strip():
        raise ValueError("Environment name is required")

    slug = slugify(name.strip())
    if not slug:
        raise ValueError("Environment name must contain alphanumeric characters")

    _ensure_dir()
    if _file_path(slug).exists():
        raise ValueError(
            f'An environment with a similar name already exists ("{slug}")'
        )

    now = _now_ms()
    env = Vibr8Env(
        name=name.strip(),
        slug=slug,
        variables=variables or {},
        createdAt=now,
        updatedAt=now,
    )
    _file_path(slug).write_text(
        json.dumps(env.to_dict(), indent=2), encoding="utf-8"
    )
    return env


def update_env(
    slug: str,
    *,
    name: str | None = None,
    variables: dict[str, str] | None = None,
) -> Vibr8Env | None:
    _ensure_dir()
    existing = get_env(slug)
    if existing is None:
        return None

    new_name = name.strip() if name and name.strip() else existing.name
    new_slug = slugify(new_name)
    if not new_slug:
        raise ValueError("Environment name must contain alphanumeric characters")

    # If name changed, check for slug collision with a different env
    if new_slug != slug and _file_path(new_slug).exists():
        raise ValueError(
            f'An environment with a similar name already exists ("{new_slug}")'
        )

    env = Vibr8Env(
        name=new_name,
        slug=new_slug,
        variables=variables if variables is not None else existing.variables,
        createdAt=existing.createdAt,
        updatedAt=_now_ms(),
    )

    # If slug changed, delete old file
    if new_slug != slug:
        try:
            _file_path(slug).unlink()
        except OSError:
            pass

    _file_path(new_slug).write_text(
        json.dumps(env.to_dict(), indent=2), encoding="utf-8"
    )
    return env


def delete_env(slug: str) -> bool:
    _ensure_dir()
    path = _file_path(slug)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False
