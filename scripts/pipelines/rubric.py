#!/usr/bin/env python3
# Status: experimental
# Path: none — library
"""
DevForge Rubric Experiment — Round 1 (no rubric) vs Round 2 (with rubric).

Compares P-R-J-27B pipeline quality with and without explicit evaluation criteria.

Usage:
  # Run full experiment (both rounds):
  python3 scripts/pipelines/rubric.py

  # Single round:
  python3 scripts/pipelines/rubric.py --round 1
  python3 scripts/pipelines/rubric.py --round 2 --best-combo A

  # Quick comparison report:
  python3 scripts/pipelines/rubric.py --compare
"""

import json, os, subprocess, sys, time, glob, argparse, urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.infra.preflight import preflight_checks
from lib.db import psql_ok
from lib.llm_client import call_llm

# ── Constants ────────────────────────────────────────────────────────────
MODE_FILE_B = "/opt/ai_data/scripts/current-mode-pod-b.env"
TIMEOUT_LLM = 600
TIMEOUT_SWAP = 300

# ── P-R-J Model Combos ──────────────────────────────────────────────────
COMBO_B = {
    "name": "B (proposer + reflector + judge)",
    "proposer_model": "proposer",
    "proposer_port": 8080,
    "refuter_model": "reviewer",
    "refuter_mode": "review-r",
    "refuter_port": 8080,
    "judge_model": "judge",
    "judge_mode": "review-j",
    "judge_port": 8081,
}

ALL_COMBOS = [COMBO_B]

# ── Rubric V3 (used in Round 2) ────────────────────────────────────────

REVIEW_RUBRIC = """## Evaluation Rubric (mandatory scoring criteria)

Each role MUST use these criteria when evaluating findings. Score each finding individually.

### P (Proposer) — Finding Quality Scoring
Score each finding 0-10 on:
1. **Correctness** (weight 0.35): Is this a real issue? 10=verified real, 0=false positive
2. **Actionability** (weight 0.30): Can someone act on this? 10=clear fix possible, 0=vague
3. **Evidence** (weight 0.25): Is it backed by data? 10=specific metrics/quotes, 0=speculation
4. **Novelty** (weight 0.10): New insight? 10=not previously known, 0=common knowledge

### R (Refuter) — Verdict Quality Scoring
Score each ACCEPT/REJECT 0-10 on:
1. **Accuracy** (weight 0.40): Verdict correct? 10=perfect, 0=wrong
2. **Reasoning** (weight 0.30): Explanation specific? 10=pinpoints exact issue, 0=vague
3. **Efficiency** (weight 0.30): Concise? 10=1 sentence sufficient, 0=overthinking

### J (Judge) — Scoring Criteria
Score P and R 0-30 each (sum of 3 sub-scores):
- **P score**: Correctness(0-10) + Coverage(0-10) + Precision(0-10)
- **R score**: Accuracy(0-10) + Efficiency(0-10) + Completeness(0-10)

**Gap rules**:
- gap ≤ 3: high consensus, auto-approve
- 3 < gap ≤ 8: moderate — include disagreement_analysis
- gap > 8: flag for escalation

### V (Verify 27B) — Final Verdict Criteria
- confidence ≥ 80: approve
- 60 ≤ confidence < 80: approved_with_conditions (list conditions)
- confidence < 60: reject or escalate

### Cost/Operational Score (new!)
- **prompt_efficiency**: tokens used vs findings processed (aim for <200 tok/finding)
- **model_appropriateness**: is this model right for this task?"""

# ── System prompts without rubric ──────────────────────────────────────
# (These are the same as in night_pipeline.py)

SYSTEM_P = """You are a code review specialist. Find bugs, security issues, and edge cases.
Output JSON:
{
  "findings": [{"id": "F01", "severity": "critical|high|medium|low", "category": "bug|security|...", "description": "1-3 sentences"}]
}"""

SYSTEM_R = """You are a review reflector. For each finding: ACCEPT (real) or REJECT (false).
Output JSON:
{
  "verdicts": [{"id": "F01", "verdict": "accept", "reason": "1 sentence"}]
}"""

