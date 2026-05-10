"""Vendored third-party code for vibr8.

Subpackages here are pinned snapshots of upstream projects, trimmed to the
files required at inference time. They are added to ``sys.path`` on demand
by the modules that consume them (see ``server.wespeaker_model`` and
``server.tse_processor``).

Sources:
- ``wespeaker/`` — subset of https://github.com/wenet-e2e/wespeaker (Apache 2.0)
- ``wesep/``    — subset of https://github.com/wenet-e2e/wesep (Apache 2.0)
"""

from __future__ import annotations

import sys
from pathlib import Path

_VENDOR_ROOT = Path(__file__).resolve().parent


def ensure_on_path() -> None:
    """Make ``wespeaker`` and ``wesep`` importable as top-level packages."""
    p = str(_VENDOR_ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)
