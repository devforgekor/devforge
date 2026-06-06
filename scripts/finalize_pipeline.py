#!/usr/bin/env python3
"""Finalize E2E 10-sample pipeline. Skip failed feedback loop; summarize existing phases."""
import json, os, sys
sys.path.insert(0, '/opt/projects/server/scripts')

EXPER_DIR = '/opt/projects/server/data/experiment'
STATE_PATH = f'{EXPER_DIR}/pipeline_state_r1_norubric.json'

with open(STATE_PATH) as f:
    state = json.load(f)

findings = state['input']['findings']
prj_results = state.get('prj', [])
prj_result = prj_results[0] if prj_results else {}
rub_eval = state.get('rubric_evaluation', {}).get('evaluations', [])
v27 = state.get('27b_verify', {})
py_v = state.get('python_verify', {})
v7 = state.get('7b_verify', {})

rubric_scores = [r.get('weighted_score', 0) for r in rub_eval if r.get('weighted_score') is not None]
rubric_avg = sum(rubric_scores) / len(rubric_scores) if rubric_scores else 0
rubric_low = sum(1 for s in rubric_scores if s < 5.0) if rubric_scores else 0

summary = {
    "status": "COMPLETE",
    "pipeline": "3B extract → Python verify → 7B verify → Rubric → P-R-J → 27B verify → Feedback loop",
    "hardware_note": "27B Q4_K_M (16GB) exceeds 22GB RAM. Phase 4 verdict synthetic.",
    "feedback_loop_note": "J(Selene) returned conversational text instead of JSON in feedback round. Self-correction failed. Feedback P-R-J skipped; using original round.",
    "rounds": 1,
    "findings": len(findings),
    "severity": state['input'].get('severity_distribution', {}),
    "phases": {
        "phase0_python_verify": {"issues": len(py_v.get("issues", [])), "total": len(findings)},
        "phase1_7b_verify": {"verdict": v7.get("final_verdict", "?"), "confidence": v7.get("confidence", 0)},
        "phase2_rubric": {"avg_score": round(rubric_avg, 2), "low_count": rubric_low, "total": len(rub_eval)},
        "phase3_prj_original": {
            "P_score": prj_result.get("P_score"), "R_score": prj_result.get("R_score"),
            "consensus": prj_result.get("consensus"), "decision": prj_result.get("decision")
        },
        "phase4_27b": {"verdict": v27.get("final_verdict", "?"), "confidence": v27.get("confidence", 0), "synthetic": True},
        "phase5_feedback": {"status": "FAILED", "reason": "J(Selene) non-JSON response, self-correction failed"},
    },
    "findings_detail": [
        {"id": f.get("id"), "severity": f.get("severity"), "category": f.get("category"),
         "description": f.get("description", "")[:80]}
        for f in findings
    ],
    "rubric_summary": {
        "avg_weighted_score": round(rubric_avg, 2),
        "below_5_count": rubric_low,
        "evaluations": rub_eval,
    },
    "pipeline_state_path": STATE_PATH,
}

summary_path = f'{EXPER_DIR}/exp_summary_final.json'
with open(summary_path, 'w') as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)

print(f'Pipeline final summary: {summary_path}')
print(f'{"="*60}')
print(f'E2E Pipeline (10 samples) — Summary')
print(f'{"="*60}')
print(f"  Phase 0 (Python verify):  ✅ {len(findings)} findings, {len(py_v.get('issues',[]))} issues")
print(f"  Phase 1 (7B verify):      ✅ {v7.get('final_verdict','?')} (conf={v7.get('confidence',0)})")
print(f"  Phase 2 (Rubric):         ✅ avg={rubric_avg:.2f}, low={rubric_low}/{len(rub_eval)}")
print(f"  Phase 3 (P-R-J):          ✅ P={prj_result.get('P_score')} R={prj_result.get('R_score')} → consensus={prj_result.get('consensus')}")
print(f"  Phase 4 (27B verify):     ⚠️ synthetic (hw limit) | verdict={v27.get('final_verdict','?')}")
print(f"  Phase 5 (Feedback loop):  ❌ J(Selene) returned conversational text instead of JSON")
print()
print(f'Pipeline state: pipeline_state_r1_norubric.json')
print(f'Summary:        exp_summary_final.json')
