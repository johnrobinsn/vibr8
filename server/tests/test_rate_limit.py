from __future__ import annotations

from server.rate_limit import get_client_rate_limit_key, normalize_rate_limit_key


class FakeRequest:
    def __init__(self, *, remote: str, headers: dict[str, str] | None = None):
        self.remote = remote
        self.headers = headers or {}


def test_normalize_rate_limit_key_buckets_ipv6_to_64() -> None:
    first = normalize_rate_limit_key("2001:db8:abcd:1234::1")
    second = normalize_rate_limit_key("2001:db8:abcd:1234::feed")

    assert first == second
    assert first == "2001:db8:abcd:1234::/64"


def test_normalize_rate_limit_key_preserves_ipv4_address() -> None:
    assert normalize_rate_limit_key("203.0.113.10") == "203.0.113.10"
    assert normalize_rate_limit_key("203.0.113.10:443") == "203.0.113.10"


def test_get_client_rate_limit_key_ignores_forwarded_for_by_default() -> None:
    request = FakeRequest(
        remote="127.0.0.1",
        headers={"X-Forwarded-For": "203.0.113.10"},
    )

    assert get_client_rate_limit_key(request, environ={}) == "127.0.0.1"


def test_get_client_rate_limit_key_uses_first_forwarded_for_when_trusted() -> None:
    request = FakeRequest(
        remote="127.0.0.1",
        headers={"X-Forwarded-For": "203.0.113.10, 198.51.100.2"},
    )

    assert (
        get_client_rate_limit_key(request, environ={"VIBR8_TRUST_PROXY": "1"})
        == "203.0.113.10"
    )
