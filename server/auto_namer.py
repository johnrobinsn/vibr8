"""Auto-naming module.

Spawns a one-shot CLI process (Claude Code or Codex) to generate a short
3-5 word session title from the user's first message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from typing import Literal, Optional

logger = logging.getLogger(__name__)

# Module-level cached binary paths
_resolved_claude_binary: Optional[str] = None
_resolved_codex_binary: Optional[str] = None

BackendType = Literal["claude", "codex"]


def _resolve_claude_binary() -> str:
    global _resolved_claude_binary
    if _resolved_claude_binary is not None:
        return _resolved_claude_binary
    path = shutil.which("claude")
    _resolved_claude_binary = path if path else "claude"
    return _resolved_claude_binary


def _resolve_codex_binary() -> str:
    global _resolved_codex_binary
    if _resolved_codex_binary is not None:
        return _resolved_codex_binary
    path = shutil.which("codex")
    _resolved_codex_binary = path if path else "codex"
    return _resolved_codex_binary


def _strip_surrounding_quotes(text: str) -> str:
    """Remove leading/trailing single or double quotes from a string."""
    return re.sub(r'^["\']|["\']$', "", text).strip()


@dataclass
class AutoNamerOptions:
    claude_binary: Optional[str] = None
    codex_binary: Optional[str] = None
    backend_type: BackendType = "claude"
    timeout_seconds: float = 15.0


async def generate_session_title(
    first_user_message: str,
    model: str,
    options: Optional[AutoNamerOptions] = None,
) -> Optional[str]:
    """Spawn a one-shot CLI process to generate a short session title.

    Supports both Claude Code and Codex backends.

    Args:
        first_user_message: The first message sent by the user.
        model: The model identifier to pass to the CLI.
        options: Optional configuration for binary paths, backend type, and timeout.

    Returns:
        The generated title string, or ``None`` if generation fails.
    """
    opts = options or AutoNamerOptions()
    backend_type = opts.backend_type
    timeout = opts.timeout_seconds

    # Truncate message to keep the prompt small
    truncated = first_user_message[:500]
    prompt = (
        "Generate a concise 3-5 word session title for this user request. "
        "Output ONLY the title, nothing else.\n\n"
        f"Request: {truncated}"
    )

    if backend_type == "codex":
        return await _generate_title_via_codex(
            prompt, model, timeout, opts.codex_binary
        )
    return await _generate_title_via_claude(
        prompt, model, timeout, opts.claude_binary
    )


async def _generate_title_via_claude(
    prompt: str,
    model: str,
    timeout: float,
    binary_override: Optional[str] = None,
) -> Optional[str]:
    binary = binary_override or _resolve_claude_binary()

    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "-p",
            prompt,
            "--model",
            model,
            "--output-format",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ,
        )

        try:
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
            logger.warning("[auto-namer] Auto-naming timed out (Claude)")
            return None

        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""

        # Try to parse as JSON first
        try:
            parsed = json.loads(stdout)
            title = (parsed.get("result") or "").strip()
            if title and len(title) < 100:
                return _strip_surrounding_quotes(title)
        except (json.JSONDecodeError, ValueError):
            # Fall back to raw stdout
            raw = stdout.strip()
            if raw and len(raw) < 100:
                return _strip_surrounding_quotes(raw)

        return None

    except Exception as err:
        logger.warning(
            "[auto-namer] Failed to generate session title via Claude: %s", err
        )
        return None


async def _generate_title_via_codex(
    prompt: str,
    model: str,
    timeout: float,
    binary_override: Optional[str] = None,
) -> Optional[str]:
    binary = binary_override or _resolve_codex_binary()

    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "exec",
            "-q",
            prompt,
            "--model",
            model,
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ,
        )

        try:
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
            logger.warning("[auto-namer] Auto-naming timed out (Codex)")
            return None

        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""

        # Codex exec --json outputs JSONL events.
        # Find the last item.completed with agentMessage.
        lines = stdout.strip().split("\n")

        for line in reversed(lines):
            try:
                event = json.loads(line)
                if (
                    event.get("type") == "item.completed"
                    and event.get("item", {}).get("type") == "agent_message"
                ):
                    text = event["item"].get("text", "")
                    if text and len(text) < 100:
                        return _strip_surrounding_quotes(text)
            except (json.JSONDecodeError, ValueError):
                continue

        # Fallback: try to extract text from any completed item
        for line in reversed(lines):
            try:
                event = json.loads(line)
                item_text = event.get("item", {}).get("text", "")
                if item_text:
                    text = item_text.strip()
                    if text and len(text) < 100:
                        return _strip_surrounding_quotes(text)
            except (json.JSONDecodeError, ValueError):
                continue

        return None

    except Exception as err:
        logger.warning(
            "[auto-namer] Failed to generate session title via Codex: %s", err
        )
        return None
