#!/usr/bin/env python3
# Status: production
# Path: imported by review_pipeline_3model.py
"""Step implementations for the 3-Model Review Pipeline.

Contains constants, system prompts, HTTP helpers, and the 4 step functions:
  Step 1  run_deep_review   R1-8B (:8083) — bug/security/edge-case discovery
  Step 2  run_reflection    Qwen7B (:8080) — ACCEPT/REJECT per finding
  Step 3  run_judgment      Selene (:8081)  — Scoring Judge (P/R scores, gap, veto)
  Step 4  run_diff          Qwen7B (:8080)  — unified diff for approved findings

Exported symbols consumed by review_pipeline_3model.py:
  R1_PORT, QWEN7B_PORT, SELENE_PORT, _poll_health,
  run_deep_review, run_reflection, run_judgment, run_diff
"""

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

from lib import scoring as _sc
from lib.llm_client import call_llm, call_llm_json

# ── Constants ──────────────────────────────────────────────────────────────
R1_PORT = 8083  # Pod A night: R1-8B
QWEN7B_PORT = 8080  # Pod B: Qwen7B (reflection + diff)
SELENE_PORT = 8081  # Pod B swap: Selene (Step 3 judge)
TIMEOUT_STEP1 = 900  # 15 min
TIMEOUT_STEP2 = 480  #  8 min
TIMEOUT_STEP3 = 480  #  8 min (Scoring Judge: rubric + decisions)
TIMEOUT_STEP4 = 600  # 10 min
MAX_TOKENS_STEP1 = 2048
MAX_TOKENS_STEP2 = 400
MAX_TOKENS_STEP3 = 1024  # Scoring Judge: rubric + scores + decisions + machine_summary
MAX_TOKENS_STEP4 = 2048

THINK_STRIP_RE = re.compile(r"<think[^>]*>.*?</think>", re.DOTALL)

# ── Step 1 System Prompt ───────────────────────────────────────────────────
SYSTEM_REVIEW_STEP1 = """\
You are a code review specialist. Your job is to find bugs, security issues,
and edge cases in the provided code diff or file.  Do NOT suggest fixes.
Focus only on: correctness, security, performance, error handling.

Output a JSON object with this exact structure:
{
  "findings": [
    {
      "id": "F01",
      "severity": "critical|high|medium|low",
      "location": "function_name or file:line",
      "category": "bug|security|performance|error_handling|edge_case",
      "description": "What is wrong and why it matters (1-3 sentences)"
    }
  ]
}
If there are no findings, return {"findings": []}.
Never include code modifications or suggested fixes."""

# ── Step 2 System Prompt ───────────────────────────────────────────────────
SYSTEM_REVIEW_STEP2 = """\
You are a code review reflector. You will receive a bug report (list of
findings) and the original code. For each finding, decide:

- ACCEPT: The finding is real and correctly identified.
- REJECT: The finding is false, irrelevant, or already handled.

Output a JSON object:
{
  "verdicts": [
    {"id": "F01", "verdict": "accept", "reason": "1-sentence explanation"},
    {"id": "F02", "verdict": "reject", "reason": "1-sentence explanation"}
  ]
}
Do NOT suggest fixes. Do NOT rewrite code. Respond ONLY with the JSON."""

