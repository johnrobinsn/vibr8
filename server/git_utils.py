"""Git utilities for worktree management and repository inspection.

Ported from companion/web/server/git-utils.ts — maintains identical logic and behavior.
"""

from __future__ import annotations

import random
import subprocess
import time
from dataclasses import dataclass
from os.path import basename
from pathlib import Path
from typing import Optional


# ─── Types ──────────────────────────────────────────────────────────────────


@dataclass
class GitRepoInfo:
    repo_root: str
    repo_name: str
    current_branch: str
    default_branch: str
    is_worktree: bool


@dataclass
class GitBranchInfo:
    name: str
    is_current: bool
    is_remote: bool
    worktree_path: Optional[str]
    ahead: int
    behind: int


@dataclass
class GitWorktreeInfo:
    path: str
    branch: str
    head: str
    is_main_worktree: bool
    is_dirty: bool


@dataclass
class WorktreeCreateResult:
    worktree_path: str
    branch: str
    """The conceptual branch the user selected."""
    actual_branch: str
    """The actual git branch in the worktree (may be e.g. main-wt-2 for duplicate sessions)."""
    is_new: bool


# ─── Paths ──────────────────────────────────────────────────────────────────

WORKTREES_BASE = Path.home() / ".companion" / "worktrees"


def sanitize_branch(branch: str) -> str:
    return branch.replace("/", "--")


def worktree_dir(repo_name: str, branch: str) -> str:
    return str(WORKTREES_BASE / repo_name / sanitize_branch(branch))


# ─── Helpers ────────────────────────────────────────────────────────────────


def git(cmd: str, cwd: str) -> str:
    """Run a git command and return stripped stdout. Raises on failure."""
    result = subprocess.run(
        f"git {cmd}",
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, f"git {cmd}", result.stdout, result.stderr
        )
    return result.stdout.strip()


def git_safe(cmd: str, cwd: str) -> Optional[str]:
    """Run a git command, returning None on any failure."""
    try:
        return git(cmd, cwd)
    except Exception:
        return None


# ─── Functions ──────────────────────────────────────────────────────────────


def _resolve_default_branch(repo_root: str) -> str:
    # Try origin HEAD
    origin_ref = git_safe("symbolic-ref refs/remotes/origin/HEAD", repo_root)
    if origin_ref:
        return origin_ref.replace("refs/remotes/origin/", "")
    # Fallback: check if main or master exists
    branches = git_safe("branch --list main master", repo_root) or ""
    if "main" in branches:
        return "main"
    if "master" in branches:
        return "master"
    # Last resort
    return "main"


def get_repo_info(cwd: str) -> Optional[GitRepoInfo]:
    repo_root = git_safe("rev-parse --show-toplevel", cwd)
    if not repo_root:
        return None

    current_branch = git_safe("rev-parse --abbrev-ref HEAD", cwd) or "HEAD"
    git_dir = git_safe("rev-parse --git-dir", cwd) or ""
    # A linked worktree's .git dir is inside the main repo's .git/worktrees/
    is_worktree = "/worktrees/" in git_dir

    default_branch = _resolve_default_branch(repo_root)

    return GitRepoInfo(
        repo_root=repo_root,
        repo_name=basename(repo_root),
        current_branch=current_branch,
        default_branch=default_branch,
        is_worktree=is_worktree,
    )


def list_branches(repo_root: str) -> list[GitBranchInfo]:
    # Get worktree mappings first
    worktrees = list_worktrees(repo_root)
    worktree_by_branch: dict[str, str] = {}
    for wt in worktrees:
        if wt.branch:
            worktree_by_branch[wt.branch] = wt.path

    result: list[GitBranchInfo] = []

    # Local branches
    local_raw = git_safe(
        "for-each-ref '--format=%(refname:short)%09%(HEAD)' refs/heads/",
        repo_root,
    )
    if local_raw:
        for line in local_raw.split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            name = parts[0]
            head = parts[1].strip() if len(parts) > 1 else ""
            is_current = head == "*"
            status = get_branch_status(repo_root, name)
            result.append(
                GitBranchInfo(
                    name=name,
                    is_current=is_current,
                    is_remote=False,
                    worktree_path=worktree_by_branch.get(name),
                    ahead=status["ahead"],
                    behind=status["behind"],
                )
            )

    # Remote branches (only those without a local counterpart)
    local_names = {b.name for b in result}
    remote_raw = git_safe(
        "for-each-ref '--format=%(refname:short)' refs/remotes/origin/",
        repo_root,
    )
    if remote_raw:
        for line in remote_raw.split("\n"):
            full = line.strip()
            if not full or full == "origin/HEAD":
                continue
            name = full.replace("origin/", "", 1)
            if name in local_names:
                continue
            result.append(
                GitBranchInfo(
                    name=name,
                    is_current=False,
                    is_remote=True,
                    worktree_path=None,
                    ahead=0,
                    behind=0,
                )
            )

    return result


