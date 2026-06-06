from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from vibr8_core import backend_models
from vibr8_core.node_operations import NodeOperations


def test_codex_model_info_reads_config_cache_and_reasoning_modes(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)

    (codex_dir / "models_cache.json").write_text(json.dumps({
        "models": [
            {
                "slug": "gpt-5.3-codex",
                "display_name": "GPT-5.3 Codex",
                "description": "",
                "visibility": "list",
                "priority": 1,
            }
        ]
    }))
    (codex_dir / "config.toml").write_text(
        "\n".join([
            'model = "gpt-5.2-codex"',
            'model_provider = "openai"',
            'model_reasoning_effort = "high"',
            'model_reasoning_summary = "auto"',
        ])
    )

    info = backend_models.get_backend_model_info("codex")

    assert info["backend"] == "codex"
    assert info["provider"] == "openai"
    assert info["model"] == "gpt-5.2-codex"
    assert info["source"] == "codex-config"
    assert info["modes"] == {
        "reasoningEffort": "high",
        "reasoningSummary": "auto",
    }


def test_explicit_ring0_model_overrides_config_but_keeps_codex_modes(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    (codex_dir / "config.toml").write_text(
        "\n".join([
            'model = "gpt-5.2-codex"',
            'model_provider = "openai"',
            'model_reasoning_effort = "medium"',
        ])
    )

    info = backend_models.get_backend_model_info("codex", explicit_model="gpt-5.3-codex")

    assert info["model"] == "gpt-5.3-codex"
    assert info["source"] == "ring0-state"
    assert info["isExplicit"] is True
    assert info["modes"]["reasoningEffort"] == "medium"


def test_opencode_model_info_reads_workdir_jsonc_and_provider(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    (work_dir / "opencode.jsonc").write_text(
        "\n".join([
            "{",
            "  // project default",
            '  "apiUrl": "https://example.test/v1",',
            '  "model": "openai/gpt-4o"',
            "}",
        ])
    )

    info = backend_models.get_backend_model_info("opencode", work_dir=work_dir)

    assert info["backend"] == "opencode"
    assert info["provider"] == "openai"
    assert info["model"] == "openai/gpt-4o"
    assert info["source"] == str(work_dir / "opencode.jsonc")


def test_claude_model_info_uses_env_when_present(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-4-6")

    info = backend_models.get_backend_model_info("claude")

    assert info["provider"] == "anthropic"
    assert info["model"] == "claude-sonnet-4-6"
    assert info["source"] == "env:CLAUDE_MODEL"


async def test_node_operations_enriches_regular_session_model_info(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    (codex_dir / "config.toml").write_text(
        "\n".join([
            'model = "gpt-5.2-codex"',
            'model_provider = "openai"',
            'model_reasoning_effort = "high"',
        ])
    )

    session = SimpleNamespace(
        sessionId="s1",
        model=None,
        cwd=str(tmp_path),
        backendType="codex",
        to_dict=lambda: {
            "sessionId": "s1",
            "state": "connected",
            "model": None,
            "cwd": str(tmp_path),
            "backendType": "codex",
        },
    )
    launcher = SimpleNamespace(list_sessions=lambda: [session])
    bridge = SimpleNamespace(_sessions={}, get_last_prompted_at=lambda _sid: 0)
    ops = NodeOperations(
        launcher=launcher,
        bridge=bridge,
        store=SimpleNamespace(),
        ring0=None,
    )

    result = await ops.list_sessions()
    listed = result["sessions"][0]

    assert listed["model"] == "gpt-5.2-codex"
    assert listed["modelInfo"]["backend"] == "codex"
    assert listed["modelInfo"]["provider"] == "openai"
    assert listed["modelInfo"]["modes"]["reasoningEffort"] == "high"
