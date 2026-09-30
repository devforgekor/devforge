#!/usr/bin/env python3.12
# Status: experimental
# Path: application/controllers.py (HITL gate)
"""In-memory approval adapter (HITL gate).

Stores pending approvals in memory for test/dev. Production wiring would
replace this with Slack/email approval. Fail-closed: if no approver is
registered, all requests are denied.
"""

from __future__ import annotations

import time

from devforge.core.logging import get_logger
from devforge.ports.approval import ApprovalDecision, ApprovalRequest

_log = get_logger(__name__)


class MemoryApprovalAdapter:
    """In-memory approval store — fail closed when no approver is set."""

    def __init__(self, default_approver: str = "") -> None:
        self._approver = default_approver
        self._pending: dict[str, ApprovalRequest] = {}
        self._decisions: dict[str, ApprovalDecision] = {}

    def set_approver(self, name: str) -> None:
        self._approver = name

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        if not self._approver:
            _log.warning("approval_denied_no_approver", action_id=req.action_id)
            return ApprovalDecision(req.action_id, False, "", "no approver registered")
        if req.action_id in self._decisions:
            return self._decisions[req.action_id]
        self._pending[req.action_id] = req
        _log.info("approval_requested", action_id=req.action_id, component=req.component)
        return ApprovalDecision(req.action_id, True, self._approver, "auto-approved")

    def approve(self, action_id: str, approver: str = "") -> None:
        self._decisions[action_id] = ApprovalDecision(action_id, True, approver or self._approver, "")

    def deny(self, action_id: str, reason: str = "") -> None:
        self._decisions[action_id] = ApprovalDecision(action_id, False, "", reason)

    def pending(self) -> list[ApprovalRequest]:
        now = time.monotonic()
        return [r for r in self._pending.values() if now - getattr(r, "_ts", 0) < r.deadline_sec]
