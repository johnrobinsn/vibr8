"""
Session name storage (in-memory).

Session names are ephemeral — they live only as long as the server process.
"""

import random

# ── Store ────────────────────────────────────────────────────────────────────

_names: dict[str, str] = {}

# ── Random name generator ───────────────────────────────────────────────────

_ADJECTIVES = [
    "amber", "azure", "bold", "bright", "calm", "cedar", "clear", "coral",
    "crisp", "cyan", "deft", "dusty", "ember", "fern", "fleet", "frost",
    "gilt", "hazy", "ivory", "jade", "keen", "lemon", "lilac", "lunar",
    "maple", "mint", "misty", "moss", "noble", "opal", "pale", "peach",
    "pine", "plum", "quiet", "rapid", "reed", "ruby", "sage", "silk",
    "slate", "slim", "solar", "stark", "steel", "stone", "swift", "teal",
    "vivid", "warm", "wild", "zinc",
]

_NOUNS = [
    "arch", "bass", "beam", "bell", "bird", "blade", "bloom", "bolt",
    "brook", "cairn", "cliff", "cloud", "cove", "crane", "creek", "crow",
    "dune", "echo", "elm", "fawn", "finch", "fjord", "flame", "flint",
    "forge", "fox", "gate", "glen", "grove", "hawk", "heron", "hill",
    "horn", "isle", "jay", "knoll", "lake", "lark", "leaf", "marsh",
    "mesa", "moth", "oak", "owl", "peak", "pine", "pond", "quill",
    "reef", "ridge", "river", "rock", "shell", "shore", "spruce", "stone",
    "stream", "trail", "vale", "wave", "wren",
]


def generate_random_name() -> str:
    """Generate a random 'Adjective Noun' name for a session."""
    adj = random.choice(_ADJECTIVES)
    noun = random.choice(_NOUNS)
    return f"{adj.title()} {noun.title()}"


def _existing_names() -> set[str]:
    return set(_names.values())


def _make_unique(name: str) -> str:
    """Append a numeric suffix if *name* already exists."""
    existing = _existing_names()
    if name not in existing:
        return name
    n = 2
    while f"{name} {n}" in existing:
        n += 1
    return f"{name} {n}"


# ── Public API ───────────────────────────────────────────────────────────────


def get_name(session_id: str) -> str | None:
    return _names.get(session_id)


def set_name(session_id: str, name: str, unique: bool = True) -> None:
    _names[session_id] = _make_unique(name) if unique else name


def get_all_names() -> dict[str, str]:
    return dict(_names)


def remove_name(session_id: str) -> None:
    _names.pop(session_id, None)


def _reset_for_test(custom_path: str | None = None) -> None:
    """Reset internal state (for testing)."""
    global _names
    _names = {}
