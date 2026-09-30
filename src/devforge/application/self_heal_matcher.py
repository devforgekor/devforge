#!/usr/bin/env python3.12
# Status: experimental
# Path: application/watchdog_service.py (composition root)
"""Self-heal matcher — auto-execute known, low-risk remediation patterns.

Matches incidents against pre-approved patterns. If a match is found and
the pattern is marked auto_safe, the remediation executes without approval
(earned autonomy, not granted).
"""

from __future__ import annotations

from typing import Any, Optional

from devforge.core.logging import get_logger
from devforge.ports.types import RemediationPlan

_log = get_logger(__name__)

# Pre-approved self-heal patterns: (component_prefix, event_type) → action
# [WHY] Only patterns with small blast radius and clear verification are listed.
_SELF_HEAL_PATTERNS: dict[tuple[str, str], dict[str, Any]] = {
    ("svc:ebook-watcher", "down"): {
        "action": "restart",
        "auto_safe": True,
        "verify": "check_ebook_pipeline",
    },
    ("oneshot:devforge-backup.service", "failed"): {
        "action": "rerun",
        "auto_safe": True,
        "verify": "check_oneshot_result",
    },
}


class SelfHealMatcher:
    """Matches incidents against pre-approved self-heal patterns."""

    def match(self, component: str, event_type: str) -> Optional[RemediationPlan]:
        """Return a RemediationPlan if the incident matches a self-heal pattern."""
        pattern = _SELF_HEAL_PATTERNS.get((component, event_type))
        if pattern is None:
            return None
        if not pattern.get("auto_safe", False):
            return None
        from devforge.ports.types import RemediationStep

        return RemediationPlan(
            component=component,
            steps=[
                RemediationStep("pre-check", "pre-check", {"component": component}),
                RemediationStep("execute", "execute", {"action": pattern["action"]}),
                RemediationStep("verify", "verify", {"method": pattern["verify"]}),
            ],
            impact="mutating",
            approved=True,
        )
