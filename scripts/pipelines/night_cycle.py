#!/usr/bin/env python3
# Status: experimental
# Path: called by — night_runner.py (subprocess), night_cycle.sh
"""Night Pipeline: DB handoff 로드 → P-R-J(30B→14B→N14B) → night_verify → DB 저장.

Pod B swap sequence: 30B(proposer) → 14B(reflector) → N14B(judge) → 27B(verifier).
Pod A stop for RAM before P-R-J, Pod B restored to day mode after.

Usage:
  python3 night_cycle.py --run-id <run_id> [--tag r1]
"""

import json, os, subprocess, sys
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.watchdog.messenger import log_message
from lib.pipeline_common import (
    JUDGE_MODEL, JUDGE_SYSTEM_PROMPT, PROPOSER_MODEL,
    PROPOSER_SYSTEM_PROMPT, REFLECTOR_MODEL, REFLECTOR_SYSTEM_PROMPT,
    VERIFIER_SYSTEM_PROMPT, HANDOFF_SYSTEM_PROMPT,
    MODEL_METADATA,
    PipelineState, _dedup_findings, abort,
    call_one, compile_handoff_single,
    load_input, log, log_phase_header, resolve_model,
    save, slack_send,
)
from lib.pod_manager import model_info, stop_pod_a

DRY_RUN = "--dry-run" in sys.argv


def _fetch_day_review(run_id):
    """Fetch day_review from activity_log by run_id."""
    sql = (
        "SELECT id, body FROM activity_log "
        f"WHERE type='day_review' AND source='day_pipeline' "
        f"AND exec_status='DONE' AND run_id='{run_id}' "
        "ORDER BY id DESC LIMIT 1"
    )
    r = subprocess.run(
        ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
         "-d", "devforge_app", "-t", "-A", "-F", "|", "-c", sql],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0 or not r.stdout.strip():
        log(f"  No day_review found for run_id={run_id}")
        return None
    parts = r.stdout.strip().split("|", 1)
    if len(parts) < 2:
        return None
    try:
        body = json.loads(parts[1])
    except (json.JSONDecodeError, IndexError):
        return None
    return {"log_id": int(parts[0]), "body": body}


def run_propose_review_judge(state, tag, rubric_append):
    """P-R-J 1회 패스. Pod A stop → P → R → J → state 저장."""
    stop_pod_a()

    log_phase_header("Night Debate — Proposer (P)")
    handoff_fragment = {}

    # P → data/pipeline_run/exp_p_{tag}.json
    p_max = 4096
    proposer_output = call_one(PROPOSER_MODEL, PROPOSER_SYSTEM_PROMPT + rubric_append,
                   state.build_context("prj_proposer"),
                   f"P_{tag}", max_tok=p_max)
    save(f"p_{tag}", tag, proposer_output)  # → data/pipeline_run/exp_p_{tag}.json
    p_findings = (proposer_output or {}).get("result", {}).get("findings", [])
    prev_count = len(p_findings)
    p_findings = _dedup_findings(p_findings)
    if len(p_findings) < prev_count:
        log(f"  Dedup: {prev_count} → {len(p_findings)} findings ({prev_count - len(p_findings)} removed)")

    # R → data/pipeline_run/exp_r_{tag}.json
    r_max = 2048
    reflector_output = call_one(REFLECTOR_MODEL, REFLECTOR_SYSTEM_PROMPT + rubric_append,
                   f"Proposer findings:\n{json.dumps(p_findings, ensure_ascii=False, indent=2)[:4000]}",
                   f"R_{tag}", max_tok=r_max)
    save(f"r_{tag}", tag, reflector_output)  # → data/pipeline_run/exp_r_{tag}.json
    r_verdicts = (reflector_output or {}).get("result", {}).get("verdicts", [])
    r_rejected = (reflector_output or {}).get("result", {}).get("rejected_findings", [])
    if r_rejected and len(r_rejected) > 0:
        log(f"  R rejected {len(r_rejected)} findings — stored in audit trail")

    # J → data/pipeline_run/exp_j_{tag}.json
    j_max = 2048
    judge_output = call_one(JUDGE_MODEL, JUDGE_SYSTEM_PROMPT + rubric_append,
                   state.build_context("prj_judge", {"rotation_index": 0})
                   + f"\n\n### P findings:\n"
                   + json.dumps(p_findings, ensure_ascii=False, indent=2)[:2000]
                   + f"\n\n### R verdicts:\n"
                   + json.dumps(r_verdicts, ensure_ascii=False, indent=2)[:2000],
                   f"J_{tag}", max_tok=j_max)
    save(f"j_{tag}", tag, judge_output)  # → data/pipeline_run/exp_j_{tag}.json

    judge_result = (judge_output or {}).get("result", {})
    j_report = judge_result.get("report", {})
    j_handoff = judge_result.get("handoff", {})
    handoff_fragment = j_handoff
    prj_result = {
        "p_model": PROPOSER_MODEL, "r_model": REFLECTOR_MODEL, "j_model": JUDGE_MODEL,
        "P_score": judge_result.get("P_score", 0),
        "R_score": judge_result.get("R_score", 0),
        "consensus": judge_result.get("consensus_score", 0),
        "decision": judge_result.get("decision", ""),
        "approved": judge_result.get("approved", []),
        "rejected": judge_result.get("rejected", []),
        "p_count": len(p_findings),
        "r_count": len(r_verdicts),
        "r_rejected_findings": r_rejected,
        "p_elapsed_ms": (proposer_output or {}).get("elapsed_ms", 0),
        "r_elapsed_ms": (reflector_output or {}).get("elapsed_ms", 0),
        "j_elapsed_ms": (judge_output or {}).get("elapsed_ms", 0),
        "report_summary": j_report.get("summary", ""),
        "report_top_issues": j_report.get("top_issues", []),
        "report_recommendation": j_report.get("recommendation", ""),
    }
    state.add_prj_rotation(prj_result)
    ps = prj_result['P_score']
    rs = prj_result['R_score']
    cs = prj_result['consensus']
    slack_msg = (f"[Night Debate] P-R-J *Round {state.round_num}*\n"
                 f"P={PROPOSER_MODEL}→{ps} | R={REFLECTOR_MODEL}→{rs} | J={JUDGE_MODEL}→consensus={cs}\n")
    if j_report.get("summary"):
        slack_msg += f"> {j_report['summary'][:120]}"
    slack_send(slack_msg)
    return prj_result, handoff_fragment, p_findings, r_verdicts