def list_worktrees(repo_root: str) -> list[GitWorktreeInfo]:
    raw = git_safe("worktree list --porcelain", repo_root)
    if not raw:
        return []

    worktrees: list[GitWorktreeInfo] = []
    current: dict = {}

    for line in raw.split("\n"):
        if line.startswith("worktree "):
            if current.get("path"):
                worktrees.append(
                    GitWorktreeInfo(
                        path=current["path"],
                        branch=current.get("branch", ""),
                        head=current.get("head", ""),
                        is_main_worktree=current.get("is_main_worktree", False),
                        is_dirty=current.get("is_dirty", False),
                    )
                )
            current = {"path": line[9:], "is_dirty": False, "is_main_worktree": False}
        elif line.startswith("HEAD "):
            current["head"] = line[5:]
        elif line.startswith("branch "):
            current["branch"] = line[7:].replace("refs/heads/", "")
        elif line == "bare":
            current["is_main_worktree"] = True
        elif line == "":
            # End of entry — check if main worktree (first one is always main)
            if len(worktrees) == 0 and current.get("path"):
                current["is_main_worktree"] = True

    # Push last entry
    if current.get("path"):
        if len(worktrees) == 0:
            current["is_main_worktree"] = True
        worktrees.append(
            GitWorktreeInfo(
                path=current["path"],
                branch=current.get("branch", ""),
                head=current.get("head", ""),
                is_main_worktree=current.get("is_main_worktree", False),
                is_dirty=current.get("is_dirty", False),
            )
        )

    # Check dirty status for each worktree
    for wt in worktrees:
        wt.is_dirty = is_worktree_dirty(wt.path)

    return worktrees


def ensure_worktree(
    repo_root: str,
    branch_name: str,
    *,
    base_branch: Optional[str] = None,
    create_branch: bool = True,
    force_new: bool = False,
) -> WorktreeCreateResult:
    repo_name = basename(repo_root)

    # Check if a worktree already exists for this branch
    existing = list_worktrees(repo_root)
    found = next((wt for wt in existing if wt.branch == branch_name), None)

    if found and not force_new:
        # Don't reuse the main worktree — it's the original repo checkout
        if not found.is_main_worktree:
            return WorktreeCreateResult(
                worktree_path=found.path,
                branch=branch_name,
                actual_branch=branch_name,
                is_new=False,
            )

    # Find a unique path: append random 4-digit suffix if the base path is taken
    base_path = worktree_dir(repo_name, branch_name)
    target_path = base_path
    for _attempt in range(100):
        if not Path(target_path).exists():
            break
        suffix = random.randint(1000, 9999)
        target_path = f"{base_path}-{suffix}"
    if Path(target_path).exists():
        target_path = f"{base_path}-{int(time.time() * 1000)}"

    # Ensure parent directory exists
    (WORKTREES_BASE / repo_name).mkdir(parents=True, exist_ok=True)

    # A worktree already exists for this branch — create a new uniquely-named
    # branch so multiple sessions can work on the same branch independently.
    if found:
        commit_hash = git("rev-parse HEAD", found.path)
        unique_branch = generate_unique_worktree_branch(repo_root, branch_name)
        git(f'worktree add -b {unique_branch} "{target_path}" {commit_hash}', repo_root)
        return WorktreeCreateResult(
            worktree_path=target_path,
            branch=branch_name,
            actual_branch=unique_branch,
            is_new=False,
        )

    # Check if branch already exists locally or on remote
    branch_exists = git_safe(f"rev-parse --verify refs/heads/{branch_name}", repo_root) is not None
    remote_branch_exists = (
        git_safe(f"rev-parse --verify refs/remotes/origin/{branch_name}", repo_root) is not None
    )

    if branch_exists:
        # Worktree add with existing local branch
        git(f'worktree add "{target_path}" {branch_name}', repo_root)
        return WorktreeCreateResult(
            worktree_path=target_path,
            branch=branch_name,
            actual_branch=branch_name,
            is_new=False,
        )

    if remote_branch_exists:
        # Create local tracking branch from remote
        git(f'worktree add -b {branch_name} "{target_path}" origin/{branch_name}', repo_root)
        return WorktreeCreateResult(
            worktree_path=target_path,
            branch=branch_name,
            actual_branch=branch_name,
            is_new=False,
        )

    if create_branch:
        # Create new branch from base
        base = base_branch or _resolve_default_branch(repo_root)
        git(f'worktree add -b {branch_name} "{target_path}" {base}', repo_root)
        return WorktreeCreateResult(
            worktree_path=target_path,
            branch=branch_name,
            actual_branch=branch_name,
            is_new=True,
        )

    raise RuntimeError(f'Branch "{branch_name}" does not exist and create_branch is False')


