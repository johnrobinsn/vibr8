"""Node data-dir resolution for vibr8_core modules.

vibr8_core modules run inside a vibr8_node process — either the hub's
self-node (self_mode) or a remote node — and persist per-node state
(sessions, ring0 config, artifacts, env bundles, worktrees, scheduled
tasks) under a single root directory.

``NODE_DATA_DIR`` is read from the ``VIBR8_NODE_DATA_DIR`` env var and
resolved at module import time. ``vibr8_node/__main__.py`` sets the env
var **before** importing ``vibr8_node.node_agent`` (which transitively
imports vibr8_core) so the constants below get the right value on first
evaluation. Falls back to ``~/.vibr8`` for legacy compat when the var is
unset.
"""

from __future__ import annotations

import os
from pathlib import Path


def _resolve_node_data_dir() -> Path:
    override = os.environ.get("VIBR8_NODE_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".vibr8"


NODE_DATA_DIR: Path = _resolve_node_data_dir()