# ── Step 3 System Prompt (Scoring Judge) ───────────────────────────────
SYSTEM_REVIEW_STEP3 = """\
You are a 3-person jury panel evaluating a code review:
- Juror 1: Security expert — did the finder catch real vulnerabilities?
- Juror 2: Performance expert — did the finder identify real bottlenecks?
- Juror 3: Code quality expert — were the reflector's verdicts accurate?

You evaluate BOTH the Finder (P — who found bugs) AND the Reflector (R — who accepted/rejected them).

Scoring Rubric (DISCRETE INTEGER per criterion, max 10 each):
  0  = Fundamentally wrong / dangerous. Reject outright.
  4  = Has merit but contains significant flaws.
  7  = Mostly correct, minor issues only.
  10 = Production-ready. No issues found.

Finder (P) scored on 3 criteria:
  Correctness: Were the findings real bugs? (0=all false, 10=all verified real)
  Coverage: Did it catch all important issues in the code? (0=missed critical, 10=comprehensive)
  Precision: How many findings were genuine vs noise? (0=mostly noise, 10=all precise)

Reflector (R) scored on 3 criteria:
  Accuracy: Did it correctly accept/reject each finding? (0=mostly wrong, 10=all correct)
  Efficiency: Did it avoid overthinking simple cases? (0=pedantic, 10=efficient)
  Completeness: Did it catch all of P's valid findings? (0=missed real bugs, 10=caught all)

P_score = sum(Finder's 3 criteria) → 0-30
R_score = sum(Reflector's 3 criteria) → 0-30

For DISPUTED findings (Finder says yes, Reflector says no), you cast the deciding vote.
In your decision, list ALL findings with your final approved/rejected verdict.

Produce 2-4 verification_items capturing the most critical checks performed
(input validation, edge case handling, security boundary, performance bottleneck).

Set action: commit=fully approved and diff can be auto-generated,
revert=rejected with no salvage, escalate=needs human review
(hallucination suspected or edge case unclear).

Output STRICT JSON (no markdown, no explanation outside JSON):
{
  "P_score": 0-30,
  "R_score": 0-30,
  "rubric_evaluation": {
    "finder": {"correctness": 0-10, "coverage": 0-10, "precision": 0-10},
    "reflector": {"accuracy": 0-10, "efficiency": 0-10, "completeness": 0-10}
  },
  "decision": "APPROVED|REJECT",
  "action": "commit|revert|escalate",
  "winner": "finder|reflector|tie",
  "hallucination_flag": true|false,
  "verification_items": [
    {"check": "description of check", "result": "pass|fail|partial", "detail": "explanation"}
  ],
  "jury_opinions": {"security": "...", "performance": "...", "quality": "..."},
  "consensus_score": 0-100,
  "disagreement_analysis": "1-sentence summary of key unresolved issues",
  "machine_summary": {
    "decision": "APPROVED|REJECT",
    "scores": {"P": 0-30, "R": 0-30},
    "gap": 0-30,
    "veto_triggered": true|false,
    "critical_findings": ["..."],
    "verification_focus": "what to check if manual review needed",
    "next_state": "diff_generation|manual_review"
  },
  "approved": ["F01", "F03"],
  "rejected": ["F02"],
  "decisions": [
    {"id": "F01", "decision": "approved", "reason": "Both agreed — real bug"},
    {"id": "F02", "decision": "rejected", "reason": "Deciding vote: false positive"}
  ]
}"""

# ── Step 4 System Prompt ───────────────────────────────────────────────────
SYSTEM_REVIEW_STEP4 = """\
You are a diff writer. You receive original code and a list of approved
findings. Generate ONLY a unified diff that fixes the approved issues.

CRITICAL RULES:
- Output ONLY a unified diff (diff --git format).
- Fix ONLY the approved findings. No other changes.
- No style/formatting/whitespace-only changes.
- No explanatory text before or after the diff.
- If a finding cannot be fixed, skip it silently."""


# ── HTTP helpers ───────────────────────────────────────────────────────────
def _poll_health(port: int, timeout: int = 240) -> bool:
    """Wait for llama.cpp server on :port/health to respond 200."""
    url = f"http://127.0.0.1:{port}/health"
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    elapsed = time.monotonic() - start
                    print(f"  [health] :{port} ready after {elapsed:.0f}s")
                    return True
        except Exception:
            pass
        time.sleep(2)
    print(f"  [health] :{port} TIMEOUT after {timeout}s")
    return False


def _parse_json_output(raw: str, label: str = "LLM") -> Dict[str, Any]:
    """Extract JSON from LLM output, handling markdown fences and think blocks."""
    cleaned = THINK_STRIP_RE.sub("", raw).strip()
    # Try to extract from ```json ... ``` fence
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if m:
        cleaned = m.group(1)
    # Fallback: find first { ... } block
    if not cleaned.startswith("{"):
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            cleaned = m.group(0)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Last resort: json_repair if available
    try:
        from lib.llm.json_parser import json_repair

        return json_repair(cleaned)
    except Exception:
        raise RuntimeError(f"Failed to parse {label} JSON output: {raw[:500]}")


