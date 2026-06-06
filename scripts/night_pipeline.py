#!/usr/bin/env python3
# Status: production
# Path: nightly_batch.sh
"""
DevForge Night Pipeline v2.0 — Single-phase P→R→J→27B→32B review/verify.

Architecture (model-loaded-at-switch-time):
  Phase 1  Python verify         No LLM — structural audit of eval data files
  Phase 2  Pod B review-qw       Qwen7B(:8080) — R(refuter) role + all model interaction
  Phase 3  Pod B review-se       Selene(:8081) — J(judge) role
  Phase 4  Pod B verify          27B(:8081) — production final verify
  Phase 5  Pod B verify_test     32B(:8081) — experimental parallel verify
  Phase 6  Feedback              Consolidate all results

Usage:
  # Run full pipeline:
  python3 night_pipeline.py --all

  # Single phase (after mode already switched):
  python3 night_pipeline.py --phase 2
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
EVAL_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "eval")
os.makedirs(EVAL_DIR, exist_ok=True)

sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql_ok
from lib.llm_client import call_llm

# ── Ports ────────────────────────────────────────────────────────────────
QWEN7B_PORT = 8080
SELENE_PORT = 8081
VERIFY_PORT = 8081
VERIFY_TEST_PORT = 8081
POD_A_REVIEW_PORT = 8083  # R1-8B night mode

# ── Mode files ───────────────────────────────────────────────────────────
MODE_FILE_A = "/opt/ai_data/scripts/current-mode-pod-a.env"
MODE_FILE_B = "/opt/ai_data/scripts/current-mode-pod-b.env"

# ── Timeouts ─────────────────────────────────────────────────────────────
TIMEOUT_LLM = 600
TIMEOUT_SWAP = 300

# ── System prompts ───────────────────────────────────────────────────────

SYSTEM_REFUTER = """You are a review reflector. Given a set of findings and the original data, decide for each finding:
- ACCEPT: the finding is real and correctly identified
- REJECT: the finding is false, irrelevant, or already handled

Output STRICT JSON:
{
  "verdicts": [
    {"id": "F01", "verdict": "accept", "reason": "1-sentence explanation"},
    {"id": "F02", "verdict": "reject", "reason": "1-sentence explanation"}
  ]
}"""

SYSTEM_JUDGE = """You are a Scoring Judge evaluating a review pipeline. You evaluate BOTH the Finder (P)
and the Reflector (R).

Scoring Rubric (DISCRETE INTEGER per criterion, max 10 each):
  0  = Fundamentally wrong / dangerous
  4  = Has merit but significant flaws
  7  = Mostly correct, minor issues only
  10 = Production-ready

Finder (P) scored on 3 criteria:
  Correctness: Were findings real? (0=all false, 10=all verified real)
  Coverage: Did it catch all important issues? (0=missed critical, 10=comprehensive)
  Precision: Genuine vs noise ratio? (0=mostly noise, 10=all precise)

Reflector (R) scored on 3 criteria:
  Accuracy: Accept/reject correctness? (0=mostly wrong, 10=all correct)
  Efficiency: Avoid overthinking? (0=pedantic, 10=efficient)
  Completeness: Catch all valid findings? (0=missed real bugs, 10=caught all)

P_score = sum(Correctness+Coverage+Precision) → 0-30
R_score = sum(Accuracy+Efficiency+Completeness) → 0-30

Output STRICT JSON:
{
  "P_score": 0-30,
  "R_score": 0-30,
  "rubric_evaluation": {
    "finder": {"correctness": 0-10, "coverage": 0-10, "precision": 0-10},
    "reflector": {"accuracy": 0-10, "efficiency": 0-10, "completeness": 0-10}
  },
  "decision": "APPROVED|REJECT",
  "action": "commit|revert|escalate",
  "hallucination_flag": true|false,
  "verification_items": [{"check": "...", "result": "pass|fail|partial", "detail": "..."}],
  "approved": ["F01"],
  "rejected": ["F02"],
  "decisions": [{"id": "F01", "decision": "approved", "reason": "..."}],
  "consensus_score": 0-100,
  "disagreement_analysis": "1-sentence summary"
}"""

SYSTEM_VERIFY = """You are a final verification specialist. Review ALL findings across all evaluation
documents. Decide for each finding:
- approved: correct, can be committed
- rejected: incorrect or not actionable
- escalate: requires human review