def _extract_json_part(txt):
    """Return (header, json_str) from '## Header\\n{...}' handoff text."""
    brace = txt.find("{")
    if brace == -1:
        return txt, ""
    return txt[:brace], txt[brace:]


def _trim_handoff(text, label=""):
    """Trim handoff document to essential fields only (prepill defense)."""
    try:
        data = json.loads(text) if isinstance(text, str) else text
    except (json.JSONDecodeError, TypeError):
        return str(text)[:2000]

    trimmed = {"_trimmed": True, "_source": label}

    hoff = data if "executive_summary" in data else data.get("handoff", data)
    if isinstance(hoff, dict) and hoff.get("executive_summary"):
        trimmed.update({
            "source": "llm_r",
            "executive_summary": hoff.get("executive_summary", "")[:200],
            "unresolved_count": hoff.get("unresolved_count", 0),
            "critical_remaining": hoff.get("critical_remaining", [])[:3],
            "p_score": hoff.get("p_score", 0),
            "r_score": hoff.get("r_score", 0),
            "j_consensus": hoff.get("j_consensus", 0),
            "approved_ids": [a.get("id","") for a in (hoff.get("approved") or [])[:5]],
            "rejected_ids": [r.get("id","") for r in (hoff.get("rejected") or [])[:5]],
            "verifier_priority": (hoff.get("verifier_priority") or [])[:3],
        })
        return json.dumps(trimmed, ensure_ascii=False, indent=2)

    if "P_score" in data and "decision" in data:
        trimmed.update({
            "source": "python",
            "P_score": data.get("P_score"),
            "R_score": data.get("R_score"),
            "consensus": data.get("consensus"),
            "decision": data.get("decision"),
            "approved_count": data.get("approved_count", data.get("total_approved", 0)),
            "rejected_count": data.get("rejected_count", data.get("total_rejected", 0)),
            "report_summary": (data.get("report_summary") or "")[:200],
        })
        return json.dumps(trimmed, ensure_ascii=False, indent=2)

    s = json.dumps(data, ensure_ascii=False, indent=2)
    return s[:2000] + ("\n... (truncated)" if len(s) > 2000 else "")


