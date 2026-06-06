#!/usr/bin/env python3
# Status: experimental
# Path: none — library
"""
Transform prj_cycle.py for experiment phases.

Independent flags that compose cleanly (applied in order):
  --structural   0-5 scaling, evidence fields, stratified catfish, J_rubric
  --rubric-off   Disable rubric_append + rubric_evaluate_findings
  --rubric-on    Re-enable rubric (undoes --rubric-off)
  --feedback-off Disable feedback loop
  --feedback-on  Re-enable feedback loop (undoes --feedback-off)

Default (no flags): structural=OFF, rubric=ON, feedback=ON (original code).

Usage:
  # Phase 0: baseline (no structural, rubric off, feedback off)
  python3 transform_prj.py --rubric-off --feedback-off < prj_cycle.py > phase0.py

  # Phase 1: structural only, rubric/feedback off
  python3 transform_prj.py --structural --rubric-off --feedback-off < prj_cycle.py

  # Phase 2: structural + rubric on, feedback off
  python3 transform_prj.py --structural --feedback-off < prj_cycle.py

  # Phase 3: structural + rubric + feedback (all on, no flags needed except --structural)
  python3 transform_prj.py --structural < prj_cycle.py

Returns exit code 0 on success, 1 if any replacement failed.
"""

import sys


def apply_replacements(src, replacements):
    """Apply (label, old, new) replacements, warning on mismatch."""
    for label, old, new in replacements:
        count = src.count(old)
        if count == 0:
            print(f"  WARN: '{label}' — NOT FOUND in source", file=sys.stderr)
            continue
        if count > 1:
            print(f"  WARN: '{label}' — {count} matches (may double-replace)", file=sys.stderr)
        src = src.replace(old, new)
    return src


# ── Structural improvements ──────────────────────────────────────