# ── Utilities ──────────────────────────────────────────────────────────────
def strip_think_blocks(text: str) -> str:
    """Remove <think>...</think> blocks from R1 output."""
    return THINK_STRIP_RE.sub("", text).strip()


# ── Scoring Judge (delegates to lib/scoring.py) ──────────────────
# Shared single source of truth for veto / gap / early-exit / runtime_metrics.
# Used by both cooperative debate and Dawn review pipeline.
def run_deep_review(code: str, task_label: str = "") -> Dict[str, Any]:
    """Step 1: R1-8B on :8083 performs deep bug/security review."""
    print(f"[step1] Deep Review — R1-8B on :{R1_PORT}")
    if not _poll_health(R1_PORT, timeout=30):
        raise RuntimeError(f"R1-8B not healthy on :{R1_PORT}")

    user_prompt = (
        f"Review this code for bugs, security issues, and edge cases.\n"
        f"Task: {task_label}\n\n```\n{code}\n```"
    )
    raw = call_llm_json(
        [{"role": "system", "content": SYSTEM_REVIEW_STEP1},
         {"role": "user", "content": user_prompt}],
        model="R1-8B",
        max_tokens=MAX_TOKENS_STEP1,
        timeout=TIMEOUT_STEP1,
    )
    cleaned = strip_think_blocks(raw)
    result = _parse_json_output(cleaned, "Step 1 (R1-8B)")
    findings = result.get("findings", [])
    print(f"[step1] Found {len(findings)} issues")
    for f in findings:
        print(f"  {f.get('id', '?')} [{f.get('severity', '?')}] {f.get('description', '')[:80]}")
    return result


def run_reflection(code: str, findings: List[Dict]) -> Dict[str, Any]:
    """Step 2: Qwen7B on :8080 reflects on findings (ACCEPT/REJECT)."""
    print(f"[step2] Reflection — Qwen7B on :{QWEN7B_PORT}")
    if not _poll_health(QWEN7B_PORT, timeout=30):
        raise RuntimeError(f"Qwen7B not healthy on :{QWEN7B_PORT}")

    findings_json = json.dumps({"findings": findings}, ensure_ascii=False, indent=2)
    user_prompt = (
        f"For each finding below, decide ACCEPT or REJECT.\n\n"
        f"Code:\n```\n{code}\n```\n\n"
        f"Findings:\n{findings_json}"
    )
    raw = call_llm_json(
        [{"role": "system", "content": SYSTEM_REVIEW_STEP2},
         {"role": "user", "content": user_prompt}],
        model="Qwen7B",
        max_tokens=MAX_TOKENS_STEP2,
        timeout=TIMEOUT_STEP2,
    )
    result = _parse_json_output(raw, "Step 2 (Qwen7B)")
    verdicts = result.get("verdicts", [])
    accepted = sum(1 for v in verdicts if v.get("verdict", "").lower() == "accept")
    rejected = len(verdicts) - accepted
    print(f"[step2] Verdicts: {accepted} accept, {rejected} reject")
    return result