def generate_unique_worktree_branch(repo_root: str, base_branch: str) -> str:
    """Generate a unique branch name for a companion-managed worktree.

    Pattern: {branch}-wt-{random4digit} (e.g. main-wt-8374).
    Uses random suffixes to avoid collisions with leftover branches.
    """
    for _attempt in range(100):
        suffix = random.randint(1000, 9999)
        candidate = f"{base_branch}-wt-{suffix}"
        if git_safe(f"rev-parse --verify refs/heads/{candidate}", repo_root) is None:
            return candidate
    # Fallback: use timestamp if all random attempts collide (extremely unlikely)
    return f"{base_branch}-wt-{int(time.time() * 1000)}"


def remove_worktree(
    repo_root: str,
    worktree_path: str,
    *,
    force: bool = False,
    branch_to_delete: Optional[str] = None,
) -> dict:
    """Remove a worktree. Returns {"removed": bool, "reason"?: str}."""
    if not Path(worktree_path).exists():
        # Already gone, clean up git's reference
        git_safe("worktree prune", repo_root)
        if branch_to_delete:
            git_safe(f"branch -D {branch_to_delete}", repo_root)
        return {"removed": True}

    if not force and is_worktree_dirty(worktree_path):
        return {
            "removed": False,
            "reason": "Worktree has uncommitted changes. Use force to remove anyway.",
        }

    try:
        force_flag = " --force" if force else ""
        git(f'worktree remove "{worktree_path}"{force_flag}', repo_root)
        # Clean up the companion-managed branch after worktree removal
        if branch_to_delete:
            git_safe(f"branch -D {branch_to_delete}", repo_root)
        return {"removed": True}
    except Exception as e:
        return {"removed": False, "reason": str(e)}


def is_worktree_dirty(worktree_path: str) -> bool:
    if not Path(worktree_path).exists():
        return False
    status = git_safe("status --porcelain", worktree_path)
    return status is not None and len(status) > 0


def git_fetch(cwd: str) -> dict:
    """Fetch from remote. Returns {"success": bool, "output": str}."""
    try:
        output = git("fetch --prune", cwd)
        return {"success": True, "output": output}
    except Exception as e:
        return {"success": False, "output": str(e)}


def git_pull(cwd: str) -> dict:
    """Pull from remote. Returns {"success": bool, "output": str}."""
    try:
        output = git("pull", cwd)
        return {"success": True, "output": output}
    except Exception as e:
        return {"success": False, "output": str(e)}


def checkout_branch(cwd: str, branch_name: str) -> None:
    git(f"checkout {branch_name}", cwd)


def get_branch_status(repo_root: str, branch_name: str) -> dict:
    """Get ahead/behind counts for a branch. Returns {"ahead": int, "behind": int}."""
    raw = git_safe(
        f"rev-list --left-right --count origin/{branch_name}...{branch_name}",
        repo_root,
    )
    if not raw:
        return {"ahead": 0, "behind": 0}
    parts = raw.split()
    behind = int(parts[0]) if len(parts) > 0 else 0
    ahead = int(parts[1]) if len(parts) > 1 else 0
    return {"ahead": ahead, "behind": behind}