def _structural_improvements(src):
    """0-5 scaling, evidence fields, stratified catfish, J_rubric."""
    reps = []

    # 1. Stratified catfish
    reps.append(("catfish-text",
        'When all P, R, J agree quickly, pay EXTRA attention — the most critical bugs are often missed by consensus.',
        'When P, R, J agree: if consensus within 1 round → inject STRONG dissent.\n'
        'If disagreement < 30% among findings → inject MODERATE dissent.\n'
        'Otherwise proceed — consensus is genuine.'))

    # 2. SYS_P rubric items (0-10 → 0-5)
    reps.append(("SYS_P-rubric-items",
        '- Correctness (0-10): Is each finding a real, verifiable issue?\n- Actionability (0-10): Is there a clear fix or mitigation?\n- Evidence (0-10): Is it backed by specific data or code?\n- Novelty (0-10): Does it add new insight?',
        '- Correctness (0-5): 0=wrong, 3=mostly correct, 5=fully correct\n- Actionability (0-5): 0=no fix, 3=partial fix, 5=clear fix\n- Evidence (0-5): 0=no evidence, 3=partial citation, 5=exact file:line\n- Novelty (0-5): 0=duplicate, 3=somewhat new, 5=unique insight'))

    # 3. SYS_P JSON finding template — add evidence field
    reps.append(("SYS_P-json-evidence",
        '    {"id": "F001", "severity": "critical|high|medium|low", "category": "bug|security|data_loss|performance|quality", "description": "1-3 sentence explanation", "file": "filename or area"}',
        '    {"id": "F001", "severity": "critical|high|medium|low", "category": "bug|security|data_loss|performance|quality", "description": "1-3 sentence explanation", "file": "filename or area", "evidence": {"file": "path/to/file", "line": "42-57", "quote": "exact code or text"}}'))

    # 4. SYS_P rubric_evaluation score fields (0-10 → 0-5)
    for field in ["correctness", "actionability", "evidence", "novelty"]:
        reps.append((f"SYS_P-{field}-score",
            f'    "{field}": 0-10,',
            f'    "{field}": 0-5,'))

    # 5. SYS_R rubric items (0-10 → 0-5 + evidence line)
    reps.append(("SYS_R-rubric-items",
        '- Accuracy (0-10): Is each accept/reject decision correct?\n- Reasoning (0-10): Is justification precise and specific?\n- Efficiency (0-10): Are verdicts concise?',
        '- Accuracy (0-5): 0=wrong, 3=mostly right, 5=fully correct\n- Reasoning (0-5): 0=vague, 3=adequate, 5=precise & specific\n- Efficiency (0-5): 0=verbose, 3=reasonable, 5=concise'))

    # 6. SYS_R JSON verdict — add evidence field
    reps.append(("SYS_R-json-evidence-1",
        '    {"id": "F001", "verdict": "accept", "reason": "concise justification"},',
        '    {"id": "F001", "verdict": "accept", "reason": "concise justification", "evidence": {"finding_id": "F001", "source_check": "file:line"}},'))
    reps.append(("SYS_R-json-evidence-2",
        '    {"id": "F002", "verdict": "reject", "reason": "concise justification"}',
        '    {"id": "F002", "verdict": "reject", "reason": "concise justification", "evidence": {"finding_id": "F002", "source_check": "file:line"}}'))

    # 7. SYS_R rubric_evaluation score fields (0-10 → 0-5)
    for field in ["accuracy", "reasoning", "efficiency"]:
        reps.append((f"SYS_R-{field}-score",
            f'    "{field}": 0-10,',
            f'    "{field}": 0-5,'))

    # 8. SYS_J P_score/R_score 0-30 → 0-15 + J_score 0-15
    reps.append(("SYS_J-scores",
        'P_score = Correctness(0-10) + Coverage(0-10) + Precision(0-10) -> 0-30\nR_score = Accuracy(0-10) + Efficiency(0-10) + Completeness(0-10) -> 0-30',
        'P_score = Correctness(0-5) + Coverage(0-5) + Precision(0-5) -> 0-15\n'
        'R_score = Accuracy(0-5) + Efficiency(0-5) + Completeness(0-5) -> 0-15\n'
        'J_score = Fairness(0-5) + Consistency(0-5) + Clarity(0-5) -> 0-15'))

    # 9. SYS_J rubric items (0-10 → 0-5 + evidence)
    reps.append(("SYS_J-rubric-items",
        '- Fairness (0-10): Are scores balanced and justified by the evidence?\n- Clarity (0-10): Is the handoff document clear and actionable?\n- Consistency (0-10): Do approved/rejected sets match the scores?',
        '- Fairness (0-5): 0=biased, 3=fair, 5=perfectly balanced with evidence\n- Consistency (0-5): 0=contradictory, 3=mostly aligned, 5=fully consistent\n- Clarity (0-5): 0=unclear, 3=adequate, 5=crystal clear with specific examples'))

    # 10. SYS_J JSON output — add J_score, J_rubric, evidence
    reps.append(("SYS_J-json-output",
        '  "P_score": 0-30,\n  "P_rubric": {"correctness": 0-10, "coverage": 0-10, "precision": 0-10},\n  "R_score": 0-30,\n  "R_rubric": {"accuracy": 0-10, "efficiency": 0-10, "completeness": 0-10},',
        '  "P_score": 0-15,\n  "P_rubric": {"correctness": 0-5, "coverage": 0-5, "precision": 0-5},\n'
        '  "P_evidence": {"correctness": [], "coverage": [], "precision": []},\n'
        '  "R_score": 0-15,\n  "R_rubric": {"accuracy": 0-5, "efficiency": 0-5, "completeness": 0-5},\n'
        '  "R_evidence": {"accuracy": [], "efficiency": [], "completeness": []},\n'
        '  "J_score": 0-15,\n  "J_rubric": {"fairness": 0-5, "consistency": 0-5, "clarity": 0-5},\n'
        '  "J_evidence": {"fairness": [], "consistency": [], "clarity": []},'))

    # 11. SYS_J rubric_evaluation (0-10 → 0-5)
    reps.append(("SYS_J-fairness", '    "fairness": 0-10,\n    "fairness_justification": "...",', '    "fairness": 0-5,\n    "fairness_justification": "...",'))
    reps.append(("SYS_J-clarity", '    "clarity": 0-10,\n    "clarity_justification": "...",', '    "clarity": 0-5,\n    "clarity_justification": "...",'))
    reps.append(("SYS_J-consistency", '    "consistency": 0-10,\n    "consistency_justification": "..."', '    "consistency": 0-5,\n    "consistency_justification": "..."'))

    # 12. SYS_V27 rubric items (0-10 → 0-5)
    reps.append(("SYS_V27-thoroughness",
        '- Thoroughness (0-10): Are all handoff documents compared and cross-checked?',
        '- Thoroughness (0-5): 0=skipped, 3=partial, 5=all docs cross-checked'))
    reps.append(("SYS_V27-evidence_check",
        '- Evidence Check (0-10): Are verification items backed by specific data?',
        '- Evidence Check (0-5): 0=no data, 3=some, 5=all items backed by specific data'))
    reps.append(("SYS_V27-feedback_quality",
        '- Feedback Quality (0-10): Is per-role feedback actionable and constructive?',
        '- Feedback Quality (0-5): 0=vague, 3=adequate, 5=actionable per-role feedback'))

    # 13. SYS_V27 rubric_evaluation fields (0-10 → 0-5)
    reps.append(("SYS_V27-rubric-thoroughness", '    "thoroughness": "0-10",', '    "thoroughness": "0-5",'))
    reps.append(("SYS_V27-rubric-evidence", '    "evidence_check": "0-10",', '    "evidence_check": "0-5",'))
    reps.append(("SYS_V27-rubric-feedback", '    "feedback_quality": "0-10",', '    "feedback_quality": "0-5",'))

    return apply_replacements(src, reps)


