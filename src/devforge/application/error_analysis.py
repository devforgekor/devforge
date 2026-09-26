#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driving/cli_cmds/errors.py (via cli.py composition root)
"""Error-record analysis (§2) — structured incidents → decision packet.

Read-only, intent-only (error-record-analysis-design.md §2): it clusters
structured incidents and ranks root-cause hypotheses, but never executes fixes.
Rules are the first stage; an LLM second stage (design §4) plugs in behind
`llm_used` when a reasoning client is wired — the packet shape is unchanged.

Layering: this module imports only `devforge.ports` and stdlib (the repository
adapter is injected by the composition root), keeping the `layering` contract.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Sequence

from devforge.core.logging import get_logger
from devforge.ports.error_analysis import (
    ErrorAnalysisRepository,
    Hypothesis,
    HypothesisPort,
    IncidentEvidence,
)

_log = get_logger(__name__)

_PROPOSE_MIN_CONFIDENCE = 0.5


def _cause_signature(ctx: dict[str, Any]) -> str:
    """Best-effort root-cause key from L2 structured diagnostics."""
    exc = ctx.get("exception") or {}
    if isinstance(exc, dict) and exc.get("type"):
        return f"exception:{exc['type']}"
    exit_code = ctx.get("exit_code")
    if exit_code in (None, 0, "0"):
        systemd = ctx.get("systemd") or {}
        exit_code = systemd.get("ExecMainStatus") if isinstance(systemd, dict) else None
    if exit_code not in (None, 0, "0"):
        return f"exit:{exit_code}"
    systemd = ctx.get("systemd") or {}
    result = systemd.get("Result") if isinstance(systemd, dict) else None
    if result and result not in ("success",):
        return f"systemd:{result}"
    return "unknown"


def _severity(incidents: Sequence[IncidentEvidence]) -> str:
    max_fail = max((i.fail_count for i in incidents), default=1)
    max_reopen = max((i.reopen_count for i in incidents), default=0)
    if max_reopen >= 3 or max_fail >= 5:
        return "high"
    if max_reopen >= 1 or max_fail >= 2:
        return "medium"
    return "low"


def _fix_proposal(signature: str) -> dict[str, Any]:
    """Generic, non-destructive remediation guidance keyed by signature."""
    if signature.startswith("exception:"):
        actions = ["Inspect the captured traceback (L2 exception) and reproduce locally"]
    elif signature.startswith("exit:"):
        actions = ["Check last action stderr (action_error) and service dependencies"]
    elif signature.startswith("systemd:"):
        actions = ["Inspect journal tail for the failed unit and restart policy"]
    else:
        actions = ["Gather more evidence (L2 degraded sections) before acting"]
    return {
        "actions": actions,
        "verify": ["Re-run the component health check", "Confirm no reopen within one interval"],
        "stop_conditions": ["Two consecutive fix attempts fail", "Confidence drops below 0.5"],
        "risk": "low",
    }


def build_decision_packet(
    incidents: Sequence[IncidentEvidence],
    *,
    since_iso: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pure rules stage → decision packet (design §2.4). No I/O, no execution."""
    now = now or datetime.now(timezone.utc)
    packet: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "window": {"since": since_iso, "incident_count": len(incidents)},
        "llm_used": False,
        "evidence": [
            {
                "incident_id": i.incident_id,
                "component": i.component,
                "raw_ref": f"incidents:{i.incident_id}",
            }
            for i in incidents
        ],
        "cluster": {"components": [], "shared_cause_confidence": 0.0},
        "root_cause": {"hypothesis": None, "confidence": 0.0, "alternatives": []},
        "fix_proposal": None,
        "decision": "insufficient_evidence",
    }
    if not incidents:
        return packet

    by_signature: dict[str, list[IncidentEvidence]] = defaultdict(list)
    for inc in incidents:
        by_signature[_cause_signature(inc.context)].append(inc)

    # Rank signatures by weighted evidence (fail_count), newest-first tiebreak.
    def weight(items: list[IncidentEvidence]) -> int:
        return sum(max(1, i.fail_count) for i in items)

    ranked = sorted(by_signature.items(), key=lambda kv: weight(kv[1]), reverse=True)
    top_signature, top_items = ranked[0]
    components = sorted({i.component for i in incidents})
    top_components = sorted({i.component for i in top_items})
    shared = len(top_components) >= 2
    confidence = round(min(0.9, 0.4 + 0.1 * len(top_items) + (0.15 if shared else 0.0)), 2)
    if top_signature == "unknown":
        confidence = min(confidence, 0.3)  # no usable signature → escalate

    packet["severity"] = _severity(incidents)
    packet["cluster"] = {
        "components": components,
        "shared_cause_confidence": confidence if shared else round(confidence * 0.5, 2),
    }
    packet["root_cause"] = {
        "hypothesis": top_signature,
        "confidence": confidence,
        "alternatives": [
            {"hypothesis": sig, "confidence": round(confidence * 0.5, 2)} for sig, _ in ranked[1:]
        ],
    }
    packet["fix_proposal"] = _fix_proposal(top_signature)
    if confidence >= _PROPOSE_MIN_CONFIDENCE:
        packet["decision"] = "propose"
    else:
        packet["decision"] = "escalate"
    return packet


