"""Ring0 Event Configuration — structured events with config-driven routing.

Events are structured JSON objects that pass through a rules engine loaded from
~/.vibr8/ring0-events.json5.  Rules control whether events reach Ring0, what
template text the LLM sees, and how the UI renders them (visible / collapsed /
hidden).  First matching rule wins.  See ring0-events.example.json5 for the
full schema and examples.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import json5

logger = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".vibr8" / "ring0-events.json5"


@dataclass
class Ring0Event:
    """A structured event to be routed to Ring0.

    All event data lives in `fields` as a flat dict.  The `type` key is
    conventional but not special — match rules treat every key uniformly.
    """
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProcessedEvent:
    """Result of applying a config rule to an event."""
    text: str                    # expanded template — what the LLM sees
    summary: str | None          # short label for collapsed UI
    ui: str                      # "visible" | "collapsed" | "hidden"
    event: Ring0Event            # original event


@dataclass
class _EventRule:
    """A single config rule (internal representation)."""
    match: dict[str, str]
    suppress: bool = False
    template: str | None = None
    summary: str | None = None
    ui: str = "visible"


class Ring0EventRouter:
    """Loads event config and routes Ring0Event objects through rules."""

    def __init__(self) -> None:
        self._rules: list[_EventRule] = []
        self._load_config()

    def _load_config(self) -> None:
        """Load rules from ~/.vibr8/ring0-events.json5."""
        if not CONFIG_PATH.exists():
            logger.info("[ring0-events] No config at %s — using defaults", CONFIG_PATH)
            return
        try:
            data = json5.loads(CONFIG_PATH.read_text())
            raw_rules = data.get("rules", [])
            self._rules = []
            for r in raw_rules:
                match = r.get("match", {})
                if not match:
                    continue
                self._rules.append(_EventRule(
                    match=match,
                    suppress=bool(r.get("suppress", False)),
                    template=r.get("template"),
                    summary=r.get("summary"),
                    ui=r.get("ui", "visible"),
                ))
            logger.info("[ring0-events] Loaded %d rule(s) from %s", len(self._rules), CONFIG_PATH)
        except Exception:
            logger.exception("[ring0-events] Failed to parse %s — using defaults", CONFIG_PATH)
            self._rules = []

    def process(self, event: Ring0Event) -> ProcessedEvent | None:
        """Apply first-match-wins rules.  Returns None if suppressed."""
        for rule in self._rules:
            if self._match_rule(rule, event):
                if rule.suppress:
                    return None
                text = self._expand(rule.template, event) if rule.template else self._default_text(event)
                summary = self._expand(rule.summary, event) if rule.summary else None
                return ProcessedEvent(text=text, summary=summary, ui=rule.ui, event=event)

        # No rule matched — hardcoded fallback
        return ProcessedEvent(
            text=self._default_text(event),
            summary=None,
            ui="visible",
            event=event,
        )

    # ── internals ────────────────────────────────────────────────────────

    def _match_rule(self, rule: _EventRule, event: Ring0Event) -> bool:
        """Check if a rule matches an event.  All keys use fnmatch glob."""
        for key, pattern in rule.match.items():
            value = str(event.fields.get(key, ""))
            if not fnmatch.fnmatch(value, pattern):
                return False
        return True

    def _expand(self, template: str, event: Ring0Event) -> str:
        """Expand ${evt.field} and ${evt} placeholders in a template."""
        result = template
        # Replace ${evt} with full JSON (must be done before field-level replacements)
        if "${evt}" in result:
            result = result.replace("${evt}", json.dumps(event.fields))
        # Replace ${evt.fieldName}
        def _replace_field(m: re.Match) -> str:
            return str(event.fields.get(m.group(1), ""))
        result = re.sub(r"\$\{evt\.(\w+)\}", _replace_field, result)
        return result

    def _default_text(self, event: Ring0Event) -> str:
        """Default text when no template is configured."""
        evt_type = event.fields.get("type", "unknown")
        rest = {k: v for k, v in event.fields.items() if k != "type"}
        return f"[event {evt_type}] {json.dumps(rest)}"