# ── Rubric toggle ────────────────────────────────────────────────

_RUBRIC_OFF_OLD = '    rubric_append = f"\n\n{RUBRIC}" if with_rubric else ""'
_RUBRIC_OFF_NEW = '    rubric_append = ""  # rubric disabled'

_RUBRIC_CALL_OLD = (
    '        # Phase 2: Rubric evaluation (finding-level scores via 7B)\n'
    '        findings = state.data.get("input", {}).get("findings", [])\n'
    '        rubric_results = rubric_evaluate_findings(findings, tag)\n'
    '        state.add_phase("rubric_evaluation", {"evaluations": rubric_results})'
)
_RUBRIC_CALL_NEW = (
    '        # Phase 2: Rubric evaluation — disabled\n'
    '        rubric_results = []\n'
    '        log("  Rubric evaluation disabled")'
)


def _rubric_off(src):
    reps = [
        ("rubric_append-off", _RUBRIC_OFF_OLD, _RUBRIC_OFF_NEW),
        ("rubric-call-off", _RUBRIC_CALL_OLD, _RUBRIC_CALL_NEW),
    ]
    return apply_replacements(src, reps)


def _rubric_on(src):
    reps = [
        ("rubric_append-on", _RUBRIC_OFF_NEW, _RUBRIC_OFF_OLD),
        ("rubric-call-on", _RUBRIC_CALL_NEW, _RUBRIC_CALL_OLD),
    ]
    return apply_replacements(src, reps)


# ── Feedback toggle ──────────────────────────────────────────────

_FEEDBACK_OFF_OLD = (
    '    # ── Phase 5: Feedback loop — save patterns to DB, re-run P-R-J ──\n'
    '    fb_count = save_feedback_to_db(fb27, tag)\n'
    '    if fb_count > 0:'
)
_FEEDBACK_OFF_NEW = (
    '    # ── Phase 5: Feedback loop — disabled ──\n'
    '    fb_count = 0  # feedback disabled\n'
    '    if False:  # feedback disabled'
)


def _feedback_off(src):
    reps = [
        ("feedback-off", _FEEDBACK_OFF_OLD, _FEEDBACK_OFF_NEW),
    ]
    return apply_replacements(src, reps)


def _feedback_on(src):
    reps = [
        ("feedback-on", _FEEDBACK_OFF_NEW, _FEEDBACK_OFF_OLD),
    ]
    return apply_replacements(src, reps)


# ── Main ─────────────────────────────────────────────────────────

def main():
    flags = set(sys.argv[1:])

    structural = "--structural" in flags
    rubric_off = "--rubric-off" in flags
    rubric_on = "--rubric-on" in flags
    feedback_off = "--feedback-off" in flags
    feedback_on = "--feedback-on" in flags

    # Safety: can't have both on and off for same toggle
    if rubric_off and rubric_on:
        print("ERROR: --rubric-off and --rubric-on are mutually exclusive", file=sys.stderr)
        sys.exit(1)
    if feedback_off and feedback_on:
        print("ERROR: --feedback-off and --feedback-on are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    src = sys.stdin.read()

    # Apply transformations in order: structural first, then rubric/feedback toggles
    if structural:
        src = _structural_improvements(src)

    if rubric_off:
        src = _rubric_off(src)
    elif rubric_on:
        src = _rubric_on(src)
    # else: default — rubric stays ON (original code)

    if feedback_off:
        src = _feedback_off(src)
    elif feedback_on:
        src = _feedback_on(src)
    # else: default — feedback stays ON (original code)

    sys.stdout.write(src)


if __name__ == "__main__":
    main()