def save_feedback_to_db(night_verify_feedback, tag):
    """Save night_verify's per-model feedback to activity_log."""
    if DRY_RUN:
        log("  [DRY] save_feedback_to_db() → simulated 1 entry")
        return 1
    if not night_verify_feedback:
        return 0

    count = 0
    for role_key, role_fb in night_verify_feedback.items():
        if not isinstance(role_fb, dict):
            continue
        model = role_fb.get("model", "")
        role = role_fb.get("role", "")
        score = role_fb.get("score", 0)
        strengths = role_fb.get("strengths", [])
        weaknesses = role_fb.get("weaknesses", [])
        improvements = role_fb.get("improvements", [])

        if not weaknesses and not strengths:
            continue

        findings = []
        for i, w in enumerate(weaknesses):
            fix = improvements[i] if i < len(improvements) else "Review and address this weakness."
            findings.append({
                "description": str(w)[:300], "severity": "medium", "category": "quality",
                "fix": str(fix)[:300],
            })
        verification_items = [
            {"check": str(s)[:300], "result": "pass", "detail": "Strength confirmed in night_verify review"}
            for s in strengths
        ]

        title = f"night_verify feedback: {model} ({role})"
        summary = f"night_verify review feedback for {model} ({role}): score={score}/100, {len(strengths)} strengths, {len(weaknesses)} weaknesses"

        body = {
            "findings": findings,
            "verification_items": verification_items,
            "source": "verify_feedback",
            "feedback_role": role,
            "feedback_model": model,
            "score": score,
        }

        body_json = json.dumps(body, ensure_ascii=False).replace("'", "''")
        title_esc = title.replace("'", "''")
        summary_esc = summary.replace("'", "''")

        sql = (
            "INSERT INTO activity_log "
            "(type, source, title, summary, body, model, summary_status, queue_status, exec_status) "
            "VALUES ("
            f"'verify_result', 'verify_feedback', '{title_esc}', "
            f"'{summary_esc}', '{body_json}', 'deepseek-v4-flash', "
            "'raw', 'reviewed', 'DONE'"
            ")"
        )
        r = subprocess.run(
            ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
             "-d", "devforge_app", "-c", sql],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            count += 1
            log(f"  Saved feedback for {model} ({role}) to activity_log")
            # Watchdog Integration (NewHand)
            try:
                log_message(
                    source="night_verify",
                    target="operator",
                    type="HOT_FIX" if score < 70 else "CONTEXT",
                    content=f"[{role}] {weaknesses[0] if weaknesses else 'Performance Feedback'}",
                    detail=json.dumps({"model": model, "role": role, "improvements": improvements, "score": score}, ensure_ascii=False)
                )
            except Exception as e:
                log(f"  [Watchdog] Error reporting feedback: {e}")
        else:
            log(f"  Failed to save feedback for {model} ({role}): {r.stderr[:100]}")

    return count