Also evaluate the overall evaluation quality.

Output STRICT JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "action": "commit|revert|escalate",
  "confidence": 0-100,
  "summary": "1-sentence final decision",
  "reasoning": "step-by-step analysis (3-5 sentences)",
  "verification_items": [
    {"check": "...", "result": "pass|fail|partial", "detail": "..."}
  ],
  "feedback": {
    "proposer_improvement": "...",
    "refuter_improvement": "...",
    "judge_improvement": "..."
  }
}"""

SYSTEM_VERIFY_32B = """You are an independent verification specialist providing a second opinion.
Review the evaluation results and identify gaps the primary verifier may have missed.

Output STRICT JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "action": "commit|revert|escalate",
  "confidence": 0-100,
  "summary": "1-sentence final decision",
  "reasoning": "independent analysis (3-5 sentences)",
  "verification_items": [
    {"check": "...", "result": "pass|fail|partial", "detail": "..."}
  ],
  "disagreement_with_27b": [
    {"issue": "...", "27b_verdict": "...", "32b_verdict": "...", "detail": "..."}
  ]
}"""


# ── Helpers ──────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_eval_data() -> Dict[str, Any]:
    """Load ALL eval_*.json files into a single consolidated dict."""
    consolidated = {"files": {}, "meta": {"total_files": 0, "total_findings": 0}}
    all_findings = []

    for fname in sorted(os.listdir(EVAL_DIR)):
        if not fname.endswith(".json") or not fname.startswith("eval_"):
            continue
        fpath = os.path.join(EVAL_DIR, fname)
        try:
            with open(fpath) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            continue

        # Normalize: extract findings from various structures
        findings = data.get("findings", [])
        if not findings and data.get("verification_items"):
            findings = data["verification_items"]
        if not findings and data.get("verification_result", {}).get("verification_items"):
            findings = data["verification_result"]["verification_items"]
        if not findings:
            findings = data.get("verdicts", [])
        if not findings and data.get("summary"):
            findings = [{"id": "SUMMARY", "description": data["summary"][:200]}]

        entry = {
            "filename": fname,
            "role": data.get("role", data.get("evaluation_type", "unknown")),
            "findings": findings,
            "summary": data.get("summary", ""),
            "verdict": data.get("verification_result", {}).get("final_verdict", ""),
            "confidence": data.get("verification_result", {}).get("confidence", 0),
        }
        consolidated["files"][fname] = entry
        consolidated["meta"]["total_files"] += 1
        consolidated["meta"]["total_findings"] += len(findings)
        all_findings.extend(findings)

    consolidated["all_findings"] = all_findings
    return consolidated


def build_consolidated_report(all_data: Dict[str, Any]) -> str:
    """Build a prompt containing all findings from all eval files."""
    parts = ["# Consolidated Evaluation Report — All Findings\n"]
    for fname, entry in all_data["files"].items():
        parts.append(f"\n## {fname} ({entry['role']})")
        parts.append(f"Summary: {entry['summary'][:300]}")
        for f in entry["findings"][:10]:  # max 10 per file
            fid = f.get("id", f.get("check", "?"))
            desc = f.get("description", f.get("detail", str(f)[:200]))
            sev = f.get("severity", f.get("result", ""))
            parts.append(f"  [{sev}] {fid}: {desc[:200]}")
    return "\n".join(parts)


def swap_pod_b(mode: str, timeout: int = TIMEOUT_SWAP) -> bool:
    """Switch Pod B to the given mode and wait for health."""
    log(f"  [swap] Pod B → {mode}")
    with open(MODE_FILE_B, "w") as f:
        f.write(f"MODE={mode}")
    r = subprocess.run(
        ["systemctl", "--user", "restart", "container-devforge-swap.service"],
        capture_output=True, timeout=60,
    )
    if r.returncode != 0:
        log(f"  [swap] restart failed: {r.stderr.decode()[:200]}")
        return False
    # Wait for health
    port = 8080 if mode == "review-qw" else 8081
    url = f"http://127.0.0.1:{port}/health"
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    log(f"  [swap] :{port} ready after {time.monotonic()-t0:.0f}s")
                    return True
        except Exception:
            pass
        time.sleep(5)
    log(f"  [swap] :{port} TIMEOUT after {timeout}s")
    return False


def swap_pod_a(mode: str, timeout: int = TIMEOUT_SWAP) -> bool:
    """Switch Pod A to the given mode."""
    log(f"  [swap] Pod A → {mode}")
    with open(MODE_FILE_A, "w") as f:
        f.write(f"MODE={mode}")
    r = subprocess.run(
        ["systemctl", "--user", "restart", "container-devforge-pod-a.service"],
        capture_output=True, timeout=60,
    )
    if r.returncode != 0:
        log(f"  [swap] restart failed: {r.stderr.decode()[:200]}")
        return False
    url = f"http://127.0.0.1:{POD_A_REVIEW_PORT}/health"
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    log(f"  [swap] Pod A :{POD_A_REVIEW_PORT} ready after {time.monotonic()-t0:.0f}s")
                    return True
        except Exception:
            pass
        time.sleep(5)
    log(f"  [swap] Pod A TIMEOUT after {timeout}s")
    return False


def call_model(
    messages: List[Dict],
    model: str,
    max_tokens: int = 1024,
    timeout: int = TIMEOUT_LLM,
    json_mode: bool = True,
    repr: str = "model",
) -> Optional[Dict]:
    """Call an LLM and return JSON result with metadata."""
    try:
        result = call_llm(
            messages,
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
            json_mode=json_mode,
            return_meta=True,
        )
        content = result["content"]
        parsed = json.loads(content) if isinstance(content, str) else content
        return {
            "result": parsed,
            "usage": result.get("usage", {}),
            "timings": result.get("timings", {}),
            "elapsed_ms": result.get("elapsed_ms", 0),
            "model": result.get("model", model),
        }
    except Exception as e:
        log(f"  [error] {repr} call failed: {e}")
        return None


def save_output(phase: str, data: Any) -> str:
    """Save phase result to eval directory."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = f"pipeline_{phase}_{ts}.json"
    fpath = os.path.join(EVAL_DIR, fname)
    with open(fpath, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"  [save] {fpath}")
    return fpath


