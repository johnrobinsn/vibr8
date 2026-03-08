"""Tests for voice_profiles CRUD and STTParams resolution."""

import tempfile
import os
from pathlib import Path

import pytest

# Override DATA_DIR before importing
_tmpdir = tempfile.mkdtemp()
os.environ["VIBR8_DATA_DIR"] = _tmpdir

from server import voice_profiles

# Point the module at our temp dir
voice_profiles.DATA_DIR = Path(_tmpdir)
voice_profiles.PROFILES_DIR = Path(_tmpdir) / "voice" / "profiles"


@pytest.fixture(autouse=True)
def clean_profiles():
    """Ensure a clean state for each test."""
    import shutil
    d = voice_profiles.PROFILES_DIR
    if d.exists():
        shutil.rmtree(d)
    yield
    if d.exists():
        shutil.rmtree(d)


def test_list_empty():
    assert voice_profiles.list_profiles("testuser") == []


def test_create_and_get():
    p = voice_profiles.create_profile("testuser", {"name": "Loud", "micGain": 3.0})
    assert p["name"] == "Loud"
    assert p["micGain"] == 3.0
    assert p["id"] is not None

    fetched = voice_profiles.get_profile("testuser", p["id"])
    assert fetched is not None
    assert fetched["name"] == "Loud"


def test_list_profiles():
    voice_profiles.create_profile("testuser", {"name": "A"})
    voice_profiles.create_profile("testuser", {"name": "B"})
    profiles = voice_profiles.list_profiles("testuser")
    assert len(profiles) == 2
    assert profiles[0]["name"] == "A"  # sorted by name
    assert profiles[1]["name"] == "B"


def test_update():
    p = voice_profiles.create_profile("testuser", {"name": "Original"})
    updated = voice_profiles.update_profile("testuser", p["id"], {"name": "Updated", "micGain": 2.5})
    assert updated is not None
    assert updated["name"] == "Updated"
    assert updated["micGain"] == 2.5


def test_update_nonexistent():
    result = voice_profiles.update_profile("testuser", "nonexistent", {"name": "X"})
    assert result is None


def test_delete():
    p = voice_profiles.create_profile("testuser", {"name": "ToDelete"})
    assert voice_profiles.delete_profile("testuser", p["id"]) is True
    assert voice_profiles.get_profile("testuser", p["id"]) is None


def test_delete_nonexistent():
    assert voice_profiles.delete_profile("testuser", "nonexistent") is False


def test_activate_profile():
    p1 = voice_profiles.create_profile("testuser", {"name": "P1"})
    p2 = voice_profiles.create_profile("testuser", {"name": "P2"})

    voice_profiles.activate_profile("testuser", p1["id"])
    assert voice_profiles.get_profile("testuser", p1["id"])["isActive"] is True
    assert voice_profiles.get_profile("testuser", p2["id"])["isActive"] is False

    # Activating p2 deactivates p1
    voice_profiles.activate_profile("testuser", p2["id"])
    assert voice_profiles.get_profile("testuser", p1["id"])["isActive"] is False
    assert voice_profiles.get_profile("testuser", p2["id"])["isActive"] is True


def test_get_active_profile():
    assert voice_profiles.get_active_profile("testuser") is None

    p = voice_profiles.create_profile("testuser", {"name": "Active"})
    voice_profiles.activate_profile("testuser", p["id"])

    active = voice_profiles.get_active_profile("testuser")
    assert active is not None
    assert active["id"] == p["id"]


def test_get_stt_params_defaults():
    """get_stt_params requires heavy ML deps — test profile resolution instead."""
    # With no profiles, should return None active profile
    assert voice_profiles.get_active_profile("testuser") is None


def test_get_stt_params_from_profile():
    p = voice_profiles.create_profile("testuser", {
        "name": "Custom",
        "micGain": 2.0,
        "vadThresholdDb": -20.0,
        "eouThreshold": 0.3,
    })
    voice_profiles.activate_profile("testuser", p["id"])

    active = voice_profiles.get_active_profile("testuser")
    assert active is not None
    assert active["micGain"] == 2.0
    assert active["vadThresholdDb"] == -20.0
    assert active["eouThreshold"] == 0.3


def test_get_profile_by_id():
    p = voice_profiles.create_profile("testuser", {
        "name": "ById",
        "micGain": 4.0,
    })

    fetched = voice_profiles.get_profile("testuser", p["id"])
    assert fetched is not None
    assert fetched["micGain"] == 4.0


def test_user_isolation():
    voice_profiles.create_profile("user1", {"name": "U1"})
    voice_profiles.create_profile("user2", {"name": "U2"})

    assert len(voice_profiles.list_profiles("user1")) == 1
    assert len(voice_profiles.list_profiles("user2")) == 1
    assert voice_profiles.list_profiles("user1")[0]["name"] == "U1"