SYSTEM_J = """You are a Scoring Judge evaluating P and R.

P_score = Correctness(0-10) + Coverage(0-10) + Precision(0-10) → 0-30
R_score = Accuracy(0-10) + Efficiency(0-10) + Completeness(0-10) → 0-30

Output JSON:
{
  "P_score": 0-30, "R_score": 0-30,
  "rubric_evaluation": {"finder": {"correctness":0,"coverage":0,"precision":0}, "reflector": {"accuracy":0,"efficiency":0,"completeness":0}},
  "decision": "APPROVED|REJECT", "action": "commit|revert|escalate",
  "approved": ["F01"], "rejected": ["F02"],
  "decisions": [{"id":"F01","decision":"approved","reason":"..."}],
  "consensus_score": 0-100,
  "verification_items": [{"check":"...","result":"pass|fail|partial","detail":"..."}]
}"""

VERIFY_SYSTEM_PROMPT = """You are a final verification specialist. Review all findings.
Output JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "action": "commit|revert|escalate",
  "confidence": 0-100,
  "summary": "1 sentence",
  "reasoning": "3-5 sentences",
  "verification_items": [{"check":"...","result":"pass|fail|partial","detail":"..."}],
  "feedback": {"proposer_improvement":"...","refuter_improvement":"...","judge_improvement":"..."}
}"""

SECONDARY_VERIFY_SYSTEM_PROMPT = """You are an independent verification specialist. Second opinion on all findings.
Output JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "action": "commit|revert|escalate",
  "confidence": 0-100,
  "summary": "1 sentence",
  "reasoning": "3-5 sentences",
  "verification_items": [{"check":"...","result":"pass|fail|partial","detail":"..."}],
  "disagreement_with_primary": [{"issue":"...","primary_verdict":"...","detail":"..."}]
}"""


def inject_rubric(system_prompt: str, role: str) -> str:
    """Append rubric instructions to a system prompt."""
    if "proposer" in role or "P " in role or "Finder" in system_prompt:
        section = "### P (Proposer) — Finding Quality Scoring\n" + REVIEW_RUBRIC.split("### P")[1].split("\n### R")[0]
    elif "refuter" in role or "R " in role or "reflector" in role.lower():
        section = "### R (Refuter) — Verdict Quality Scoring\n" + REVIEW_RUBRIC.split("### R")[1].split("\n### J")[0]
    elif "judge" in role or "J " in role or "Scoring Judge" in system_prompt:
        section = "### J (Judge) — Scoring Criteria\n" + REVIEW_RUBRIC.split("### J")[1].split("\n### V")[0]
    elif "verify" in role or "V " in role:
        section = "### V (Verify 27B) — Final Verdict Criteria\n" + REVIEW_RUBRIC.split("### V")[1]
    else:
        section = REVIEW_RUBRIC
    return system_prompt + "\n\n" + section


# ── Helpers ──────────────────────────────────────────────────────────────

def log(msg):
    log_ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{log_ts}] {msg}", flush=True)


def load_input() -> Dict:
    """Load consolidated pipeline input."""
    fpath = os.path.join(SCRIPTS_DIR, "..", "pipeline_input", "consolidated_input_compact.json")
    if not os.path.exists(fpath):
        fpath = fpath.replace("_compact", "")
    with open(fpath) as f:
        return json.load(f)


def swap_pod_b(mode: str, timeout: int = TIMEOUT_SWAP) -> bool:
    log(f"  [swap] Pod B → {mode}")
    with open(MODE_FILE_B, "w") as f:
        f.write(f"MODE={mode}")
    r = subprocess.run(["systemctl", "--user", "restart", "container-devforge-pod-b.service"],
                       capture_output=True, timeout=60)
    if r.returncode != 0:
        return False
    port = 8080 if mode == "review-r" else 8081
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(5)
    return False


def call_llm_json(messages, model, max_tokens=1024, timeout=TIMEOUT_LLM, label=""):
    """Call LLM, return parsed JSON with metadata."""
    try:
        result = call_llm(messages, model=model, max_tokens=max_tokens,
                          timeout=timeout, json_mode=True, return_meta=True)
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
        log(f"  [error] {label} failed: {e}")
        return None


def find_experiment_file(pattern):
    """Find latest experiment result file matching pattern."""
    files = sorted(glob.glob(os.path.join(EXPERIMENT_DIR, pattern)), reverse=True)
    return files[0] if files else None


# ── Phase runners ────────────────────────────────────────────────────────