# ── Phase implementations ────────────────────────────────────────────────

def phase_1_python_verify(all_data: Dict) -> Dict:
    """Phase 1: Python structural audit. No LLM needed."""
    log("\n=== Phase 1: Python Verify ===")
    result = {
        "phase": 1,
        "role": "python_verify",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "files_loaded": all_data["meta"]["total_files"],
        "total_findings": all_data["meta"]["total_findings"],
        "findings": [],
    }

    for fname, entry in all_data["files"].items():
        result["findings"].append({
            "file": fname,
            "role": entry["role"],
            "finding_count": len(entry["findings"]),
            "has_verdict": bool(entry["verdict"]),
        })
        if entry.get("confidence"):
            result["findings"][-1]["confidence"] = entry["confidence"]

    save_output("01_python_verify", result)
    return result


def phase_2_refuter(all_data: Dict) -> Dict:
    """Phase 2: Pod B review-qw — Qwen7B(:8080) plays R role."""
    log("\n=== Phase 2: Refuter (Qwen7B :8080) ===")
    log("  [mode] Pod B → review-qw")

    if not swap_pod_b("review-qw"):
        log("  [error] Pod B review-qw failed to load")
        return {"phase": 2, "error": "Pod B swap failed"}

    consolidated_text = build_consolidated_report(all_data)

    log("  [llm] Calling Qwen7B for refuter role...")
    messages = [
        {"role": "system", "content": SYSTEM_REFUTER},
        {"role": "user", "content": consolidated_text},
    ]
    response = call_model(messages, "Qwen7B", repr="Qwen7B(refuter)")
    if not response:
        return {"phase": 2, "error": "LLM call failed"}

    result = {
        "phase": 2,
        "role": "refuter_qwen7b",
        "model": "Qwen2.5-Coder-7B-Instruct",
        "port": QWEN7B_PORT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "usage": response["usage"],
        "timings": response["timings"],
        "elapsed_ms": response["elapsed_ms"],
        "result": response["result"],
    }

    save_output("02_refuter", result)
    return result