def main():
    # ── Queue mode: delegate to prj_cycle queue consumer ─────
    if "--queue" in sys.argv:
        limit = 5
        for i, a in enumerate(sys.argv):
            if a == "--limit" and i + 1 < len(sys.argv):
                limit = int(sys.argv[i + 1])
        from pipelines.prj_cycle import run_queue_mode
        result = run_queue_mode(limit=limit)
        log(f"Queue mode complete: {result['processed']}/{result['total']} processed")
        return

    run_id = None
    tag = "r1"
    for i, a in enumerate(sys.argv):
        if a == "--run-id" and i + 1 < len(sys.argv):
            run_id = sys.argv[i + 1]
        if a == "--tag" and i + 1 < len(sys.argv):
            tag = sys.argv[i + 1]

    if not run_id:
        log("ERROR: --run-id required")
        sys.exit(1)

    log("=" * 60)
    log("NIGHT PIPELINE")
    log(f"Run ID: {run_id}, Tag: {tag}")
    log("=" * 60)

    # 1. Fetch day_review from DB
    log_phase_header("Phase: Load day_review from DB")
    day_review = _fetch_day_review(run_id)
    if not day_review:
        log(f"  No day_review found for run_id={run_id} — exiting")
        sys.exit(1)
    log(f"  Loaded day_review log_id={day_review['log_id']}")
    day_body = day_review["body"]

    # Reconstruct PipelineState from day_review data
    data = load_input()
    state = PipelineState(1, False, data)
    py_verify = day_body.get("python_verify", {})
    day_verify = day_body.get("day_verify", {})
    if py_verify:
        state.add_phase("python_verify", py_verify)
    if day_verify:
        state.add_phase("day_verify", day_verify)
    log(f"  Restored state: python_verify={bool(py_verify)}, day_verify={bool(day_verify)}")

    rubric_append = ""  # rubric disabled
    round_num = 1

    # Phase 3: P-R-J 1 pass
    prj_result, handoff_fragment, p_findings, r_verdicts = run_propose_review_judge(state, tag, rubric_append)

    # Phase 3.5: R(night_reflector) writes final handoff
    # → data/pipeline_run/exp_handoff_r_{tag}.json (LLM-R)
    # → data/pipeline_run/exp_handoff_llm_{tag}.json (LLM)
    # → data/pipeline_run/exp_handoff_py_{tag}.json (Python compiled)
    log_phase_header("Phase 3.5: R handoff writer")

    r_ctx_parts = [
        f"=== NIGHT DEBATE COMPLETE ===",
        f"P_model={PROPOSER_MODEL} R_model={REFLECTOR_MODEL} J_model={JUDGE_MODEL}\n",
        f"=== P PROPOSED FINDINGS ({len(p_findings)}) ===",
    ]
    for pf in p_findings:
        r_ctx_parts.append(
            f"  {pf['id']} [{pf.get('severity','?')}/{pf.get('category','?')}]: {pf.get('description','')[:200]}")
    r_ctx_parts.append(f"\n=== R VERDICTS ({len(r_verdicts)}) ===")
    for rv in r_verdicts:
        r_ctx_parts.append(f"  {rv['id']}: {rv.get('verdict','?')} — {rv.get('reason','')[:150]}")
    r_ctx_parts.append(f"\n=== J FINAL DECISION ===")
    r_ctx_parts.append(f"  P_score={prj_result.get('P_score','?')} R_score={prj_result.get('R_score','?')}")
    r_ctx_parts.append(f"  consensus={prj_result.get('consensus','?')} decision={prj_result.get('decision','?')}")
    r_ctx_parts.append(f"  approved={prj_result.get('approved',[])}")
    r_ctx_parts.append(f"  rejected={prj_result.get('rejected',[])}")
    r_ctx_parts.append(f"  summary: {prj_result.get('report_summary','')}")
    for ti in (prj_result.get('report_top_issues') or []):
        r_ctx_parts.append(f"  top issue: {ti}")
    r_handoff_ctx = "\n".join(r_ctx_parts)

    r_hoff_resp = call_one(REFLECTOR_MODEL, HANDOFF_SYSTEM_PROMPT, r_handoff_ctx, f"handoff_R_{tag}")
    r_hoff_data = (r_hoff_resp or {}).get("result", {}).get("handoff", {})
    save(f"handoff_r_{tag}", tag, {  # → data/pipeline_run/exp_handoff_r_{tag}.json
        "source": "llm_r", "handoff": r_hoff_data,
        "p_findings_count": len(p_findings), "r_verdicts_count": len(r_verdicts),
        "model_metadata": {k: MODEL_METADATA.get(resolve_model(k)) for k in (PROPOSER_MODEL, REFLECTOR_MODEL, JUDGE_MODEL)}})

    # Save handoffs
    handoff_models = {
        "P": {"key": PROPOSER_MODEL, **MODEL_METADATA.get(resolve_model(PROPOSER_MODEL), {})},
        "R": {"key": REFLECTOR_MODEL, **MODEL_METADATA.get(resolve_model(REFLECTOR_MODEL), {})},
        "J": {"key": JUDGE_MODEL, **MODEL_METADATA.get(resolve_model(JUDGE_MODEL), {})},
    }
    hoff_meta = json.dumps(handoff_models, ensure_ascii=False, indent=2)
    hoff_header = f"## Model Metadata (for future reference)\n{hoff_meta}\n\n"

    llm_save = {"source": "llm_r", "round": round_num,
                "r_model": REFLECTOR_MODEL, "handoff": r_hoff_data}
    save(f"handoff_llm_{tag}", tag, llm_save)  # → data/pipeline_run/exp_handoff_llm_{tag}.json
    llm_text = (hoff_header
                + f"## Handoff (R={model_info(REFLECTOR_MODEL)}) [LLM-R]\n"
                + json.dumps(llm_save, ensure_ascii=False, indent=2))

    py_single = compile_handoff_single(prj_result, round_num, False)
    py_single["model_metadata"] = handoff_models
    save(f"handoff_py_{tag}", tag, py_single)  # → data/pipeline_run/exp_handoff_py_{tag}.json
    py_text = f"## Handoff [Python]\n" + json.dumps(py_single, ensure_ascii=False, indent=2)
    pyc_text = json.dumps(py_single, ensure_ascii=False, indent=2)

    state.add_phase("handoffs", {"llm_texts": [llm_text], "py_texts": [py_text],
                                 "consolidated": py_single})

    log(f"  P-R-J 완료: {prj_result.get('decision','?')} (consensus={prj_result.get('consensus','?')})")
    slack_send(f"[Night Debate] P-R-J 완료: {prj_result.get('decision','?')} (consensus={prj_result.get('consensus','?')})")

    # Phase 4: night_verify (27B) → data/pipeline_run/exp_night_verify_{tag}.json
    log_phase_header("Phase 4: night_verify")

    llm_header, llm_json = _extract_json_part(llm_text)
    py_header, py_json = _extract_json_part(py_text)
    trimmed_llm = _trim_handoff(llm_json, "llm_r")
    trimmed_py = _trim_handoff(py_json, "python")
    trimmed_pyc = _trim_handoff(pyc_text, "python_consolidated")

    verifier_input = (
        "Below are 3 handoff documents:\n"
        "- 1 LLM-R\n"
        "- 1 Python\n"
        "- 1 Python consolidated\n\n"
        + f"\n\n---\n\n{llm_header}{trimmed_llm}\n\n---\n\n"
        + f"{py_header}{trimmed_py}\n\n---\n\n"
        + f"## Consolidated Handoff [Python]\n{trimmed_pyc}\n\n"
        "Compare LLM-R vs Python. "
        "In your 'handoff_comparison' field, state which source "
        "(llm_r or python) was more useful for verification overall and why."
    )

    night_verify_context = state.build_context("final_verify") + "\n\n" + verifier_input
    night_verify_resp = call_one("night_verify", VERIFIER_SYSTEM_PROMPT + rubric_append,
                   night_verify_context, f"night_verify_{tag}")
    night_verify_result = (night_verify_resp or {}).get("result", {})
    save(f"night_verify_{tag}", tag, night_verify_resp)  # → data/pipeline_run/exp_night_verify_{tag}.json
    state.add_phase("night_verify", night_verify_result)
    night_verify_verdict = night_verify_result.get('final_verdict', '?')
    night_verify_confidence = night_verify_result.get('confidence', '?')
    handoff_comparison = night_verify_result.get('handoff_comparison', {})
    night_verify_feedback = night_verify_result.get('feedback', {})
    log(f"  night_verify verdict={night_verify_verdict} confidence={night_verify_confidence}")
    log(f"  handoff preference: {handoff_comparison.get('better_handoff', '?')}")
    slack_send(f"[Night Pipeline] night_verify: *{night_verify_verdict}* "
               f"(conf={night_verify_confidence}) handoff={handoff_comparison.get('better_handoff','?')}")

    # Save feedback to DB → activity_log (type='feedback')
    fb_count = save_feedback_to_db(night_verify_feedback, tag)
    if fb_count:
        log(f"  Saved {fb_count} feedback entries to activity_log")

    # Save final summary
    summary = {
        "round": round_num, "with_rubric": False,
        "run_id": run_id, "day_review_log_id": day_review["log_id"],
        "python_verify": {"issues_found": py_verify.get("issues_found", 0) if py_verify else 0,
                          "total": py_verify.get("total_findings", 0) if py_verify else 0},
        "day_verify": {"verdict": day_verify.get("final_verdict", "?"), "confidence": day_verify.get("confidence", 0)},
        "prj": [prj_result],
        "night_verify": {"verdict": night_verify_result.get("final_verdict", "?"),
                         "confidence": night_verify_result.get("confidence", 0),
                         "feedback": night_verify_feedback,
                         "handoff_comparison": handoff_comparison},
        "feedback_saved": fb_count,
    }
    save(f"summary_night_{tag}", tag, summary)  # → data/pipeline_run/exp_summary_night_{tag}.json

    # Save night_review to activity_log → DB (type='night_review')
    log_phase_header("Saving night_review to activity_log")
    night_handoff = {
        "tag": tag,
        "run_id": run_id,
        "day_review_log_id": day_review["log_id"],
        "prj_results": prj_result,
        "night_verify": night_verify_result,
        "handoff_preference": handoff_comparison.get("better_handoff", "equal"),
        "schema_version": 1,
    }
    body_json = json.dumps(night_handoff, ensure_ascii=False).replace("'", "''")
    sql = (
        "INSERT INTO activity_log "
        "(type, source, title, summary, body, run_id, exec_status) "
        "VALUES ("
        f"'night_review', 'night_cycle', 'Night Debate: {tag}', "
        f"'night_verify={night_verify_verdict} confidence={night_verify_confidence}', "
        f"'{body_json}'::jsonb, '{run_id}', 'DONE'"
        ")"
    )
    r = subprocess.run(
        ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
         "-d", "devforge_app", "-c", sql],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode == 0:
        log(f"  night_review saved (run_id={run_id})")
    else:
        log(f"  DB save failed (non-fatal): {r.stderr[:200]}")

    log(f"\n{'='*60}")
    log("NIGHT PIPELINE COMPLETE")
    log(f"{'='*60}")


if __name__ == "__main__":
    main()
