#!/usr/bin/env python3
"""3-Model Review Pipeline — Nightly Code Review (llama.cpp).

Adversarial 3-stage (P→R→J) pipeline with Scoring Judge:

  Step 1  Deep Review     Pod A R1-8B (:8083)   Bug/security/edge-case discovery
  Step 2  Reflection      Pod B Qwen7B (:8080)  ACCEPT/REJECT per finding
  Step 3  Judgment        Pod B Selene (:8081)  Scoring Judge — P_score/R_score/gap/veto
  Step 4  Diff Gen        Pod B Qwen7B (:8080)  Unified diff (gap≤10 → auto; gap>10 → skip)

Judge gating:
  gap ≤ 5  → diff_generation (high confidence consensus)
  5 < gap ≤ 10 → diff_generation (within threshold)
  gap > 10 → manual_review (skip diff, flag for human)
  P_score==0 | R_score==0 | decision=="REJECT" → veto → skip diff

Model assignment (P→R→J pipeline):
  Step 1 (Deep Review)   Pod A :8083  R1-8B
  Step 2 (Reflection)    Pod B :8080  Qwen7B
  Step 3 (Judgment)      Pod B :8081  Selene (swap)
  Step 4 (Diff Gen)      Pod B :8080  Qwen7B

Usage:
  python3 review_pipeline_3model.py --task-id T01       # single task
  python3 review_pipeline_3model.py --all                # all unprocessed
  python3 review_pipeline_3model.py --step 1 --data ...  # single step
  python3 review_pipeline_3model.py --orchestrate         # full pipeline
"""

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Dict

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

from review_pipeline_steps import (  # noqa: E402
    QWEN7B_PORT,
    SELENE_PORT,
    _poll_health,
    run_deep_review,
    run_diff,
    run_judgment,
    run_reflection,
)

# ── Pod switching ──────────────────────────────────────────────────────────
MODE_FILE_B = "/opt/ai_data/scripts/current-mode-pod-b.env"


def _swap_pod_b(mode: str, timeout: int = 300) -> bool:
    """Switch Pod B to the given mode by writing MODE_FILE and restarting."""
    print(f"[orchestrate] Switching Pod B → {mode}")
    tmp = MODE_FILE_B + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"MODE={mode}")
    os.replace(tmp, MODE_FILE_B)
    subprocess.run(
        ["systemctl", "--user", "restart", "container-devforge-swap.service"],
        capture_output=True,
        timeout=30,
    )
    check_port = SELENE_PORT if mode == "review-se" else QWEN7B_PORT
    return _poll_health(check_port, timeout=timeout)


