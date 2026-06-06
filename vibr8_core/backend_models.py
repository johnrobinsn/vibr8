"""Per-node backend model discovery.

Reads Codex / OpenCode / Hermes model lists from the local node's home
dir and PATH. Imported by both `server/routes.py` (legacy hub-default
endpoint) and `vibr8_core.node_operations` (per-node tunnel command),
so remote nodes get the same logic against *their* home/PATH.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_OPENCODE_CACHE_TTL = 300  # 5 minutes
_opencode_models_cache: list[dict[str, str]] | None = None
_opencode_models_cache_time: float = 0
_MODEL_INFO_CACHE_TTL = 30
_model_info_cache: dict[tuple[str, str, str, str, str], tuple[float, dict[str, Any]]] = {}

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
    {"value": "gpt-5.5", "label": "GPT-5.5", "description": "", "provider": "openai"},
    {"value": "claude-opus-4-20250514", "label": "Claude Opus 4", "description": "", "provider": "anthropic"},
    {"value": "claude-sonnet-4-20250514", "label": "Claude Sonnet 4", "description": "", "provider": "anthropic"},
    {"value": "gpt-4o", "label": "GPT-4o", "description": "", "provider": "openai"},
    {"value": "deepseek-r1", "label": "DeepSeek R1", "description": "", "provider": "deepseek"},
]

_BACKEND_DEFAULTS: dict[str, dict[str, str]] = {
    "claude": {
        "model": "claude-opus-4-6",
        "provider": "anthropic",
        "displayName": "Opus",
    },
    "codex": {
        "model": "gpt-5.3-codex",
        "provider": "openai",
        "displayName": "GPT-5.3 Codex",
    },
    "opencode": {
        "model": "google/gemini-2.5-pro",
        "provider": "google",
        "displayName": "Gemini 2.5 Pro",
    },
    "hermes": {
        "model": "gpt-5.5",
        "provider": "openai",
        "displayName": "GPT-5.5",
    },
    "computer-use": {
        "model": "ByteDance-Seed/UI-TARS-1.5-7B",
        "provider": "ByteDance-Seed",
        "displayName": "UI-TARS 1.5 7B",
    },
}


def _model_id_to_label(model_id: str) -> str:
    return model_id.replace("-", " ").replace(".", ".").title()


def _read_jsonc(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            import json5
            data = json5.loads(path.read_text())
            if isinstance(data, dict):
                return data
    except Exception:
        logger.warning("[backend-models] Failed to read JSON config %s", path)
    return {}


def _infer_provider(model: str, fallback: str = "") -> str:
    if "/" in model:
        return model.split("/", 1)[0]
    lowered = model.lower()
    if lowered.startswith("claude-"):
        return "anthropic"
    if lowered.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if lowered.startswith("gemini-"):
        return "google"
    if lowered.startswith("grok-"):
        return "xai"
    if lowered.startswith("deepseek-"):
        return "deepseek"
    return fallback


def _base_model_info(backend: str) -> dict[str, Any]:
    default = _BACKEND_DEFAULTS.get(backend, {})
    return {
        "backend": backend,
        "provider": default.get("provider", ""),
        "model": default.get("model", ""),
        "displayName": default.get("displayName", ""),
        "source": "fallback",
        "isExplicit": False,
        "modes": {},
    }


def _apply_model(
    info: dict[str, Any],
    model: str,
    *,
    provider: str = "",
    display_name: str = "",
    source: str,
    explicit: bool = False,
) -> None:
    model = (model or "").strip()
    if not model:
        return
    info["model"] = model
    info["provider"] = provider or _infer_provider(model, info.get("provider", ""))
    info["displayName"] = display_name or _model_id_to_label(model.split("/")[-1])
    info["source"] = source
    info["isExplicit"] = explicit


def _first_codex_cache_model() -> dict[str, str]:
    result = get_codex_models()
    models = result.get("models") if isinstance(result, dict) else None
    if isinstance(models, list) and models:
        first = models[0]
        if isinstance(first, dict):
            return {
                "model": str(first.get("value", "")),
                "displayName": str(first.get("label", "")),
            }
    return {}


def _codex_config_model_info(explicit_model: str = "") -> dict[str, Any]:
    info = _base_model_info("codex")
    cache_model = _first_codex_cache_model()
    if cache_model.get("model"):
        _apply_model(
            info,
            cache_model["model"],
            display_name=cache_model.get("displayName", ""),
            provider="openai",
            source="codex-models-cache",
        )

    config_path = Path.home() / ".codex" / "config.toml"
    config: dict[str, Any] = {}
    try:
        import tomllib
        if config_path.exists():
            config = tomllib.loads(config_path.read_text())
    except Exception:
        logger.warning("[backend-models] Failed to read Codex config %s", config_path)

    provider = str(config.get("model_provider") or info.get("provider") or "")
    configured_model = str(config.get("model") or "")
    if configured_model:
        _apply_model(info, configured_model, provider=provider, source="codex-config")

    modes: dict[str, Any] = {}
    for config_key, mode_key in (
        ("model_reasoning_effort", "reasoningEffort"),
        ("model_reasoning_summary", "reasoningSummary"),
        ("approval_policy", "approvalPolicy"),
        ("sandbox_mode", "sandboxMode"),
    ):
        value = config.get(config_key)
        if value not in (None, ""):
            modes[mode_key] = value
    if modes:
        info["modes"] = modes

    if explicit_model:
        _apply_model(info, explicit_model, provider=provider, source="ring0-state", explicit=True)
    return info


def _claude_model_info(explicit_model: str = "") -> dict[str, Any]:
    info = _base_model_info("claude")
    env_model = os.environ.get("CLAUDE_MODEL", "").strip()
    if env_model:
        _apply_model(info, env_model, provider="anthropic", source="env:CLAUDE_MODEL")
    if explicit_model:
        _apply_model(info, explicit_model, provider="anthropic", source="ring0-state", explicit=True)
    return info


def _opencode_model_info(explicit_model: str = "", work_dir: Path | None = None) -> dict[str, Any]:
    info = _base_model_info("opencode")
    config_paths: list[Path] = []
    if work_dir is not None:
        config_paths.append(work_dir / "opencode.jsonc")
    config_paths.extend([
        Path.home() / ".config" / "opencode" / "opencode.jsonc",
        Path.home() / ".config" / "opencode" / "opencode.json",
    ])
    for path in config_paths:
        config = _read_jsonc(path)
        model = str(config.get("model") or "")
        if model:
            _apply_model(info, model, source=str(path))
            break
    if explicit_model:
        _apply_model(info, explicit_model, source="ring0-state", explicit=True)
    return info


def _hermes_model_info(explicit_model: str = "") -> dict[str, Any]:
    info = _base_model_info("hermes")
    try:
        import yaml
        config_path = Path.home() / ".hermes" / "config.yaml"
        if config_path.exists():
            config = yaml.safe_load(config_path.read_text()) or {}
            model_config = config.get("model", {})
            if isinstance(model_config, dict):
                model = str(model_config.get("default") or "")
                provider = str(model_config.get("provider") or "")
                if model:
                    _apply_model(info, model, provider=provider, source="hermes-config")
    except Exception:
        logger.warning("[backend-models] Failed to read Hermes config for model info")
    if explicit_model:
        _apply_model(info, explicit_model, source="ring0-state", explicit=True)
    return info


def get_backend_model_info(
    backend: str,
    explicit_model: str | None = None,
    work_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Return the best passive model/provider/mode awareness for a backend.

    This reads local config/cache files only. It intentionally avoids spawning
    backend CLIs just to answer a status query.
    """
    backend = (backend or "claude").strip()
    explicit = (explicit_model or "").strip()
    wd = Path(work_dir) if work_dir else None
    cache_key = (
        backend,
        explicit,
        str(wd or ""),
        str(Path.home()),
        os.environ.get("CLAUDE_MODEL", ""),
    )
    now = time.monotonic()
    cached = _model_info_cache.get(cache_key)
    if cached and now - cached[0] < _MODEL_INFO_CACHE_TTL:
        return copy.deepcopy(cached[1])

    if backend == "codex":
        info = _codex_config_model_info(explicit)
    elif backend == "opencode":
        info = _opencode_model_info(explicit, wd)
    elif backend == "hermes":
        info = _hermes_model_info(explicit)
    elif backend == "claude":
        info = _claude_model_info(explicit)
    else:
        info = _base_model_info(backend)
        if explicit:
            _apply_model(info, explicit, source="ring0-state", explicit=True)

    _model_info_cache[cache_key] = (now, copy.deepcopy(info))
    return info


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