def run_judgment(
    code: str, findings: List[Dict], reviewer_accepted: List[str], reflector_verdicts: List[Dict]
) -> Dict[str, Any]:
    """Step 3: Selene on :8081 — Scoring Judge (P_score/R_score/gap/veto).

    Returns enriched verdict with judge metadata for downstream gating.
    """
    print(f"[step3] Judgment (Scoring Judge) — Selene on :{SELENE_PORT}")
    if not _poll_health(SELENE_PORT, timeout=30):
        raise RuntimeError(f"Selene not healthy on :{SELENE_PORT}")

    # Determine 2:0 fast-path (both agree) vs 1:1 disputed
    refl_accept = {v["id"] for v in reflector_verdicts if v.get("verdict", "").lower() == "accept"}
    fast_path = [fid for fid in reviewer_accepted if fid in refl_accept]
    disputed = [f["id"] for f in findings if f["id"] not in fast_path]

    if not disputed:
        # All findings 2:0 agreed — fast-path ratify
        # J still provides scoring for observability
        print(f"[step3] All {len(fast_path)} findings: 2:0 fast-path ratified")
        return _build_fastpath_verdict(findings, fast_path, reflector_verdicts)

    print(f"[step3] {len(fast_path)} fast-path, {len(disputed)} disputed → Selene scoring judge")
    disputed_findings = [f for f in findings if f["id"] in disputed]
    user_prompt = (
        f"Code:\n```\n{code}\n```\n\n"
        f"Finder (P) found {len(findings)} issues: {json.dumps(findings, ensure_ascii=False, indent=2)}\n\n"
        f"Reflector (R) verdicts: {json.dumps(reflector_verdicts, ensure_ascii=False, indent=2)}\n\n"
        f"Already agreed (2:0 fast-path): {json.dumps(fast_path)}\n"
        f"Disputed (you must score both sides + decide):\n"
        f"{json.dumps(disputed_findings, ensure_ascii=False, indent=2)}"
    )
    raw = call_llm_json(
        [{"role": "system", "content": SYSTEM_REVIEW_STEP3},
         {"role": "user", "content": user_prompt}],
        model="Selene",
        max_tokens=MAX_TOKENS_STEP3,
        timeout=TIMEOUT_STEP3,
    )
    result = _parse_json_output(raw, "Step 3 (Selene)")

    # Merge fast-path into judge result
    for fid in fast_path:
        if fid not in result.get("approved", []):
            result.setdefault("approved", []).append(fid)
        result.setdefault("decisions", []).append(
            {"id": fid, "decision": "approved", "reason": "2:0 fast-path"}
        )

    # ── server-side veto + gap + runtime_metrics ───────────────────
    # Guard: if LLM didn't return P_score/R_score (old format), skip scoring
    has_scores = "P_score" in result and "R_score" in result
    if not has_scores:
        print("  [judge] LLM returned old-format verdict (no P_score/R_score) — skipping scoring")
        result["P_score"] = -1
        result["R_score"] = -1
        result.setdefault("machine_summary", {})["next_state"] = "diff_generation"
        result["runtime_metrics"] = {
            "applied_gap_threshold": _sc.THRESHOLDS[1],
            "actual_gap": -1,
            "next_state": "diff_generation",
            "is_veto": False,
        }
        approved = result.get("approved", [])
        rejected = result.get("rejected", [])
        print(f"[step3] Final: {len(approved)} approved, {len(rejected)} rejected | "
              f"legacy format (no scoring)")
        return result

    is_veto = _sc.check_veto(result)
    p_score = result.get("P_score", 0)
    r_score = result.get("R_score", 0)
    gap = abs(p_score - r_score)
    next_state = _sc.next_state_single_pass(result)
    runtime_metrics = _sc.inject_runtime_metrics(result, round_num=1)

    print(f"  [judge] P_score={p_score} R_score={r_score} gap={gap} "
          f"next_state={next_state}" + (" VETO" if is_veto else ""))

    # Enrich machine_summary with server-computed values
    ms = result.setdefault("machine_summary", {})
    ms["gap"] = gap
    ms["veto_triggered"] = is_veto
    ms["scores"] = {"P": p_score, "R": r_score}
    if not ms.get("next_state"):
        ms["next_state"] = next_state

    result["runtime_metrics"] = runtime_metrics

    approved = result.get("approved", [])
    rejected = result.get("rejected", [])
    print(f"[step3] Final: {len(approved)} approved, {len(rejected)} rejected | "
          f"next_state={next_state}")
    return result