# ── Orchestrator ───────────────────────────────────────────────────────────
def run_full_review(code: str, task_label: str = "", swap_fn=None) -> Dict[str, Any]:
    """Run the complete 4-step P→R→J pipeline. swap_fn(step_name) handles model swaps.

    Returns: {diff, report, confidence, approved_count, total_findings}
    """
    print(f"\n{'=' * 60}")
    print(f"Review Pipeline: {task_label}")
    print(f"{'=' * 60}")

    # Step 1: Deep Review (R1-8B on Pod A :8083)
    step1 = run_deep_review(code, task_label)
    findings = step1.get("findings", [])
    if not findings:
        print("[pipeline] No findings — review complete")
        return {
            "diff": "",
            "report": step1,
            "confidence": 1.0,
            "approved_count": 0,
            "total_findings": 0,
        }

    # Determine reviewer's accepted findings (R1 reports all as findings;
    # we treat all as "reviewer accepted" since R1's role is to FIND issues)
    reviewer_accepted = [f["id"] for f in findings]

    # Step 2: Reflection (Qwen7B on Pod B :8080)
    if swap_fn:
        swap_fn("review-qw")
    step2 = run_reflection(code, findings)
    reflector_verdicts = step2.get("verdicts", [])

    # Step 3: Judgment (Selene on Pod B :8081) — Scoring Judge
    if swap_fn:
        swap_fn("review-se")
    step3 = run_judgment(code, findings, reviewer_accepted, reflector_verdicts)

    # ── gap-based gating ────────────────────────────────────────────
    p_score = step3.get("P_score", 0)
    r_score = step3.get("R_score", 0)
    gap = abs(p_score - r_score)
    is_veto = step3.get("machine_summary", {}).get("veto_triggered", False)
    next_state = step3.get("machine_summary", {}).get("next_state", "diff_generation")
    runtime_metrics = step3.get("runtime_metrics", {})

    print(f"[pipeline] judge verdict: P_score={p_score} R_score={r_score} "
          f"gap={gap} next_state={next_state}" + (" VETO" if is_veto else ""))

    # Step 4: Diff Generation (only if auto-approved)
    diff_text = ""
    approved_ids = set(step3.get("approved", []))
    approved_findings = [f for f in findings if f["id"] in approved_ids]

    if next_state == "diff_generation" and not is_veto and approved_findings:
        if swap_fn:
            swap_fn("review-qw")
        diff_text = run_diff(code, approved_findings)
    elif is_veto:
        print("[pipeline] Veto triggered — skipping diff generation")
    elif next_state == "manual_review":
        print(f"[pipeline] Gap {gap} > threshold — marking for manual review, skipping diff")
    else:
        print("[pipeline] No approved findings — skipping diff generation")

    confidence = len(approved_ids) / len(findings) if findings else 1.0
    result = {
        "diff": diff_text,
        "report": {
            "findings": findings,
            "verdicts": step3.get("decisions", []),
            "step1_raw": step1,
            "step2_raw": step2,
            "step3_raw": step3,
        },
        "confidence": round(confidence, 2),
        "approved_count": len(approved_ids),
        "total_findings": len(findings),
        # scoring metadata
        "scoring": {
            "P_score": p_score,
            "R_score": r_score,
            "gap": gap,
            "is_veto": is_veto,
            "next_state": next_state,
            "runtime_metrics": runtime_metrics,
        },
    }
    print(
        f"\n[pipeline] Done. {result['approved_count']}/{result['total_findings']} "
        f"findings approved (confidence={result['confidence']}, next={next_state})"
    )
    return result