_LLM_BUDGET_SEC = 120.0  # cap the whole LLM stage so `errors analyze` never hangs

_HYPOTHESIS_SYSTEM = (
    "You are a read-only SRE analyst. Given structured watchdog incident evidence, "
    "propose the most likely root causes. Output ONLY a JSON array of objects "
    '{"hypothesis": str, "confidence": 0..1, "rationale": str}. Cite only the provided '
    "components; never invent evidence. Maximum 3 items."
)


def build_hypothesis_prompt(packet: dict[str, Any]) -> list[dict[str, str]]:
    """Pure: render the rules packet into an LLM chat request (design §4)."""
    evidence = packet.get("evidence", [])
    lines = [
        f"- incident {e['incident_id']} component={e['component']} ({e['raw_ref']})"
        for e in evidence
    ]
    user = (
        f"Window: {packet.get('window')}\n"
        f"Rule-based cluster: {packet.get('cluster')}\n"
        f"Rule-based root_cause: {packet.get('root_cause')}\n"
        f"Evidence ({len(evidence)}):\n" + "\n".join(lines)
    )
    return [
        {"role": "system", "content": _HYPOTHESIS_SYSTEM},
        {"role": "user", "content": user},
    ]


def merge_hypotheses(
    packet: dict[str, Any], hypotheses: Sequence[Hypothesis], *, model: str
) -> dict[str, Any]:
    """Pure: fold LLM hypotheses into the packet (§2.5 uncertainty + raw_ref)."""
    if not hypotheses:
        return packet
    ranked = sorted(hypotheses, key=lambda h: h.confidence, reverse=True)
    top = ranked[0]
    prev = packet.get("root_cause") or {}
    alternatives: list[dict[str, Any]] = []
    if prev.get("hypothesis"):
        alternatives.append(
            {"hypothesis": prev.get("hypothesis"), "confidence": prev.get("confidence")}
        )
    alternatives.extend(
        {"hypothesis": h.hypothesis, "confidence": round(h.confidence, 2)} for h in ranked[1:]
    )
    merged = dict(packet)
    merged["root_cause"] = {
        "hypothesis": top.hypothesis,
        "confidence": round(top.confidence, 2),
        "rationale": top.rationale,
        "alternatives": alternatives,
    }
    merged["llm_used"] = True
    merged["llm_model"] = model
    if top.confidence >= _PROPOSE_MIN_CONFIDENCE:
        merged["decision"] = "propose"
    return merged


class ErrorAnalysisService:
    """Reads structured incidents and builds a decision packet (no side effects)."""

    def __init__(
        self, repository: ErrorAnalysisRepository, hypothesis: HypothesisPort | None = None
    ) -> None:
        self._repository = repository
        self._hypothesis = hypothesis

    async def analyze(self, since_iso: str, *, use_llm: bool = False) -> dict[str, Any]:
        incidents = await self._repository.list_incidents(since_iso)
        packet = build_decision_packet(incidents, since_iso=since_iso)
        if not use_llm or self._hypothesis is None or not incidents:
            return packet
        try:
            hypotheses = await asyncio.wait_for(
                self._hypothesis.hypothesize(build_hypothesis_prompt(packet)),
                timeout=_LLM_BUDGET_SEC,
            )
        except Exception as e:  # noqa: BLE001 — LLM stage must never break analysis
            _log.warning("error_analysis_llm_failed", error=str(e))
            return packet
        model = getattr(self._hypothesis, "model_name", "llm")
        return merge_hypotheses(packet, hypotheses, model=model)