def _build_fastpath_verdict(
    findings: List[Dict], fast_path: List[str], reflector_verdicts: List[Dict]
) -> Dict[str, Any]:
    """All findings agreed 2:0 — skip LLM call, synthesize verdict directly."""
    decisions = []
    refl_accept = {v["id"] for v in reflector_verdicts if v.get("verdict", "").lower() == "accept"}
    refl_reject = {v["id"] for v in reflector_verdicts if v.get("verdict", "").lower() == "reject"}
    for f in findings:
        fid = f["id"]
        if fid in fast_path:
            decisions.append({"id": fid, "decision": "approved", "reason": "Both agreed"})
        elif fid not in refl_accept and fid not in refl_reject:
            # P found it, R didn't mention it → treat as disputed (shouldn't happen in fast-path)
            decisions.append({"id": fid, "decision": "approved", "reason": "R silent, default accept"})
        else:
            decisions.append({"id": fid, "decision": "rejected", "reason": "Both rejected"})

    approved = fast_path
    rejected = [f["id"] for f in findings if f["id"] not in fast_path]

    # 2:0 fast-path → high confidence scores
    p_score = 20  # P found real issues
    r_score = 22  # R accurately accepted them
    gap = abs(p_score - r_score)  # should be small (2)

    return {
        "approved": approved,
        "rejected": rejected,
        "decisions": decisions,
        # scoring
        "P_score": p_score,
        "R_score": r_score,
        "rubric_evaluation": {
            "finder": {"correctness": 7, "coverage": 7, "precision": 6},
            "reflector": {"accuracy": 8, "efficiency": 7, "completeness": 7},
        },
        "decision": "APPROVED",
        "action": "commit",
        "winner": "tie",
        "hallucination_flag": False,
        "verification_items": [
            {"check": "All findings 2:0 agreed", "result": "pass", "detail": "No disputes — fast-path consensus"},
        ],
        "jury_opinions": {
            "security": "No disputed findings — fast-path consensus",
            "performance": "No disputed findings — fast-path consensus",
            "quality": "No disputed findings — fast-path consensus",
        },
        "consensus_score": 90,
        "disagreement_analysis": "No disputes — all findings ratified 2:0",
        "machine_summary": {
            "decision": "APPROVED",
            "scores": {"P": p_score, "R": r_score},
            "gap": gap,
            "veto_triggered": False,
            "critical_findings": [],
            "verification_focus": "none — fast-path consensus",
            "next_state": "diff_generation",
        },
        "runtime_metrics": {
            "applied_gap_threshold": _sc.THRESHOLDS[1],
            "actual_gap": gap,
            "next_state": "diff_generation",
            "is_veto": False,
        },
    }


def run_diff(code: str, approved_findings: List[Dict]) -> str:
    """Step 4: Qwen7B on :8080 generates unified diff for approved findings."""
    if not approved_findings:
        print("[step4] No approved findings — skipping diff generation")
        return ""

    print(f"[step4] Diff Generation — Qwen7B on :{QWEN7B_PORT} ({len(approved_findings)} findings)")
    if not _poll_health(QWEN7B_PORT, timeout=30):
        raise RuntimeError(f"Qwen7B not healthy on :{QWEN7B_PORT}")

    user_prompt = (
        f"Generate a unified diff to fix these approved findings:\n\n"
        f"Original code:\n```\n{code}\n```\n\n"
        f"Approved findings to fix:\n"
        f"{json.dumps(approved_findings, ensure_ascii=False, indent=2)}\n\n"
        f"Output ONLY the unified diff (diff --git format). No explanation."
    )
    diff_text = call_llm(
        [{"role": "system", "content": SYSTEM_REVIEW_STEP4},
         {"role": "user", "content": user_prompt}],
        model="Qwen7B",
        max_tokens=MAX_TOKENS_STEP4,
        timeout=TIMEOUT_STEP4,
    )
    # Sanitize: strip any markdown fences
    diff_text = diff_text.strip()
    if diff_text.startswith("```"):
        diff_text = re.sub(r"^```(?:diff)?\s*\n?", "", diff_text)
        diff_text = re.sub(r"\n?```\s*$", "", diff_text)
    print(f"[step4] Diff generated ({len(diff_text)} chars)")
    return diff_text
