"""Artifact storage — persistent curated content items shared by Ring0/sessions."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

_ARTIFACTS_PATH = Path.home() / ".vibr8" / "artifacts.json"


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


def list_artifacts(session_id: str | None = None) -> list[dict]:
    artifacts = _load()
    if session_id:
        artifacts = [a for a in artifacts if a.get("sourceSessionId") == session_id]
    artifacts.sort(key=lambda a: a.get("createdAt", 0), reverse=True)
    return artifacts


def create_artifact(username: str, data: dict) -> dict:
    artifacts = _load()
    artifact = {
        "id": str(uuid.uuid4()),
        "title": data.get("title", "Untitled"),
        "type": data.get("type", "markdown"),
        "content": data.get("content", ""),
        "sourceSessionId": data.get("sourceSessionId"),
        "sourceSessionName": data.get("sourceSessionName"),
        "createdAt": time.time(),
        "filename": data.get("filename"),
        "username": username,
    }
    artifacts.append(artifact)
    _save(artifacts)
    return artifact


def delete_artifact(artifact_id: str) -> bool:
    artifacts = _load()
    before = len(artifacts)
    artifacts = [a for a in artifacts if a["id"] != artifact_id]
    if len(artifacts) == before:
        return False
    _save(artifacts)
    return True
