"""Ring0 authentication integration tests."""

from __future__ import annotations

from vibr8_core.ring0 import Ring0Manager


class EnabledAuth:
    enabled = True

    def __init__(self) -> None:
        self.created_for: list[str] = []

    def create_service_token(self, service_name: str) -> str:
        self.created_for.append(service_name)
        return f"svc-token-for-{service_name}"


class DisabledAuth:
    enabled = False

    def create_service_token(self, service_name: str) -> str:
        raise AssertionError("disabled auth should not create service tokens")


def test_ring0_mcp_env_includes_service_token_when_auth_enabled(tmp_path) -> None:
    auth = EnabledAuth()
    manager = Ring0Manager(
        3456,
        auth_manager=auth,  # type: ignore[arg-type]
        config_path=tmp_path / "ring0.json",
        work_dir=tmp_path / "ring0",
        scheme="http",
    )

    env = manager._get_mcp_env()

    assert env["VIBR8_TOKEN"] == "svc-token-for-ring0"
    assert auth.created_for == ["ring0"]


def test_ring0_mcp_env_omits_service_token_when_auth_disabled(tmp_path) -> None:
    manager = Ring0Manager(
        3456,
        auth_manager=DisabledAuth(),  # type: ignore[arg-type]
        config_path=tmp_path / "ring0.json",
        work_dir=tmp_path / "ring0",
        scheme="http",
    )

    env = manager._get_mcp_env()

    assert "VIBR8_TOKEN" not in env


def test_ring0_mcp_env_omits_service_token_without_auth_manager(tmp_path) -> None:
    manager = Ring0Manager(
        3456,
        auth_manager=None,
        config_path=tmp_path / "ring0.json",
        work_dir=tmp_path / "ring0",
        scheme="http",
    )

    env = manager._get_mcp_env()

    assert "VIBR8_TOKEN" not in env