def _run_orchestrated() -> Dict[str, Any]:
    """Full 4-step pipeline with automatic Pod B mode switching between steps."""
    # Load review tasks from activity_log (unprocessed code mod tasks)
    from lib.db import psql

    rows = psql(
        "SELECT id, details->>'code' as code, title "
        "FROM activity_log "
        "WHERE queue_status = 'unprocessed' AND type = 'stage' "
        "ORDER BY created_at DESC LIMIT 10"
    )
    if not rows or not rows.strip():
        print("[orchestrate] No unprocessed tasks found")
        return {"results": [], "message": "No unprocessed tasks"}

    results = []
    for line in rows.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        task_id = parts[0].strip()
        # For now, read code from batch results
        code_file = os.path.join(
            os.path.dirname(SCRIPTS_DIR), "batch_results", f"{task_id}_diff.txt"
        )
        code = ""
        if os.path.exists(code_file):
            with open(code_file) as f:
                code = f.read()
        if not code:
            # Fallback: use title as task description
            title = parts[2].strip() if len(parts) > 2 else task_id
            code = f"// Task: {title}\n// No code file found for {task_id}"

        label = f"Task {task_id}"

        # Step 1: R1-8B deep review (Pod A :8083)
        step1 = run_deep_review(code, label)
        findings = step1.get("findings", [])
        if not findings:
            results.append({"task_id": task_id, "findings": 0, "diff": ""})
            continue

        reviewer_accepted = [f["id"] for f in findings]

        # Step 2: Qwen7B reflection (Pod B :8080, loaded as review-qw)
        step2 = run_reflection(code, findings)
        reflector_verdicts = step2.get("verdicts", [])

        # Step 3: Swap to Selene, then judge (Scoring Judge)
        if not _swap_pod_b("review-se"):
            raise RuntimeError("Failed to load Selene on Pod B")
        step3 = run_judgment(code, findings, reviewer_accepted, reflector_verdicts)

        # ── gap-based gating ────────────────────────────────────────
        p_score = step3.get("P_score", 0)
        r_score = step3.get("R_score", 0)
        gap = abs(p_score - r_score)
        is_veto = step3.get("machine_summary", {}).get("veto_triggered", False)
        next_state = step3.get("machine_summary", {}).get("next_state", "diff_generation")

        # Step 4: Diff generation only if auto-approved
        diff_text = ""
        approved_ids = set(step3.get("approved", []))
        approved_findings = [f for f in findings if f["id"] in approved_ids]

        if next_state == "diff_generation" and not is_veto and approved_findings:
            if not _swap_pod_b("review-qw"):
                raise RuntimeError("Failed to restore Qwen7B on Pod B")
            diff_text = run_diff(code, approved_findings)
        else:
            if is_veto:
                print(f"[orchestrate] {task_id}: VETO — skipping diff generation")
            elif next_state == "manual_review":
                print(f"[orchestrate] {task_id}: gap {gap} > threshold — manual review, skipping diff")
            else:
                print(f"[orchestrate] {task_id}: No approved findings — skipping diff")
            # Restore Pod B to Qwen7B for clean state (next iteration swaps again if needed)
            _swap_pod_b("review-qw")

        confidence = len(approved_ids) / len(findings) if findings else 1.0
        results.append(
            {
                "task_id": task_id,
                "findings": len(findings),
                "approved": len(approved_ids),
                "confidence": round(confidence, 2),
                "diff": diff_text,
                "scoring": {
                    "P_score": p_score,
                    "R_score": r_score,
                    "gap": gap,
                    "is_veto": is_veto,
                    "next_state": next_state,
                },
            }
        )
        print(
            f"[orchestrate] {task_id}: {len(approved_ids)}/{len(findings)} "
            f"approved, {len(diff_text)} chars diff, next={next_state}"
        )

    return {"results": results, "total": len(results)}


# ── CLI ────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="3-Model Code Review Pipeline")
    parser.add_argument("--code", help="Code to review (or file path)")
    parser.add_argument("--code-file", help="Read code from file")
    parser.add_argument("--task-label", default="", help="Task identifier")
    parser.add_argument(
        "--step", type=int, choices=[1, 2, 3, 4], help="Run a single step (requires --data)"
    )
    parser.add_argument("--data", help="JSON data for single-step mode")
    parser.add_argument("--output", "-o", help="Write result JSON to file")
    parser.add_argument(
        "--orchestrate",
        action="store_true",
        help="Full pipeline with automatic Pod B mode switching",
    )
    args = parser.parse_args()

    if args.orchestrate:
        result = _run_orchestrated()
        output = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
        else:
            print(output)
        return

    if args.step:
        # Single-step mode
        data = json.loads(args.data) if args.data else {}
        code = args.code or data.get("code", "")
        if not code and args.code_file:
            with open(args.code_file) as f:
                code = f.read()

        if args.step == 1:
            result = run_deep_review(code, args.task_label)
        elif args.step == 2:
            result = run_reflection(code, data.get("findings", []))
        elif args.step == 3:
            result = run_judgment(
                code,
                data.get("findings", []),
                data.get("reviewer_accepted", []),
                data.get("reflector_verdicts", []),
            )
        elif args.step == 4:
            diff = run_diff(code, data.get("approved_findings", []))
            result = {"diff": diff}
    else:
        # Full pipeline
        code = args.code or ""
        if args.code_file:
            with open(args.code_file) as f:
                code = f.read()
        if not code:
            parser.error("--code or --code-file required for full pipeline")

        # In full-pipeline mode, mode switching is handled by nightly_batch.sh.
        # The model-swap callback writes MODE= to the Pod B mode file and
        # restarts the container.
        def _noop_swap(_step: str) -> None:
            pass

        result = run_full_review(code, args.task_label, swap_fn=_noop_swap)

    # Output
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"[pipeline] Result written to {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()
