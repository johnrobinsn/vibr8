"""Hub data-dir resolution.

The hub stores auth, the node registry, ICE-server config, etc. under a
single root directory — defaults to ``~/.vibr8`` but can be redirected
via the ``VIBR8_HUB_DATA_DIR`` env var so a dev hub can run alongside a
production hub without colliding on disk.

This is hub-scoped on purpose: node-scoped data (sessions, ring0, env
bundles) is already routed via ``VIBR8_SELF_NODE_DATA_DIR`` and the
per-node constructor injection in ``vibr8_node/node_agent.py``.
"""

from __future__ import annotations

import os
from pathlib import Path


def _resolve_hub_dir() -> Path:
    override = os.environ.get("VIBR8_HUB_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".vibr8"


VIBR8_DIR: Path = _resolve_hub_dir()
