"""Pin the native-client wire surface.

Any addition or removal to what the server sends over `/ws/native/*`
or accepts on that channel must move in lockstep with three things:

1. The frozenset constants in `vibr8_core.ws_bridge`
   (`NATIVE_RPC_COMMANDS`, `NATIVE_INBOUND_TYPES`, `NATIVE_PUSH_EVENTS`).
2. The tables in `docs/native-client-contract.md`.
3. The Android wrapper (`/mntc/code/vibr8-android`) — additive on
   the server side is safe for the Android client (strict receiver
   ignores unknowns), but for anything the Android client is
   expected to send, that repo needs a matching change.

These tests fail loudly when (1) drifts from the source code, so
someone forgot to update the constants after touching the wire.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from vibr8_core import ws_bridge
from vibr8_core.ws_bridge import (
    NATIVE_INBOUND_TYPES,
    NATIVE_PUSH_EVENTS,
    NATIVE_RPC_COMMANDS,
)


# ── NATIVE_RPC_COMMANDS ─────────────────────────────────────────────────────


def test_native_rpc_commands_matches_frozen_set() -> None:
    """The set of native-preferred RPC commands the server may send to
    an Android client. Frozen at v1 to exactly these two — see
    docs/native-client-contract.md §B1."""
    assert NATIVE_RPC_COMMANDS == frozenset({
        "bring_to_foreground",
        "launch_app",
    })


def test_rpc_call_uses_native_rpc_commands_constant() -> None:
    """Guard against a refactor that reintroduces a local literal set
    inside `rpc_call` (which is how this constant started — see git
    history). If someone hard-codes the set again, the module-level
    constant and the runtime behavior drift silently."""
    source = Path(ws_bridge.__file__).read_text()
    # Any occurrence of the old literal form outside its comment
    # sentence — e.g. `_native_methods = {"bring_to_foreground", ...}`
    # — would mean the runtime path stopped consulting the constant.
    assert 'method in NATIVE_RPC_COMMANDS' in source, (
        "rpc_call must dispatch off NATIVE_RPC_COMMANDS, not a local literal"
    )
    # Explicitly forbid the old local-variable form re-appearing.
    forbidden = re.compile(r'_native_methods\s*=\s*\{')
    assert not forbidden.search(source), (
        "found a local `_native_methods = {...}` definition — remove it "
        "and reference NATIVE_RPC_COMMANDS instead"
    )


# ── NATIVE_INBOUND_TYPES ────────────────────────────────────────────────────


def test_native_inbound_types_matches_frozen_set() -> None:
    """Native → server ``type`` values (RPC responses excluded — they
    correlate by ``id``, not ``type``). Frozen at v1 to exactly these
    three — see docs/native-client-contract.md §C."""
    assert NATIVE_INBOUND_TYPES == frozenset({
        "subscribe",
        "unsubscribe",
        "permission_response",
    })


def test_handle_native_message_branches_match_inbound_types() -> None:
    """Extract the string literals compared against `msg_type` inside
    `handle_native_message` and assert they match NATIVE_INBOUND_TYPES.
    Fails when a new branch is added to the handler but the constant
    (and the contract doc) wasn't updated."""
    source = Path(ws_bridge.__file__).read_text()

    # Locate the handler body — anything after `async def handle_native_message`
    # until the next top-level def / class.
    start = source.index("async def handle_native_message")
    tail = source[start:]
    # Cut off at the next method def at the same indentation.
    end_match = re.search(r"\n    (async def |def )", tail[1:])
    body = tail[: end_match.start() + 1] if end_match else tail

    # Every `msg_type == "…"` branch.
    branches = set(re.findall(r'msg_type\s*==\s*"([a-z_]+)"', body))
    assert branches == set(NATIVE_INBOUND_TYPES), (
        f"handle_native_message branches {branches} != NATIVE_INBOUND_TYPES "
        f"{set(NATIVE_INBOUND_TYPES)}. Either add/remove a branch or update "
        "the constant + docs/native-client-contract.md §C."
    )


# ── NATIVE_PUSH_EVENTS ──────────────────────────────────────────────────────


