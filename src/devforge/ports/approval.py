#!/usr/bin/env python3.12
# Status: experimental
# Path: application/controllers.py (HITL gate)
"""Human-in-the-loop approval port (HITL gate).

Before executing a mutating action, the controller requests approval via this
port. The port binds evidence, action, deadline, and result into one auditable
record (Elastic HITL pattern). If no approver is wired, the action is denied
(fail closed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ApprovalRequest:
    """One approval request with evidence, action, and deadline."""

    action_id: str
    component: str
    action: str
    evidence: dict[str, Any] = field(default_factory=dict)
    deadline_sec: int = 300


@dataclass(frozen=True)
class ApprovalDecision:
    """One approval decision (approved/denied)."""

    action_id: str
    approved: bool
    approver: str = ""
    reason: str = ""


class ApprovalPort(Protocol):
    """HITL approval gate — mutating actions require approval before execution."""

    async def request(self, req: ApprovalRequest) -> ApprovalDecision: ...