def phase_3_judge(all_data: Dict, refuter_result: Dict) -> Dict:
    """Phase 3: Pod B review-se — Selene(:8081) plays J role."""
    log("\n=== Phase 3: Judge (Selene :8081) ===")
    log("  [mode] Pod B → review-se")

    if not swap_pod_b("review-se"):
        log("  [error] Pod B review-se failed to load")
        return {"phase": 3, "error": "Pod B swap failed"}

    # Build input: findings + refuter verdicts
    all_findings = all_data.get("all_findings", [])
    refuter_verdicts = refuter_result.get("result", {}).get("verdicts", [])
    consolidated_text = build_consolidated_report(all_data)

    messages = [
        {"role": "system", "content": SYSTEM_JUDGE},
        {"role": "user", "content":
            f"## Findings\n{json.dumps(all_findings[:30], ensure_ascii=False, indent=2)}\n\n"
            f"## Refuter Verdicts\n{json.dumps(refuter_verdicts, ensure_ascii=False, indent=2)}\n\n"
            f"## Full Context\n{consolidated_text[:2000]}"},
    ]

    log("  [llm] Calling Selene for judge role...")
    response = call_model(messages, "Selene", max_tokens=2048, repr="Selene(judge)")
    if not response:
        return {"phase": 3, "error": "LLM call failed"}

    result = {
        "phase": 3,
        "role": "judge_selene",
        "model": "selene-1-mini-llama-3.1-8b",
        "port": SELENE_PORT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "usage": response["usage"],
        "timings": response["timings"],
        "elapsed_ms": response["elapsed_ms"],
        "result": response["result"],
    }

    save_output("03_judge", result)
    return result


def phase_4_verify_27b(all_data: Dict, judge_result: Dict) -> Dict:
    """Phase 4: Pod B verify — 27B(:8081) production final verify."""
    log("\n=== Phase 4: 27B Verify (:8081) ===")
    log("  [mode] Pod B → verify")

    if not swap_pod_b("verify"):
        log("  [error] Pod B verify failed to load")
        return {"phase": 4, "error": "Pod B swap failed"}

    judge_verdict = judge_result.get("result", {})
    consolidated_text = build_consolidated_report(all_data)

    messages = [
        {"role": "system", "content": SYSTEM_VERIFY},
        {"role": "user", "content":
            f"## Judge Verdict\n{json.dumps(judge_verdict, ensure_ascii=False, indent=2)[:2000]}\n\n"
            f"## Full Evaluation Data\n{consolidated_text[:3000]}"},
    ]

    log("  [llm] Calling 27B for production verify...")
    response = call_model(messages, "Qwen27B", max_tokens=2048, repr="27B(verify)")
    if not response:
        return {"phase": 4, "error": "LLM call failed"}

    result = {
        "phase": 4,
        "role": "verify_27b",
        "model": "Qwen3.6-27B",
        "port": VERIFY_PORT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "usage": response["usage"],
        "timings": response["timings"],
        "elapsed_ms": response["elapsed_ms"],
        "result": response["result"],
    }

    save_output("04_verify_27b", result)
    return result


def phase_5_verify_32b(all_data: Dict, verify_27b_result: Dict) -> Dict:
    """Phase 5: Pod B verify_test — 32B(:8081) experimental parallel verify."""
    log("\n=== Phase 5: 32B Verify (:8081) ===")
    log("  [mode] Pod B → verify_test")

    if not swap_pod_b("verify_test"):
        log("  [error] Pod B verify_test failed to load")
        return {"phase": 5, "error": "Pod B swap failed"}

    v27b = verify_27b_result.get("result", {})
    consolidated_text = build_consolidated_report(all_data)

    messages = [
        {"role": "system", "content": SYSTEM_VERIFY_32B},
        {"role": "user", "content":
            f"## 27B Verdict\n{json.dumps(v27b, ensure_ascii=False, indent=2)[:2000]}\n\n"
            f"## Full Evaluation Data\n{consolidated_text[:3000]}"},
    ]

    log("  [llm] Calling 32B for experimental verify...")
    response = call_model(messages, "Qwen32B", max_tokens=2048, repr="32B(verify)")
    if not response:
        return {"phase": 5, "error": "LLM call failed"}

    result = {
        "phase": 5,
        "role": "verify_32b",
        "model": "Qwen2.5-Coder-32B-Instruct",
        "port": VERIFY_TEST_PORT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "usage": response["usage"],
        "timings": response["timings"],
        "elapsed_ms": response["elapsed_ms"],
        "result": response["result"],
    }

    save_output("05_verify_32b", result)
    return result