def run_primary_verify(input_data: Dict, with_rubric: bool = False, output_suffix: str = "") -> Dict:
    """Phase 0: 30B (day mode :8080) verifies all 48 findings."""
    log("\n=== 30B Verify (day mode :8080) ===")
    suffix = f"_rubric{output_suffix}" if with_rubric else output_suffix

    findings_text = json.dumps(input_data["findings"], ensure_ascii=False)[:4000]
    system = VERIFY_SYSTEM_PROMPT if not with_rubric else inject_rubric(VERIFY_SYSTEM_PROMPT, "verify_primary")

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content":
            f"Review {len(input_data['findings'])} findings from pipeline audit.\n\n"
            f"## Summary\n{json.dumps(input_data.get('severity_breakdown',{}), ensure_ascii=False)}\n"
            f"From {input_data['total_files_merged']} evaluation files.\n\n"
            f"## Findings\n{findings_text}"},
    ]
    label = f"30B_verify{suffix}"
    log(f"  [llm] Calling 30B...")
    response = call_llm_json(messages, "proposer", max_tokens=2048, label=label)
    if not response:
        return {"error": "30B call failed"}

    result = {
        "phase": "primary_verify", "round": suffix or "norubric",
        "model": "proposer", "port": 8080,
        "with_rubric": with_rubric,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "usage": response["usage"], "timings": response["timings"],
        "elapsed_ms": response["elapsed_ms"],
        "result": response["result"],
    }
    fpath = os.path.join(EXPERIMENT_DIR, f"primary_verify{suffix}.json")
    with open(fpath, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log(f"  [save] {fpath}")
    return result


def run_prj_combo(combo: Dict, input_data: Dict, round_num: int,
                  with_rubric: bool = False) -> Dict:
    """Run P→R→J with a specific model combo. Swap Pod B twice."""
    suffix = f"_round{round_num}"
    rubric_tag = "_rubric" if with_rubric else ""

    log(f"\n=== P-R-J {combo['name']} ===")
    findings_text = json.dumps(input_data["findings"], ensure_ascii=False)[:4000]

    log(f"  [llm] Proposer ({combo['proposer_model']})...")
    p_system = SYSTEM_P if not with_rubric else inject_rubric(SYSTEM_P, "proposer")
    p_resp = call_llm_json(
        [{"role": "system", "content": p_system},
         {"role": "user", "content": findings_text}],
        combo["proposer_model"], max_tokens=2048,
        label=f"P_{combo['proposer_model']}{rubric_tag}")
    if not p_resp:
        return {"combo": combo["name"], "error": "Proposer failed"}

    fpath = os.path.join(EXPERIMENT_DIR, f"proposer_{combo['name'][0]}{suffix}{rubric_tag}.json")
    with open(fpath, "w") as f:
        json.dump(p_resp, f, ensure_ascii=False, indent=2)

    # ── Step 2: Refuter — swap Pod B ──
    log(f"  [swap] Pod B → {combo['refuter_mode']}")
    swap_pod_b(combo["refuter_mode"])

    p_findings = p_resp.get("result", {}).get("findings", [])
    log(f"  [llm] Refuter ({combo['refuter_model']}) on {len(p_findings)} findings...")
    r_system = SYSTEM_R if not with_rubric else inject_rubric(SYSTEM_R, "refuter")
    r_resp = call_llm_json(
        [{"role": "system", "content": r_system},
         {"role": "user", "content":
             f"# Findings\n{json.dumps(p_findings, ensure_ascii=False, indent=2)[:3000]}\n\n"
             f"# Original data\n{findings_text[:2000]}"}],
        combo["refuter_model"],
        label=f"R_{combo['refuter_model']}{rubric_tag}")
    if not r_resp:
        return {"combo": combo["name"], "error": "Refuter failed"}

    fpath = os.path.join(EXPERIMENT_DIR, f"refuter_{combo['name'][0]}{suffix}{rubric_tag}.json")
    with open(fpath, "w") as f:
        json.dump(r_resp, f, ensure_ascii=False, indent=2)

    # ── Step 3: Judge — swap Pod B again ──
    log(f"  [swap] Pod B → {combo['judge_mode']}")
    swap_pod_b(combo["judge_mode"])

    r_verdicts = r_resp.get("result", {}).get("verdicts", [])
    log(f"  [llm] Judge ({combo['judge_model']})...")
    j_system = SYSTEM_J if not with_rubric else inject_rubric(SYSTEM_J, "judge")
    j_resp = call_llm_json(
        [{"role": "system", "content": j_system},
         {"role": "user", "content":
             f"## Findings\n{json.dumps(p_findings, ensure_ascii=False, indent=2)[:2000]}\n\n"
             f"## Refuter Verdicts\n{json.dumps(r_verdicts, ensure_ascii=False, indent=2)[:2000]}"}],
        combo["judge_model"], max_tokens=2048,
        label=f"J_{combo['judge_model']}{rubric_tag}")
    if not j_resp:
        return {"combo": combo["name"], "error": "Judge failed"}

    fpath = os.path.join(EXPERIMENT_DIR, f"judge_{combo['name'][0]}{suffix}{rubric_tag}.json")
    with open(fpath, "w") as f:
        json.dump(j_resp, f, ensure_ascii=False, indent=2)

    # Save combo summary
    result = {
        "combo": combo["name"], "round": round_num, "with_rubric": with_rubric,
        "proposer": {"model": combo["proposer_model"], "result": p_resp.get("result", {})},
        "refuter": {"model": combo["refuter_model"], "result": r_resp.get("result", {})},
        "judge": {"model": combo["judge_model"], "result": j_resp.get("result", {})},
        "timings": {
            "proposer_ms": p_resp.get("elapsed_ms", 0),
            "refuter_ms": r_resp.get("elapsed_ms", 0),
            "judge_ms": j_resp.get("elapsed_ms", 0),
            "total_ms": p_resp.get("elapsed_ms", 0) + r_resp.get("elapsed_ms", 0) + j_resp.get("elapsed_ms", 0),
        },
        "tokens": {
            "proposer": p_resp.get("usage", {}),
            "refuter": r_resp.get("usage", {}),
            "judge": j_resp.get("usage", {}),
        },
    }
    fpath = os.path.join(EXPERIMENT_DIR, f"combo_{combo['name'][0]}{suffix}{rubric_tag}_summary.json")
    with open(fpath, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log(f"  [done] Combo {combo['name']} complete")
    return result


def run_final_verify(input_data: Dict, prj_results: List[Dict],
                   with_rubric: bool = False, output_suffix: str = "") -> Dict:
    """Phase: 27B verify — reviews all P-R-J results."""
    log("\n=== 27B Verify (:8081 verified) ===")
    if not swap_pod_b("verify"):
        return {"error": "Pod B verify swap failed"}

    suffix = f"_rubric{output_suffix}" if with_rubric else output_suffix
    summaries = []
    for r in prj_results:
        if r and "combo" in r:
            j = r.get("judge", {}).get("result", {})
            summaries.append({
                "combo": r["combo"],
                "P_score": j.get("P_score", 0),
                "R_score": j.get("R_score", 0),
                "decision": j.get("decision", ""),
                "consensus": j.get("consensus_score", 0),
            })

    system = VERIFY_SYSTEM_PROMPT if not with_rubric else inject_rubric(VERIFY_SYSTEM_PROMPT, "verify_final")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content":
            f"## P-R-J Results Summary\n{json.dumps(summaries, ensure_ascii=False, indent=2)}\n\n"
            f"## Total Findings Analyzed\n{json.dumps(input_data.get('severity_breakdown',{}), ensure_ascii=False)}\n"
            f"From {input_data['total_files_merged']} evaluation files."},
    ]
    log("  [llm] 27B verify...")
    resp = call_llm_json(messages, "verifier", max_tokens=2048, label=f"final_verify{suffix}")
    if not resp:
        return {"error": "27B call failed"}

    result = {
        "phase": "verify_final", "round": suffix or "norubric",
        "with_rubric": with_rubric,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "usage": resp["usage"], "timings": resp["timings"],
        "elapsed_ms": resp["elapsed_ms"],
        "result": resp["result"],
        "prj_summaries": summaries,
    }
    fpath = os.path.join(EXPERIMENT_DIR, f"verify_final{suffix}.json")
    with open(fpath, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result



def find_best_combo(round1_prj_results: List[Dict]) -> str:
    """Find the best P-R-J combo from Round 1 based on P/R scores."""
    best = COMBO_A["name"]
    best_score = -1
    for r in round1_prj_results:
        if not r or "error" in r:
            continue
        j = r.get("judge", {}).get("result", {})
        p_score = j.get("P_score", 0)
        r_score = j.get("R_score", 0)
        consensus = j.get("consensus_score", 50)
        total = p_score + r_score + consensus
        log(f"  Combo {r['combo']}: P={p_score} R={r_score} consensus={consensus} total={total}")
        if total > best_score:
            best_score = total
            best = r["combo"]
    log(f"  Best combo: {best} (total={best_score})")
    return best


def compare_rounds() -> Dict:
    """Compare Round 1 vs Round 2 results."""
    log("\n=== Comparison: Round 1 (no rubric) vs Round 2 (with rubric) ===")

    # Load 27B results from both rounds
    final_verify_nr = find_experiment_file("verify_final_norubric*.json")
    final_verify_r = find_experiment_file("verify_final_rubric_round2*.json")

    # Load combo summaries
    combos_nr = sorted(glob.glob(os.path.join(EXPERIMENT_DIR, "combo_*_round1_norubric_summary.json")))
    combos_r = sorted(glob.glob(os.path.join(EXPERIMENT_DIR, "combo_*_rubric_round2_summary.json")))

    def load_scores(files):
        scores = []
        for f in files:
            try:
                d = json.load(open(f))
                j = d.get("judge", {}).get("result", {})
                p = d.get("proposer", {}).get("result", {})
                r = d.get("refuter", {}).get("result", {})
                tok = d.get("tokens", {})
                p_tok = sum(tok.get("proposer", {}).get(k, 0) for k in ["prompt_tokens","completion_tokens"])
                r_tok = sum(tok.get("refuter", {}).get(k, 0) for k in ["prompt_tokens","completion_tokens"])
                j_tok = sum(tok.get("judge", {}).get(k, 0) for k in ["prompt_tokens","completion_tokens"])
                scores.append({
                    "combo": d["combo"],
                    "P_score": j.get("P_score", 0),
                    "R_score": j.get("R_score", 0),
                    "consensus": j.get("consensus_score", 0),
                    "decision": j.get("decision", ""),
                    "p_findings": len(p.get("findings", [])),
                    "r_verdicts": len(r.get("verdicts", [])),
                    "total_tokens": p_tok + r_tok + j_tok,
                    "total_ms": d.get("timings", {}).get("total_ms", 0) / 1000,
                })
            except Exception as e:
                log(f"  [error] loading {f}: {e}")
        return scores

    nr_scores = load_scores(combos_nr)
    r_scores = load_scores(combos_r)

    comparison = {
        "round1_norubric": {"combos": nr_scores, "final_verify": load_verify(final_verify_nr)},
        "round2_withrubric": {"combos": r_scores, "final_verify": load_verify(final_verify_r)},
        "differences": {},
    }

    if nr_scores and r_scores:
        avg_p_nr = sum(s["P_score"] for s in nr_scores) / len(nr_scores)
        avg_p_r = sum(s["P_score"] for s in r_scores) / len(r_scores)
        avg_r_nr = sum(s["R_score"] for s in nr_scores) / len(nr_scores)
        avg_r_r = sum(s["R_score"] for s in r_scores) / len(r_scores)
        avg_c_nr = sum(s["consensus"] for s in nr_scores) / len(nr_scores)
        avg_c_r = sum(s["consensus"] for s in r_scores) / len(r_scores)
        avg_tok_nr = sum(s["total_tokens"] for s in nr_scores) / len(nr_scores)
        avg_tok_r = sum(s["total_tokens"] for s in r_scores) / len(r_scores)

        comparison["differences"] = {
            "avg_P_score": {"no_rubric": round(avg_p_nr, 1), "with_rubric": round(avg_p_r, 1),
                           "delta": round(avg_p_r - avg_p_nr, 1)},
            "avg_R_score": {"no_rubric": round(avg_r_nr, 1), "with_rubric": round(avg_r_r, 1),
                           "delta": round(avg_r_r - avg_r_nr, 1)},
            "avg_consensus": {"no_rubric": round(avg_c_nr, 1), "with_rubric": round(avg_c_r, 1),
                             "delta": round(avg_c_r - avg_c_nr, 1)},
            "avg_tokens_per_combo": {"no_rubric": round(avg_tok_nr), "with_rubric": round(avg_tok_r),
                                    "delta": round(avg_tok_r - avg_tok_nr)},
        }

    fpath = os.path.join(EXPERIMENT_DIR, "comparison_report.json")
    with open(fpath, "w") as f:
        json.dump(comparison, f, ensure_ascii=False, indent=2)
    log(f"  [save] {fpath}")
    return comparison


def load_verify(fpath):
    if not fpath:
        return None
    try:
        d = json.load(open(fpath))
        r = d.get("result", {})
        return {
            "verdict": r.get("final_verdict", ""),
            "confidence": r.get("confidence", 0),
            "summary": r.get("summary", ""),
        }
    except Exception:
        return None


# ── Main ─────────────────────────────────────────────────────────────────

def run_experiment(rounds: Optional[List[int]] = None):
    """Run the full experiment."""
    t0 = time.monotonic()
    log("=" * 60)
    log("DevForge Rubric Experiment")
    log("=" * 60)

    input_data = load_input()
    log(f"Loaded {input_data['total_findings']} findings from {input_data['total_files_merged']} files")

    if rounds is None:
        rounds = [1, 2]

    round1_prj_results = []
    round2_prj_results = []

    # ── Round 1: No rubric ──
    if 1 in rounds:
        log("\n" + "=" * 50)
        log("ROUND 1: No rubric (control)")
        log("=" * 50)

        log("\n--- 30B Verify ---")
        r1_primary = run_primary_verify(input_data, with_rubric=False, output_suffix="_round1_norubric")

        log("\n--- P-R-J: 3 combos ---")
        for i, combo in enumerate(ALL_COMBOS):
            log(f"\n--- Combo {combo['name']} ({i+1}/3) ---")
            result = run_prj_combo(combo, input_data, round_num=1, with_rubric=False)
            round1_prj_results.append(result)

        log("\n--- 27B Verify ---")
        r1_final_verify = run_final_verify(input_data, round1_prj_results,
                                  with_rubric=False, output_suffix="_round1")

    # ── Round 2: With rubric, best combo only ──
    if 2 in rounds:
        log("\n" + "=" * 50)
        log("ROUND 2: With rubric (treatment)")
        log("=" * 50)

        best_combo_name = find_best_combo(round1_prj_results or [])
        best_combo = next((c for c in ALL_COMBOS if c["name"] == best_combo_name), ALL_COMBOS[0])
        log(f"Best combo: {best_combo['name']}")

        log("\n--- 30B Verify (with rubric) ---")
        r2_primary = run_primary_verify(input_data, with_rubric=True, output_suffix="_round2")

        log(f"\n--- P-R-J: {best_combo['name']} (with rubric) ---")
        r2_prj = run_prj_combo(best_combo, input_data, round_num=2, with_rubric=True)
        round2_prj_results.append(r2_prj)

        log("\n--- 27B Verify (with rubric) ---")
        r2_final_verify = run_final_verify(input_data, round2_prj_results,
                                  with_rubric=True, output_suffix="_round2")

    # ── Comparison ──
    if 1 in rounds and 2 in rounds:
        log("\n" + "=" * 50)
        log("COMPARISON: Round 1 vs Round 2")
        log("=" * 50)
        comparison = compare_rounds()
        if comparison.get("differences"):
            d = comparison["differences"]
            log(f"  P_score: {d['avg_P_score']['no_rubric']} → {d['avg_P_score']['with_rubric']} (Δ{d['avg_P_score']['delta']:+g})")
            log(f"  R_score: {d['avg_R_score']['no_rubric']} → {d['avg_R_score']['with_rubric']} (Δ{d['avg_R_score']['delta']:+g})")
            log(f"  Consensus: {d['avg_consensus']['no_rubric']} → {d['avg_consensus']['with_rubric']} (Δ{d['avg_consensus']['delta']:+g})")
            log(f"  Tokens/combo: {d['avg_tokens_per_combo']['no_rubric']} → {d['avg_tokens_per_combo']['with_rubric']} (Δ{d['avg_tokens_per_combo']['delta']:+g})")

    total = round(time.monotonic() - t0, 1)
    log(f"\nExperiment complete in {total}s")
    log(f"Results in: {EXPERIMENT_DIR}/")


def main():
    preflight_checks("rubric.py")
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, choices=[1, 2])
    ap.add_argument("--best-combo", choices=["A", "B", "C"])
    ap.add_argument("--compare", action="store_true")
    args = ap.parse_args()

    if args.compare:
        compare_rounds()
    elif args.best_combo:
        # Run Round 2 only with specific combo
        combo = {"A": COMBO_A, "B": COMBO_B, "C": COMBO_C}[args.best_combo]
        input_data = load_input()
        log(f"Running Round 2 with combo {combo['name']}")
        r2_prj = run_prj_combo(combo, input_data, round_num=2, with_rubric=True)
        r2_final_verify = run_final_verify(input_data, [r2_prj], with_rubric=True, output_suffix="_round2")
    elif args.round:
        run_experiment(rounds=[args.round])
    else:
        run_experiment()


if __name__ == "__main__":
    main()