def test_native_push_events_matches_frozen_set() -> None:
    """The node-agnostic observer contract's push event catalog —
    see docs/native-client-contract.md §B2. Reduced in v1 to
    `attention` and `busy` (relays of events/v1 hooks) because the
    prior larger set leaked ``vibr8_node``-specific concepts
    (sessions, CLI, permission_request) into a channel other node
    shapes (hello-node, wear-node) must also implement without
    those primitives."""
    assert NATIVE_PUSH_EVENTS == frozenset({
        "attention",
        "busy",
    })


def test_push_to_native_clients_call_sites_use_documented_events() -> None:
    """Grep every ``_push_to_native_clients(..., "<event>", ...)`` and
    ``_push_to_all_native_clients("<event>", ...)`` call in
    ``ws_bridge.py`` and assert every event name is in
    NATIVE_PUSH_EVENTS. The prior transitional allowance for
    legacy-dark names is gone; the ``vibr8_node``-specific fire sites
    have been removed and the two helpers are now called only by the
    hub's tunnel handler for events/v1 ``attention``/``busy``. Any
    new push added must appear in the contract set."""
    source = Path(ws_bridge.__file__).read_text()

    per_session = re.compile(
        r'_push_to_native_clients\([^,]+,\s*"([a-z_]+)"',
    )
    fanout = re.compile(
        r'_push_to_all_native_clients\(\s*"([a-z_]+)"',
    )

    found = set(per_session.findall(source)) | set(fanout.findall(source))
    unknown = found - set(NATIVE_PUSH_EVENTS)
    assert not unknown, (
        f"_push_to_native_clients call sites in ws_bridge.py emit event "
        f"names not in NATIVE_PUSH_EVENTS: {sorted(unknown)}. Either "
        "add to the contract (v1 doc §B2 + NATIVE_PUSH_EVENTS) or "
        "remove the call site."
    )


def test_no_legacy_dark_fires_in_ws_bridge() -> None:
    """The old ``vibr8_node``-specific push event names must not be
    fired from ``ws_bridge.py`` under any wrapping. Guards against
    regressions where someone reintroduces a dark
    ``_push_to_native_clients(session.id, "permission_request", …)``
    kind of call (which reached no client before the migration but
    would silently bypass the events/v1 relay after)."""
    source = Path(ws_bridge.__file__).read_text()
    forbidden = {
        "guard_state", "tts_muted", "voice_mode",
        "status_change",
        "permission_request", "permission_cancelled",
        "cli_connected", "cli_disconnected",
        "sessions_changed",
    }
    for name in forbidden:
        pattern = re.compile(
            rf'_push_to_(?:all_)?native_clients\([^)]*"{name}"',
        )
        matches = pattern.findall(source)
        assert not matches, (
            f"Reintroduced legacy dark push fire for {name!r} in "
            "ws_bridge.py — this event name is a vibr8_node-specific "
            "concept and doesn't belong on the node-agnostic observer "
            "channel. Route through _attention_hook or drop."
        )


# ── Cross-repo assertion (best-effort, dev-machine only) ────────────────────

_ANDROID_KEEP_ALIVE = Path(
    "/mntc/code/vibr8-android/android/app/src/main/java/ai/ringzero/vibr8/"
    "KeepAliveService.java",
)


@pytest.mark.skipif(
    not _ANDROID_KEEP_ALIVE.is_file(),
    reason="vibr8-android repo not present on this machine",
)
def test_android_keepalive_recognizes_exactly_native_rpc_commands() -> None:
    """When both repos are on disk, guarantee the Android side
    dispatches on exactly the RPC commands the server sends. Detects
    the classic drift where one side ships a new command without the
    other.

    Extracts every `"<name>".equals(command)` literal in
    KeepAliveService.java and compares against NATIVE_RPC_COMMANDS.
    Skipped when the Android repo isn't checked out beside this one
    (CI probably won't have it; the check earns its keep locally)."""
    source = _ANDROID_KEEP_ALIVE.read_text()
    dispatched = set(
        re.findall(r'"([a-z_]+)"\s*\.equals\(\s*command\s*\)', source),
    )
    assert dispatched == set(NATIVE_RPC_COMMANDS), (
        f"KeepAliveService dispatches on {dispatched}, NATIVE_RPC_COMMANDS "
        f"is {set(NATIVE_RPC_COMMANDS)}. Either the server added a command "
        "without updating the Android client, or vice versa."
    )