def phase_6_feedback(all_data: Dict, results: Dict) -> Dict:
    """Phase 6: Consolidate all results into final feedback."""
    log("\n=== Phase 6: Consolidated Feedback ===")

    total_phases = sum(1 for v in results.values() if v and "error" not in v)
    total_errors = sum(1 for v in results.values() if v and "error" in v)
    total_elapsed = sum(v.get("elapsed_ms", 0) for v in results.values() if v and "elapsed_ms" in v)
    total_prompt_tok = sum(v.get("usage", {}).get("prompt_tokens", 0) for v in results.values() if v)
    total_gen_tok = sum(v.get("usage", {}).get("completion_tokens", 0) for v in results.values() if v)

    result = {
        "phase": 6,
        "role": "consolidated_feedback",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "execution_summary": {
            "total_phases_completed": total_phases,
            "total_errors": total_errors,
            "total_elapsed_ms": total_elapsed,
            "total_prompt_tokens": total_prompt_tok,
            "total_gen_tokens": total_gen_tok,
            "total_tokens": total_prompt_tok + total_gen_tok,
        },
        "phase_results": {},
    }
    for phase_key, data in results.items():
        if data and "error" not in data:
            result["phase_results"][phase_key] = {
                "role": data.get("role", ""),
                "model": data.get("model", ""),
                "elapsed_ms": data.get("elapsed_ms", 0),
                "usage": data.get("usage", {}),
                "result_preview": str(data.get("result", {}))[:300],
            }

    result["rubric_v3_recommendation"] = {
        "note": "Based on pipeline execution, update rubric with operational_impact criterion",
        "criteria": [
            {"id": "faithfulness", "weight": 0.25},
            {"id": "coverage", "weight": 0.20},
            {"id": "schema_compliance", "weight": 0.15},
            {"id": "conciseness", "weight": 0.15},
            {"id": "cost_efficiency", "weight": 0.15},
            {"id": "operational_impact", "weight": 0.10},
        ],
    }

    save_output("06_feedback", result)
    return result


# ── Orchestrator ──────────────────────────────────────────────────────────

def run_pipeline(phases: Optional[List[int]] = None) -> Dict[str, Any]:
    """Run the full pipeline or selected phases."""
    t_start = time.monotonic()
    log("=" * 60)
    log("DevForge Night Pipeline v2.0")
    log("=" * 60)

    # Phase 0: Load all eval data
    log("Loading eval data files...")
    all_data = load_eval_data()
    log(f"  Loaded {all_data['meta']['total_files']} files, "
        f"{all_data['meta']['total_findings']} total findings")

    if phases is None:
        phases = [1, 2, 3, 4, 5, 6]

    results: Dict[str, Any] = {}

    if 1 in phases:
        results["phase1"] = phase_1_python_verify(all_data)

    if 2 in phases:
        results["phase2"] = phase_2_refuter(all_data)

    if 3 in phases:
        refuter = results.get("phase2", {})
        results["phase3"] = phase_3_judge(all_data, refuter)

    if 4 in phases:
        judge = results.get("phase3", {})
        results["phase4"] = phase_4_verify_27b(all_data, judge)

    if 5 in phases:
        v27b = results.get("phase4", {})
        results["phase5"] = phase_5_verify_32b(all_data, v27b)

    if 6 in phases:
        results["phase6"] = phase_6_feedback(all_data, results)

    total = round(time.monotonic() - t_start, 1)
    log("\n" + "=" * 60)
    log(f"Pipeline complete in {total}s")
    phases_run = [p for p in phases if p <= len(results) or p == 6]
    log(f"  Phases: {len(results)} completed in {total}s")
    save_output("complete", {"total_elapsed_s": total, "phases_run": phases, "results": results})

    return results


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="DevForge Night Pipeline v2.0")
    ap.add_argument("--all", action="store_true", help="Run full pipeline")
    ap.add_argument("--phase", type=int, choices=[1, 2, 3, 4, 5, 6],
                    help="Run a single phase (mode must already be set)")
    args = ap.parse_args()

    if args.phase:
        run_pipeline(phases=[args.phase])
    else:
        run_pipeline()

    sys.exit(0)


if __name__ == "__main__":
    main()
