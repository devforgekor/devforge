#!/usr/bin/env python3
# Status: experimental
# Path: domain/watchdog/recovery/
"""Recovery kind classification (orchestrator.py dispatch table)."""

from __future__ import annotations

from typing import Optional, Protocol

from devforge.ports.types import RecoveryAction

# Exact-match overrides take precedence over prefix rules.
_EXACT: dict[str, str] = {
    "svc:svc-pod-forwarding": "svcpod",  # recover_svcpod_forwarding
    "svc:ebook-watcher": "ebook",  # recover_ebook_watcher
    "system:memory": "oom",  # recover_oom
    # Alert-only (legacy ALERT_ONLY_TARGETS) — monitor only, never restart.
    "svc:container-postgres": "",
    "svc:container-devforge-mcp": "",
    "svc:container-flaresolverr": "",
    "svc:anthropic-openrouter-proxy": "",
    "svc:anthropic-proxy": "",
    "svc:or-rate-limiter": "",
}
# Prefix → kind. "" prefix means "no recovery (alert-only)".
_PREFIX: dict[str, str] = {
    "oneshot:": "oneshot",  # recover_oneshot
    "timer:": "timer_kick",  # systemctl --user start <svc>
    "llm:": "cascade",  # recover_inference_cascade
    "infra:": "cascade",
    "pipeline:": "pipeline",  # systemctl --user restart devforge-day-cycle
    "syssvc:": "",  # alert-only (rootful, no restart)
    "system:disk": "",  # alert-only
    "dataimpulse:": "",  # alert-only (path liveness; recovery is ebooklib's job)
}


def classify_recovery_kind(component: str) -> Optional[str]:
    """Return the recovery kind for a tracker key, or None if alert-only."""
    if component in _EXACT:
        return _EXACT[component] or None
    if component.startswith("svc:container-"):
        return "container"  # recover_container
    if component.startswith("svc:"):
        return "service"  # recover_service
    for prefix, kind in _PREFIX.items():
        if component.startswith(prefix):
            return kind or None
    return None


class RecoveryStrategy(Protocol):
    def kind_for(self, component: str) -> Optional[str]: ...
    def create_action(
        self, component: str, state: str, reason: str, backoff_sec: int
    ) -> Optional[RecoveryAction]: ...


class DefaultRecoveryStrategy:
    """Maps a component key to a RecoveryAction using the legacy dispatch table."""

    def kind_for(self, component: str) -> Optional[str]:
        return classify_recovery_kind(component)

    def create_action(
        self, component: str, state: str, reason: str, backoff_sec: int
    ) -> Optional[RecoveryAction]:
        kind = self.kind_for(component)
        if kind is None:
            return None
        return RecoveryAction(
            component=component, kind=kind, reason=reason, backoff_sec=backoff_sec
        )
