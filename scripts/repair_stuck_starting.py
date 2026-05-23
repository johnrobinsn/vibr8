#!/usr/bin/env python3
"""Repair launcher.json entries stuck in state="starting" with dead PIDs.

Run while the vibr8 server is stopped. Backs up the original file before
writing.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main() -> int:
    path = Path.home() / ".vibr8" / "sessions" / "launcher.json"
    if not path.exists():
        print(f"launcher.json not found at {path}", file=sys.stderr)
        return 1

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        print("launcher.json is not a list — refusing to edit", file=sys.stderr)
        return 1

    repaired = []
    for entry in data:
        sid = entry.get("sessionId", "?")
        state = entry.get("state")
        pid = entry.get("pid")
        if state == "starting":
            alive = pid_alive(pid) if pid else False
            if not alive:
                entry["state"] = "exited"
                entry["exitCode"] = entry.get("exitCode") or -1
                repaired.append((sid, pid))

    if not repaired:
        print("Nothing to repair.")
        return 0

    backup = path.with_suffix(f".bak.{int(time.time())}")
    shutil.copy2(path, backup)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Backed up to {backup}")
    print(f"Repaired {len(repaired)} session(s):")
    for sid, pid in repaired:
        print(f"  {sid}  pid={pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
