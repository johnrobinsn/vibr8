"""Per-node backend model discovery.

Reads Codex / OpenCode / Hermes model lists from the local node's home
dir and PATH. Imported by both `server/routes.py` (legacy hub-default
endpoint) and `vibr8_core.node_operations` (per-node tunnel command),
so remote nodes get the same logic against *their* home/PATH.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


_OPENCODE_CACHE_TTL = 300  # 5 minutes
_opencode_models_cache: list[dict[str, str]] | None = None
_opencode_models_cache_time: float = 0

_OPENCODE_PROVIDER_ORDER = {"opencode": 0, "openai": 1, "openrouter": 2}

_OPENCODE_FALLBACK = [
    {"value": "google/gemini-2.5-pro", "label": "Gemini 2.5 Pro", "description": "", "provider": "google"},
    {"value": "google/gemini-2.5-flash", "label": "Gemini 2.5 Flash", "description": "", "provider": "google"},
    {"value": "anthropic/claude-sonnet-4-20250514", "label": "Claude Sonnet 4", "description": "", "provider": "anthropic"},
    {"value": "anthropic/claude-opus-4-20250514", "label": "Claude Opus 4", "description": "", "provider": "anthropic"},
    {"value": "openai/gpt-4o", "label": "GPT-4o", "description": "", "provider": "openai"},
    {"value": "openai/o3", "label": "o3", "description": "", "provider": "openai"},
]

_HERMES_FALLBACK = [
    {"value": "gpt-5.5", "label": "GPT-5.5", "description": "", "provider": "openai-codex"},
    {"value": "claude-opus-4-20250514", "label": "Claude Opus 4", "description": "", "provider": "anthropic"},
    {"value": "claude-sonnet-4-20250514", "label": "Claude Sonnet 4", "description": "", "provider": "anthropic"},
    {"value": "gpt-4o", "label": "GPT-4o", "description": "", "provider": "openai"},
    {"value": "deepseek-r1", "label": "DeepSeek R1", "description": "", "provider": "deepseek"},
]


def _model_id_to_label(model_id: str) -> str:
    return model_id.replace("-", " ").replace(".", ".").title()


def _parse_opencode_models(output: str) -> list[dict[str, str]]:
    models: list[dict[str, str]] = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line or "/" not in line:
            continue
        provider, _, model_part = line.partition("/")
        # openrouter models have a second slash: openrouter/deepseek/deepseek-r1
        label = _model_id_to_label(model_part.split("/")[-1])
        models.append({
            "value": line,
            "label": label,
            "description": "",
            "provider": provider,
        })
    models.sort(key=lambda m: (
        _OPENCODE_PROVIDER_ORDER.get(m["provider"], 99),
        m["provider"],
        m["label"].lower(),
    ))
    return models


async def get_opencode_models() -> list[dict[str, str]]:
    global _opencode_models_cache, _opencode_models_cache_time
    now = time.monotonic()
    if _opencode_models_cache and now - _opencode_models_cache_time < _OPENCODE_CACHE_TTL:
        return _opencode_models_cache
    try:
        proc = await asyncio.create_subprocess_exec(
            "opencode", "models",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        models = _parse_opencode_models(stdout.decode())
        if models:
            _opencode_models_cache = models
            _opencode_models_cache_time = now
            return models
    except Exception:
        logger.warning("[backend-models] Failed to fetch opencode models, using fallback")
    return _opencode_models_cache or _OPENCODE_FALLBACK


def get_hermes_models() -> list[dict[str, str]]:
    """Read available models from Hermes config, falling back to a static list."""
    try:
        import yaml
        config_path = Path.home() / ".hermes" / "config.yaml"
        if config_path.exists():
            config = yaml.safe_load(config_path.read_text())
            model_config = config.get("model", {})
            default_model = model_config.get("default", "")
            default_provider = model_config.get("provider", "")
            if default_model:
                models = [{"value": default_model, "label": _model_id_to_label(default_model),
                           "description": f"Default ({default_provider})" if default_provider else "Default",
                           "provider": default_provider}]
                for entry in _HERMES_FALLBACK:
                    if entry["value"] != default_model:
                        models.append(entry)
                return models
    except Exception:
        logger.warning("[backend-models] Failed to read hermes config, using fallback")
    return list(_HERMES_FALLBACK)


def get_codex_models() -> dict:
    """Read available Codex models from ~/.codex/models_cache.json."""
    cache_path = Path.home() / ".codex" / "models_cache.json"
    if not cache_path.exists():
        return {"error": "Codex models cache not found"}
    try:
        cache = json.loads(cache_path.read_text())
        models = sorted(
            [m for m in cache.get("models", []) if m.get("visibility") == "list"],
            key=lambda m: m.get("priority", 99),
        )
        return {"models": [
            {"value": m["slug"], "label": m.get("display_name", m["slug"]), "description": m.get("description", "")}
            for m in models
        ]}
    except Exception as e:
        return {"error": f"Failed to parse Codex models cache: {e}"}
