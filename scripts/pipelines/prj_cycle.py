#!/usr/bin/env python3
# Status: production
# Path: night_cycle.sh / 15m_cycle.sh
"""
P-R-J 고정 역할 실험: P=night_proposer, R=night_reflector, J=night_judge

P-R-J = night_proposer + night_reflector + night_judge (고정 역할)
night_proposer -> night_reflector -> night_judge 1회 패스

파이프라인: Python 검증 -> day_verify -> P-R-J 1회 -> 핸드오프 저장

컨테이너 전략:
- Extract: Pod B(3B extractor:8080) extract 전담, Pod A(7B reviewer:8082)는 유지
  → start_pod_a_only("day",8082)로 extract 직전 7B reviewer 재시작 (verify 준비)
- Day_verify: Pod A(7B reviewer:8082) verify/MCP/global context 전담
- P-R-J: Pod A(7B reviewer) stop (RAM 확보), Pod B(30B/14B/14B) 순차 swap

사용법:
  python3 prj_cycle.py
"""

import json, os, subprocess, sys, time, urllib.request, hashlib, uuid
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.infra.preflight import preflight_checks
from lib.llm.json_parser import save_dlq, validate_schema
from lib.pod_manager import (
    kill_all, start_pod_b, start_pod_a, start_pod_a_only, stop_pod_a,
    start_day_both, ensure_model, NIGHT_MODELS,
    MODEL_METADATA, model_info, MODE_FILE_B, MODE_FILE_A, TIMEOUT,
    wait_health, wait_probe,
)
from lib.token_budget import TokenBudget

RESUME_PRJ = "--resume-prj" in sys.argv
DRY_RUN = "--dry-run" in sys.argv
INPUT_OVERRIDE = None
for _i, _a in enumerate(sys.argv):
    if _a == "--input" and _i + 1 < len(sys.argv):
        INPUT_OVERRIDE = sys.argv[_i + 1]

if DRY_RUN:
    print("[DRY RUN] 모드 활성화 — LLM 호출/컨테이너 없이 데이터 흐름만 검증")
    print()

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPER_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "experiment")
EVENTS_DIR = os.path.join(EXPER_DIR, "events")
os.makedirs(EXPER_DIR, exist_ok=True)
os.makedirs(EVENTS_DIR, exist_ok=True)
sys.path.insert(0, SCRIPTS_DIR)

# ── Schema definitions (JSON Schema subset) ────────────────────────
# Validated against role outputs in llm_call() and compile_handoff*().

VERIFY_SCHEMA = {
    "required": ["final_verdict", "confidence", "summary"],
    "additionalProperties": False,
    "properties": {
        "final_verdict":  {"type": "string", "enum": ["PASS", "FAIL", "NEEDS_REVIEW", "ESCALATE"]},
        "confidence":     {"type": "integer"},
        "summary":        {"type": "string"},
        "reasoning":      {"type": "string"},
        "action":         {"type": "string"},
        "verification_items": {"type": "array"},
        "schema_version": {"type": "integer"},
    },
}

PRJ_RESULT_SCHEMA = {
    "required": ["P_score", "R_score", "consensus", "decision"],
    "additionalProperties": True,
    "properties": {
        "P_score":    {"type": "integer"},
        "R_score":    {"type": "integer"},
        "consensus":  {"type": "integer"},
        "decision":   {"type": "string", "enum": ["APPROVED", "REJECT"]},
        "approved":   {"type": "array"},
        "rejected":   {"type": "array"},
        "schema_version": {"type": "integer"},
    },
}

HANDOFF_SCHEMA = {
    "required": ["source"],
    "additionalProperties": True,
    "properties": {
        "source":         {"type": "string"},
        "approved_ids":   {"type": "array"},
        "rejected_ids":   {"type": "array"},
        "schema_version": {"type": "integer"},
        "checksum":       {"type": "string"},
    },
}
from lib.llm_client import call_llm, resolve_model
from lib.db import psql, psql_ok, esc_sql, psql_json

from lib.pipeline_common import (
    save, strip_code_fence, _extract_json, timestamp,
    abort, llm_call, slack_send, PipelineState,
)
from pipelines.night_cycle import save_feedback_to_db
from lib.common import log
from sentence_transformers import SentenceTransformer

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"

# ── Slack notification ──────────────────────────────────────────

_SF = Path.home() / ".config/devforge/secrets.env"
_SLACK_TOKEN = ""
_SLACK_CHANNEL = "U0APJGD8CBW"
if _SF.exists():
    for _line in _SF.read_text().split("\n"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _k.strip() == "SLACK_BOT_TOKEN":
                _SLACK_TOKEN = _v.strip().strip('"').strip("'")
            elif _k.strip() == "SLACK_CHANNEL":
                _SLACK_CHANNEL = _v.strip().strip('"').strip("'")



def load_input():
    if INPUT_OVERRIDE:
        fpath = INPUT_OVERRIDE
        if not os.path.isabs(fpath):
            fpath = os.path.join(SCRIPTS_DIR, "..", fpath)
    else:
        fpath = os.path.join(SCRIPTS_DIR, "..", "pipeline_input", "consolidated_input_compact.json")
        if not os.path.exists(fpath):
            fpath = fpath.replace("_compact", "")
    with open(fpath) as f:
        return json.load(f)




# ── Pipeline State Blackboard ──────────────────────────────────────────
# 단일 JSON 파일에 모든 phase 결과를 축적. 각 phase는 add_phase()로 추가,
# build_context()로 다음 phase의 LLM 프롬프트용 요약문 생성.





def _schema_for_label(label: str) -> dict:
	"""Pick schema based on label prefix. Returns {} for no validation."""
	if "day_verify" in label or "night_verify" in label or "verify" in label:
		return VERIFY_SCHEMA
	if label.startswith("P_") or label.startswith("J_") or label.startswith("R_"):
		return PRJ_RESULT_SCHEMA
	if "handoff" in label:
		return HANDOFF_SCHEMA
	return {}

def _log_schema_warnings(data: dict, label: str, model: str) -> None:
	"""Validate parsed JSON against schema; log warnings without aborting."""
	schema = _schema_for_label(label)
	if not schema:
		return
	errs = validate_schema(data, schema)
	if errs:
		log(f"  Schema warnings ({label}): {{'; '.join(errs[:5])}}")
		save_dlq(json.dumps(data, ensure_ascii=False), stage=label + "_schema",
		         model=model, error="; ".join(errs[:3]), attempt=1)

def _dedup_findings(findings, threshold=0.92):
    """Deduplicate findings by cosine similarity of description+file embeddings.

    P findings 중복 제거 — Reflector가 동일한 finding을 반복 검토하지 않도록 필터링.
    all-MiniLM-L6-v2 임베딩 사용 (extract.py와 동일), 384-dim.
    """
    if len(findings) < 2:
        return findings
    try:
        embedder = SentenceTransformer("all-MiniLM-L6-v2")
        texts = [f"{f.get('description','')} {f.get('file','')}" for f in findings]
        embs = embedder.encode(texts, normalize_embeddings=True)
        keep = []
        for i in range(len(findings)):
            if all(sum(embs[i] * embs[j]) < threshold for j in keep):
                keep.append(i)
        return [findings[i] for i in keep]
    except Exception as e:
        log(f"  Dedup failed (proceeding without): {e}")
        return findings





# ── System prompts ─────────────────────────────────────────────────────

PROPOSER_SYSTEM_PROMPT = """You are a code review specialist. Analyze the evaluation findings below. Identify bugs, security issues, data loss risks, and edge cases.

CRITICAL — Every finding MUST include "file" field (filename or area). Evidence grounding is required:
each issue must cite a specific file so the Reflector can verify it against real code.

When all P, R, J agree quickly, pay EXTRA attention — the most critical bugs are often missed by consensus.

=== EVALUATION RUBRIC (self-assessment) ===
Rate your OWN findings on these criteria:
- Correctness (0-10): Is each finding a real, verifiable issue?
- Actionability (0-10): Is there a clear fix or mitigation?
- Evidence (0-10): Is it backed by specific data or code?
- Novelty (0-10): Does it add new insight?

Return JSON:
{
  "findings": [
    {"id": "F001", "severity": "critical|high|medium|low", "category": "bug|security|data_loss|performance|quality", "description": "1-3 sentence explanation", "file": "filename or area - REQUIRED", "line_range": "optional, e.g. 42-56"}
  ],
  "rubric_evaluation": {
    "correctness": 0-10,
    "correctness_justification": "why this score",
    "actionability": 0-10,
    "actionability_justification": "...",
    "evidence": 0-10,
    "evidence_justification": "...",
    "novelty": 0-10,
    "novelty_justification": "..."
  }
}"""

REFLECTOR_SYSTEM_PROMPT = """You are a review reflector. For each finding submitted by the Proposer, decide ACCEPT or REJECT. Be precise — if the finding is valid, ACCEPT it. If it is not a real issue or duplicates another, REJECT it.

Do not silently discard filtered findings. Store rejected findings alongside the reasoning for why they were excluded — the verifier needs to know what was rejected and why.

=== EVALUATION RUBRIC (self-assessment) ===
Rate your OWN verdicts on these criteria:
- Accuracy (0-10): Is each accept/reject decision correct?
- Reasoning (0-10): Is justification precise and specific?
- Efficiency (0-10): Are verdicts concise?

Return JSON:
{
  "verdicts": [
    {"id": "F001", "verdict": "accept", "reason": "concise justification"},
    {"id": "F002", "verdict": "reject", "reason": "concise justification"}
  ],
  "rejected_findings": [
    {"id": "F002", "severity": "high", "description": "one-line summary of the rejected finding", "rejection_reason": "why this was rejected, e.g. false positive, duplicate, low impact"}
  ],
  "rubric_evaluation": {
    "accuracy": 0-10,
    "accuracy_justification": "why this score",
    "reasoning": 0-10,
    "reasoning_justification": "...",
    "efficiency": 0-10,
    "efficiency_justification": "..."
  }
}"""

JUDGE_SYSTEM_PROMPT = """You are a Scoring Judge evaluating both the Proposer (P) and Reflector (R).

P_score = Correctness(0-10) + Coverage(0-10) + Precision(0-10) -> 0-30
R_score = Accuracy(0-10) + Efficiency(0-10) + Completeness(0-10) -> 0-30

=== EVALUATION RUBRIC (self-assessment) ===
Rate your OWN judging:
- Fairness (0-10): Are scores balanced and justified by the evidence?
- Clarity (0-10): Is the handoff document clear and actionable?
- Consistency (0-10): Do approved/rejected sets match the scores?

IMPORTANT — handoff rules:
- The "handoff" fields below must be derived ONLY from the actual approved/rejected
  decisions you just made (listed in "approved" and "rejected" arrays above).
- Do NOT add finding IDs that are not in your approved/rejected arrays.
- Do NOT fabricate or guess finding content.
- "unresolved_count" = len(rejected) — findings rejected by R are unresolved.
- "critical_remaining" = IDs of rejected findings that had severity "critical" or "high".
- "key_accepted"/"key_rejected" = first 5 of each, already in the arrays above.
- "findings_confidence" — score each finding 0-100 so the verifier can prioritize.
  High confidence (90+): well-supported, likely correct.
  Low confidence (<60): weak evidence, needs special verifier attention.

Return ONLY valid JSON — no markdown, no commentary.

Return JSON:
{
  "P_score": 0-30,
  "P_rubric": {"correctness": 0-10, "coverage": 0-10, "precision": 0-10},
  "R_score": 0-30,
  "R_rubric": {"accuracy": 0-10, "efficiency": 0-10, "completeness": 0-10},
  "decision": "APPROVED|REJECT",
  "consensus_score": 0-100,
  "approved": ["F001"],
  "rejected": [],
  "decisions": [{"id": "F001", "decision": "approved|rejected", "reason": "..."}],
  "findings_confidence": [
    {"id": "F001", "confidence": 85, "note": "brief rationale for this confidence score"}
  ],
  "rubric_evaluation": {
    "fairness": 0-10,
    "fairness_justification": "...",
    "clarity": 0-10,
    "clarity_justification": "...",
    "consistency": 0-10,
    "consistency_justification": "..."
  },
  "report": {
    "summary": "1-2 sentence overall assessment of this rotation",
    "top_issues": ["most critical finding in 1 line"],
    "quality_notes": {"strengths": ["..."], "weaknesses": ["..."]},
    "recommendation": "commit or escalate in 1 sentence"
  },
  "handoff": {
    "rotation_summary": "Brief state of findings after this rotation",
    "unresolved_count": <number>,
    "critical_remaining": ["F001"],
    "verifier_focus": ["area for verifier to double-check"],
    "key_accepted": ["F001"],
    "key_rejected": ["F002"]
  }
}"""

VERIFIER_SYSTEM_PROMPT = """You are a final verifier. Review all findings and P-R-J results.

You will receive THREE handoff documents:
1. [LLM-R] — R(night_reflector) handoff (comprehensive summary after full P-R-J cycle)
2. [Python] — deterministic handoff
3. [Python consolidated] — full rotation summary

Compare LLM-R vs Python. After your final verdict,
write detailed, actionable feedback per model+role:
e.g., P=night_proposer, R=night_reflector, J=night_judge — separate feedback for each.

=== EVALUATION RUBRIC (self-assessment) ===
Rate your OWN verification on these criteria:
- Thoroughness (0-10): Are all handoff documents compared and cross-checked?
- Evidence Check (0-10): Are verification items backed by specific data?
- Feedback Quality (0-10): Is per-role feedback actionable and constructive?

Return JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "action": "commit|revert|escalate",
  "confidence": 0-100,
  "summary": "1 sentence",
  "reasoning": "3-5 sentences",
  "verification_items": [{"check":"...","result":"pass|fail|partial","detail":"..."}],
  "rubric_evaluation": {
    "thoroughness": "0-10",
    "thoroughness_justification": "...",
    "evidence_check": "0-10",
    "evidence_check_justification": "...",
    "feedback_quality": "0-10",
    "feedback_quality_justification": "..."
  },
  "feedback": {
    "P": {"model":"night_proposer","role":"proposer","score":0,"strengths":[],"weaknesses":[],"improvements":[]},
    "R": {"model":"night_reflector","role":"reflector","score":0,"strengths":[],"weaknesses":[],"improvements":[]},
    "J": {"model":"night_judge","role":"judge","score":0,"strengths":[],"weaknesses":[],"improvements":[]}
  },
  "handoff_comparison": {
    "better_handoff": "llm_r|python|equal",
    "reason": "why one handoff source was more useful for verification",
    "llm_r_strengths": ["..."],
    "python_strengths": ["..."]
  }
}"""

SECONDARY_VERIFIER_SYSTEM_PROMPT = """You are an independent second-opinion verifier.

You will receive THREE handoff documents:
1. [LLM-J] — Judge LLM handoff
2. [Python] — deterministic handoff
3. [Python consolidated] — full rotation summary

Compare LLM-J vs Python. After your final verdict,
write detailed, actionable feedback per model+role:
e.g., P=night_proposer, R=night_reflector, J=night_judge — separate feedback for each.

Return JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "action": "commit|revert|escalate",
  "confidence": 0-100,
  "summary": "1 sentence",
  "reasoning": "3-5 sentences",
  "verification_items": [{"check":"...","result":"pass|fail|partial","detail":"..."}],
  "disagreement_with_primary": [{"issue":"...","primary_verdict":"...","my_verdict":"...","detail":"..."}],
  "feedback": {
    "P": {"model":"night_proposer","role":"proposer","score":0,"strengths":[],"weaknesses":[],"improvements":[]},
    "R": {"model":"night_reflector","role":"reflector","score":0,"strengths":[],"weaknesses":[],"improvements":[]},
    "J": {"model":"night_judge","role":"judge","score":0,"strengths":[],"weaknesses":[],"improvements":[]}
  },
  "handoff_comparison": {
    "better_handoff": "llm_j|python|equal",
    "reason": "why one handoff source was more useful for verification",
    "llm_j_strengths": ["..."],
    "python_strengths": ["..."]
  }
}"""

RUBRIC = """
## EVALUATION RUBRIC -- APPLY TO YOUR ROLE

### Proposer: rate each finding 0-10
- Correctness (0.35): Is it a real, verifiable issue?
- Actionability (0.30): Is there a clear fix or mitigation?
- Evidence (0.25): Is it backed by specific data or code?
- Novelty (0.10): Does it add new insight?

### Refuter: rate each verdict 0-10
- Accuracy (0.40): Is the accept/reject decision correct?
- Reasoning (0.30): Is the justification precise and specific?
- Efficiency (0.30): Is the verdict concise?

### Judge scoring rules
P = Correctness + Coverage + Precision (0-30)
R = Accuracy + Efficiency + Completeness (0-30)
gap <= 3 -> high consensus
gap > 8 -> escalate for review
consensus_score = 100 - (gap * 10)"""


# ── Phase 2: Rubric Evaluation (finding-level scores) ────────────────

RUBRIC_SYSTEM_PROMPT = """You are a rubric evaluation specialist. Assess each finding below against the standard criteria.

## Criteria (weighted)
- Correctness (0.35): Is this a real, verifiable issue?
- Actionability (0.30): Is there a clear fix or mitigation?
- Evidence (0.25): Is it backed by specific data or code?
- Novelty (0.10): Does it add new insight?

For each finding assign 0-10 per criterion with brief justification.
weighted_score = correctness*0.35 + actionability*0.30 + evidence*0.25 + novelty*0.10

Return ONLY valid JSON — no markdown, no commentary.
Schema:
{
  "rubric_evaluations": [
    {"id": "finding_id", "correctness": 0-10, "correctness_justification": "...",
     "actionability": 0-10, "actionability_justification": "...",
     "evidence": 0-10, "evidence_justification": "...",
     "novelty": 0-10, "novelty_justification": "...",
     "weighted_score": 0.00}
  ]
}"""


def rubric_evaluate_findings(findings, tag):
    """Phase 2: Evaluate each finding against rubric criteria (day_verify)."""
    log("\n--- Phase 2: Rubric Evaluation (finding-level) ---")
    if not findings:
        log("  No findings to evaluate — skipping rubric evaluation")
        return []

    # Build finding text for evaluation
    finding_lines = []
    for f in findings:
        fid = f.get("fid", f.get("id", "?"))
        desc = f.get("description", "").replace("\n", " ")[:200]
        sev = f.get("severity", "?")
        cat = f.get("category", "?")
        finding_lines.append(f"  [{sev}/{cat}] {fid}: {desc}")

    user_text = "Evaluate these findings against the rubric:\n\n" + "\n".join(finding_lines[:20])
    resp = call_one("day_verify", RUBRIC_SYSTEM_PROMPT, user_text, f"rubric_{tag}", max_tok=4096)
    rubrics = (resp or {}).get("result", {}).get("rubric_evaluations", [])

    # Build a lookup for quick access
    rubric_by_id = {r["id"]: r for r in rubrics if "id" in r}
    for f in findings:
        fid = f.get("fid", f.get("id", ""))
        if fid in rubric_by_id:
            f["rubric"] = rubric_by_id[fid]

    avg_score = 0.0
    if rubrics:
        scores = [r.get("weighted_score", 0) for r in rubrics if r.get("weighted_score") is not None]
        avg_score = sum(scores) / len(scores) if scores else 0.0

    log(f"  Evaluated {len(rubrics)} findings, avg weighted_score={avg_score:.2f}")
    return rubrics


# ── Phase 0: Python structure verification ─────────────────────────────

def python_verify(data, tag):
    log("\n--- Phase 0: Python 구조 검증 ---")
    findings_list = data.get("findings", [])
    total = len(findings_list)
    issues = []

    ids = [f.get("id", f.get("fid", f"idx_{i}")) for i, f in enumerate(findings_list)]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        issues.append({"check": "id_duplicates", "severity": "error",
                       "detail": f"Duplicate IDs: {dupes}"})
        log(f"  {FAIL} ID duplicates: {dupes}")
    else:
        log(f"  {PASS} All {total} IDs unique")

    REQUIRED = {"id", "severity", "category", "description"}
    missing = []
    for i, f in enumerate(findings_list):
        m = REQUIRED - set(f.keys())
        if m:
            missing.append((ids[i], m))
    if missing:
        issues.append({"check": "missing_fields", "severity": "error",
                       "detail": f"{len(missing)} findings missing fields: {missing}"})
        log(f"  {FAIL} {len(missing)} findings missing required fields")
    else:
        log(f"  {PASS} All {total} findings have required fields")

    VALID_SEV = {"critical", "high", "medium", "low", "pass", "fail", "partial"}
    invalid_severity = [(ids[i], f.get("severity", "?"))
               for i, f in enumerate(findings_list)
               if f.get("severity", "").lower() not in VALID_SEV]
    if invalid_severity:
        issues.append({"check": "invalid_severity", "severity": "warn", "detail": str(invalid_severity)})
        log(f"  {WARN} Invalid severities: {invalid_severity}")
    else:
        log(f"  {PASS} All severities valid")

    empty = [(ids[i], f.get("description", "")[:50])
             for i, f in enumerate(findings_list)
             if not f.get("description", "").strip()]
    if empty:
        issues.append({"check": "empty_description", "severity": "error",
                       "detail": f"{len(empty)} empty descriptions"})
        log(f"  {FAIL} {len(empty)} empty descriptions")
    else:
        log(f"  {PASS} All descriptions non-empty")

    expected = data.get("total_findings", 0)
    if expected and expected != total:
        issues.append({"check": "count_mismatch", "severity": "error",
                       "detail": f"meta={expected} actual={total}"})
        log(f"  {FAIL} Count mismatch: meta={expected} actual={total}")
    else:
        log(f"  {PASS} Finding count matches metadata ({total})")

    without_source = [ids[i] for i, f in enumerate(findings_list) if not f.get("source_file")]
    if without_source:
        log(f"  {WARN} {len(without_source)} findings missing source_file")

    severity_dist = {}
    for f in findings_list:
        s = f.get("severity", "unknown").lower()
        severity_dist[s] = severity_dist.get(s, 0) + 1
    log(f"  Severity distribution: {severity_dist}")

    result = {"total_findings": total, "issues_found": len(issues),
              "issues": issues, "severity_distribution": severity_dist}
    save(f"pyverify_{tag}", tag, result)
    log(f"  -> {len(issues)} issues, {total} findings checked")
    return result


# ── Model constants ─────────────────────────────────────────────────

MOCK_RESULT = {
    "result": {
        "findings": [
            {"id": "F001", "severity": "critical", "category": "bug", "description": "Mock finding for dry-run test", "file": "mock.py"},
            {"id": "F002", "severity": "high", "category": "security", "description": "Another mock finding", "file": "mock.py"},
        ],
        "rubric_evaluations": [
            {"id": "F001", "correctness": 8, "correctness_justification": "Real issue",
             "actionability": 7, "actionability_justification": "Clear fix",
             "evidence": 9, "evidence_justification": "Code evidence present",
             "novelty": 6, "novelty_justification": "Known pattern",
             "weighted_score": 7.65},
            {"id": "F002", "correctness": 5, "correctness_justification": "Unclear",
             "actionability": 4, "actionability_justification": "No mitigation",
             "evidence": 6, "evidence_justification": "Partial evidence",
             "novelty": 3, "novelty_justification": "Well known",
             "weighted_score": 4.75},
        ],
        "verdicts": [
            {"id": "F001", "verdict": "accept", "reason": "Valid dry-run finding"},
            {"id": "F002", "verdict": "reject", "reason": "Not reproducible in dry-run"},
        ],
        "P_score": 25, "R_score": 22,
        "P_rubric": {"correctness": 8, "coverage": 9, "precision": 8},
        "R_rubric": {"accuracy": 7, "efficiency": 8, "completeness": 7},
        "decision": "APPROVED", "consensus_score": 85,
        "approved": ["F001"], "rejected": ["F002"],
        "rubric_evaluation": {
            "fairness": 8, "fairness_justification": "Balanced scoring",
            "clarity": 7, "clarity_justification": "Clear report",
            "consistency": 8, "consistency_justification": "Consistent decisions",
        },
        "report": {
            "summary": "Mock dry-run report summary",
            "top_issues": ["F001: critical bug in mock.py"],
            "quality_notes": {"strengths": ["Good coverage"], "weaknesses": ["Limited data"]},
            "recommendation": "Commit after review",
        },
        "handoff": {
            "source": "dry_run_mock",
            "executive_summary": "Dry-run test handoff",
            "approved": [{"id": "F001", "severity":"critical","category":"bug","finding":"dry-run","approval_rationale":"test"}],
            "rejected": [{"id": "F002", "severity":"high","category":"security","finding":"dry-run","rejection_rationale":"test"}],
            "critical_remaining": [], "unresolved_count": 0,
            "verifier_priority": ["Check F001"], "quality_red_flags": [],
            "p_score": 25, "r_score": 22, "j_consensus": 85,
        },
        "handoff_comparison": {
            "better_handoff": "equal", "reason": "Both sources agree in dry-run",
            "llm_r_strengths": ["Rich descriptions"], "python_strengths": ["Deterministic counts"],
        },
        "final_verdict": "approved_with_conditions", "action": "commit", "confidence": 80,
        "summary": "Dry-run verification passed with conditions",
        "reasoning": "Mock reasoning for dry-run test",
        "verification_items": [{"check":"All findings verified","result":"pass","detail":"Mock verification"}],
        "feedback": {
            "P": {"model":"night_proposer","role":"proposer","score":80,"strengths":["Good coverage"],"weaknesses":["Needs more detail"],"improvements":["Add more context"]},
            "R": {"model":"night_reflector","role":"reflector","score":75,"strengths":["Accurate"],"weaknesses":["Brief reasoning"],"improvements":["Elaborate on rejections"]},
            "J": {"model":"night_judge","role":"judge","score":85,"strengths":["Fair"],"weaknesses":["Could be more detailed"],"improvements":["Add more rationale"]},
        },
    },
    "usage": {"prompt_tokens": 500, "completion_tokens": 200},
    "timings": {"prompt_per_second": 10, "predicted_per_second": 5},
    "elapsed_ms": 1500,
}

PROPOSER_MODEL = "night_proposer"
REFLECTOR_MODEL = "night_reflector"
JUDGE_MODEL = "night_judge"


# ── Model call with single-model-at-a-time guarantee ──────────────────

def call_one(model_name, sys_prompt, user_text, tag_label, max_tok=2048):
    """ensure_model -> LLM call. Day models skip restart if already healthy."""
    physical = resolve_model(model_name)
    if DRY_RUN:
        log(f"  [DRY] call_one({model_name}) → mock response")
        return MOCK_RESULT
    # Day models (reviewer/extractor) skip restart — Pod B stays running
    # Night models (proposer/reflector/judge/verifier) always restart for mode swap
    skip_if_healthy = physical not in NIGHT_MODELS
    ok = ensure_model(physical, skip_if_healthy=skip_if_healthy)
    if not ok:
        abort("컨테이너 시작 실패", model_name,
              f"{model_name} 컨테이너가 300s 내에 준비되지 않음")
    return llm_call(
        [{"role": "system", "content": sys_prompt},
         {"role": "user", "content": user_text}],
        physical, max_tokens=max_tok, label=tag_label)


# ── Python handoff compiler (deterministic, no hallucination) ────────────

def compile_handoff_single(r, round_num, with_rubric):
    """Python-compiled handoff from P-R-J result data."""
    n_approved = len(r.get("approved", []))
    n_rejected = len(r.get("rejected", []))
    handoff = {
        "source": "python_compiled",
        "p_model": r["p_model"],
        "r_model": r["r_model"],
        "j_model": r["j_model"],
        "round": round_num,
        "with_rubric": with_rubric,
        "P_score": r["P_score"],
        "R_score": r["R_score"],
        "consensus": r["consensus"],
        "decision": r["decision"],
        "approved_count": n_approved,
        "rejected_count": n_rejected,
        "approved_ids": sorted(r.get("approved", [])),
        "rejected_ids": sorted(r.get("rejected", [])),
        "report_summary": r.get("report_summary", ""),
        "r_rejected_findings": r.get("r_rejected_findings", []),
        "schema_version": 1,
    }
    handoff["checksum"] = hashlib.sha256(
        json.dumps(handoff, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]
    return handoff


def compile_handoff(prj_results, round_num, with_rubric):
    """Consolidated handoff from P-R-J results."""
    first_prj_result = prj_results[0] if prj_results else {}
    handoff = {
        "source": "python_consolidated",
        "round": round_num,
        "with_rubric": with_rubric,
        "P_score": first_prj_result.get("P_score", 0),
        "R_score": first_prj_result.get("R_score", 0),
        "consensus": first_prj_result.get("consensus", 0),
        "decision": first_prj_result.get("decision", ""),
        "total_approved": len(first_prj_result.get("approved", [])),
        "total_rejected": len(first_prj_result.get("rejected", [])),
        "all_approved_ids": sorted(first_prj_result.get("approved", [])),
        "all_rejected_ids": sorted(first_prj_result.get("rejected", [])),
        "report_summary": first_prj_result.get("report_summary", ""),
        "top_issues": first_prj_result.get("report_top_issues", [])[:5],
        "schema_version": 1,
    }
    handoff["checksum"] = hashlib.sha256(
        json.dumps(handoff, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]
    return handoff


# ── Round runner ──────────────────────────────────────────────────────

def run_propose_review_judge(state, tag, rubric_append):
    """P-R-J 1회 패스. Pod A stop → P → R → J → state 저장."""
    # Pod A(7B reviewer) stop — Pod B가 30B/14B로 전환되기 전 RAM 확보
    stop_pod_a()

    log("\n--- Phase 3: P-R-J (P) ---")
    handoff_fragment = {}

    # P — gets findings by severity + P context
    p_max = 4096
    proposer_output = call_one(PROPOSER_MODEL, PROPOSER_SYSTEM_PROMPT + rubric_append,
                   state.build_context("prj_proposer"),
                   f"P_{tag}", max_tok=p_max)
    save(f"p_{tag}", tag, proposer_output)
    p_findings = (proposer_output or {}).get("result", {}).get("findings", [])
    prev_count = len(p_findings)
    p_findings = _dedup_findings(p_findings)
    if len(p_findings) < prev_count:
        log(f"  Dedup: {prev_count} → {len(p_findings)} findings ({prev_count - len(p_findings)} removed)")

    # R
    r_max = 2048
    reflector_output = call_one(REFLECTOR_MODEL, REFLECTOR_SYSTEM_PROMPT + rubric_append,
                   f"Proposer findings:\n{json.dumps(p_findings, ensure_ascii=False, indent=2)[:4000]}",
                   f"R_{tag}", max_tok=r_max)
    save(f"r_{tag}", tag, reflector_output)
    r_verdicts = (reflector_output or {}).get("result", {}).get("verdicts", [])
    r_rejected = (reflector_output or {}).get("result", {}).get("rejected_findings", [])
    if r_rejected and len(r_rejected) > 0:
        log(f"  R rejected {len(r_rejected)} findings — stored in audit trail")

    # J
    j_max = 2048
    judge_output = call_one(JUDGE_MODEL, JUDGE_SYSTEM_PROMPT + rubric_append,
                   state.build_context("prj_judge", {"rotation_index": 0})
                   + f"\n\n### P findings:\n"
                   + json.dumps(p_findings, ensure_ascii=False, indent=2)[:2000]
                   + f"\n\n### R verdicts:\n"
                   + json.dumps(r_verdicts, ensure_ascii=False, indent=2)[:2000],
                   f"J_{tag}", max_tok=j_max)
    save(f"j_{tag}", tag, judge_output)

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
    slack_msg = (f"[P-R-J] *Round {state.round_num}*\n"
                 f"P={PROPOSER_MODEL}→{ps} | R={REFLECTOR_MODEL}→{rs} | J={JUDGE_MODEL}→consensus={cs}\n")
    if j_report.get("summary"):
        slack_msg += f"> {j_report['summary'][:120]}"
    slack_send(slack_msg)
    return prj_result, handoff_fragment, p_findings, r_verdicts


# ── R handoff writer ───────────────────────────────────────────────

HANDOFF_SYSTEM_PROMPT = """You are a senior reviewer (R) writing the final handoff document after a complete P-R-J review cycle.

The full cycle is complete:
- **P (night_proposer)**: Proposed findings with severity/category
- **You (R, night_reflector)**: Reviewed each finding — accepted or rejected
- **J (night_judge)**: Final scoring and consolidated decision

Your job: synthesize ALL of the above into a comprehensive handoff for the **final verifier (night_verify)**.

Grounding rules:
- ALL finding IDs must come from the actual data below. Do NOT fabricate.
- Approved items = accepted by R AND approved by J
- Rejected items = rejected by R OR rejected by J
- "critical_remaining" = IDs of rejected findings with severity critical/high
- Include specific severity, category, and rationale for each finding

Return ONLY valid JSON — no markdown, no commentary.

Schema:
{
  "handoff": {
    "source": "R_handoff",
    "executive_summary": "1-2 sentence overview of the full P-R-J cycle including key decisions",
    "approved": [
      {"id": "F001", "severity": "critical|high|medium|low", "category": "bug|security|...",
       "finding": "brief description (under 150 chars)",
       "approval_rationale": "why this was accepted by both R and J"}
    ],
    "rejected": [
      {"id": "F002", "severity": "...", "category": "...",
       "finding": "brief description",
       "rejection_rationale": "why this was rejected"}
    ],
    "critical_remaining": [],
    "unresolved_count": 0,
    "verifier_priority": [
      "specific item for verifier to double-check (with concrete reason and finding ID)"
    ],
    "quality_red_flags": ["systemic concern across multiple findings"],
    "p_score": 0-30,
    "r_score": 0-30,
    "j_consensus": 0-100,
    "rubric_evaluation": {
      "completeness": "0-10",
      "completeness_justification": "...",
      "accuracy": "0-10",
      "accuracy_justification": "...",
      "clarity": "0-10",
      "clarity_justification": "..."
    }
  }
}"""


def _trim_handoff(text, label=""):
    """Trim handoff document to essential fields only (prepill defense)."""
    try:
        data = json.loads(text) if isinstance(text, str) else text
    except (json.JSONDecodeError, TypeError):
        return str(text)[:2000]

    trimmed = {"_trimmed": True, "_source": label}

    # LLM-R handoff: {"source":"llm_r","handoff":{...}}
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

    # Python handoff: {"P_score":N, "decision":"...", ...}
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

    # Fallback: truncate
    s = json.dumps(data, ensure_ascii=False, indent=2)
    return s[:2000] + ("\n... (truncated)" if len(s) > 2000 else "")


def run_round(round_num, with_rubric, resume_state_path=None):
    tag = f"r{round_num}_{'rubric' if with_rubric else 'norubric'}"
    log(f"\n{'='*60}")
    log(f"ROUND {round_num}: {'WITH RUBRIC' if with_rubric else 'NO RUBRIC'}")
    log(f"{'='*60}")

    rubric_append = ""  # rubric disabled

    if resume_state_path:
        # Resume: load existing pipeline_state, skip Phase 0/1
        with open(resume_state_path) as f:
            state_data = json.load(f)
        data = load_input()
        state = PipelineState(round_num, with_rubric, data, existing_data=state_data)
        day_verify_result = state_data.get("day_verify", {})
        verify_result = state_data.get("python_verify", {})
        log("RESUME: 기존 state 로드, Python/day_verify 검증 건너뜀")
    else:
        data = load_input()
        state = PipelineState(round_num, with_rubric, data)

        # Phase 0: Python verify (no LLM)
        verify_result = python_verify(data, tag)
        state.add_phase("python_verify", verify_result)
        slack_send(f"[P-R-J] *Round {round_num}* Python verify: {verify_result['issues_found']} issues ({verify_result['total_findings']} findings)")

        # Phase 1: day_verify (lightweight pre-filter)
        log("\n--- Phase 1: day_verify ---")
        day_verify_resp = call_one("day_verify", VERIFIER_SYSTEM_PROMPT + rubric_append,
                       state.build_context("day_verify"),
                       f"day_verify_{tag}")
        day_verify_result = (day_verify_resp or {}).get("result", {})
        save(f"day_verify_{tag}", tag, day_verify_resp)
        day_verify_verdict = day_verify_result.get('final_verdict', '?')
        day_verify_confidence = day_verify_result.get('confidence', '?')
        log(f"  day_verify verdict={day_verify_verdict} confidence={day_verify_confidence}")
        state.add_phase("day_verify", day_verify_result)
        slack_send(f"[P-R-J] *Round {round_num}* day_verify: *{day_verify_verdict}* (confidence={day_verify_confidence})")

        # Phase 2: Rubric evaluation — disabled
        rubric_results = []
        log("  Rubric evaluation disabled")

    # Phase 3: P-R-J 1 pass (single fixed rotation)
    prj_result, handoff_fragment, p_findings, r_verdicts = run_propose_review_judge(state, tag, rubric_append)

    # ── Phase 3.5: R(night_reflector) writes the final handoff ──
    # R has full context: P findings, its own verdicts, and J's final decision.
    # R produces a comprehensive structured handoff for the night_verify verifier.
    log("\n--- Phase 3.5: R(night_reflector) writes final handoff ---")

    r_ctx_parts = [
        f"=== P-R-J CYCLE COMPLETE ===",
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
    save(f"handoff_r_{tag}", tag, {
        "source": "llm_r", "handoff": r_hoff_data,
        "p_findings_count": len(p_findings), "r_verdicts_count": len(r_verdicts),
        "model_metadata": {k: MODEL_METADATA.get(resolve_model(k)) for k in (PROPOSER_MODEL, REFLECTOR_MODEL, JUDGE_MODEL)}})

    # ── Save handoffs: 1 LLM-R + 1 Python ────────────
    handoff_models = {
        "P": {"key": PROPOSER_MODEL, **MODEL_METADATA.get(resolve_model(PROPOSER_MODEL), {})},
        "R": {"key": REFLECTOR_MODEL, **MODEL_METADATA.get(resolve_model(REFLECTOR_MODEL), {})},
        "J": {"key": JUDGE_MODEL, **MODEL_METADATA.get(resolve_model(JUDGE_MODEL), {})},
    }
    hoff_meta = json.dumps(handoff_models, ensure_ascii=False, indent=2)
    hoff_header = f"## Model Metadata (for future reference)\n{hoff_meta}\n\n"

    llm_save = {"source": "llm_r", "round": round_num,
                "r_model": REFLECTOR_MODEL, "handoff": r_hoff_data}
    save(f"handoff_llm_{tag}", tag, llm_save)
    llm_text = (hoff_header
                + f"## Handoff (R={model_info(REFLECTOR_MODEL)}) [LLM-R]\n"
                + json.dumps(llm_save, ensure_ascii=False, indent=2))
    log(f"  R handoff: {len(r_hoff_data.get('approved',[]))} approved, "
        f"{len(r_hoff_data.get('rejected',[]))} rejected")

    py_single = compile_handoff_single(prj_result, round_num, with_rubric)
    py_single["model_metadata"] = handoff_models
    save(f"handoff_py_{tag}", tag, py_single)
    py_text = f"## Handoff [Python]\n" + json.dumps(py_single, ensure_ascii=False, indent=2)
    pyc_text = json.dumps(py_single, ensure_ascii=False, indent=2)
    log(f"  -> 1 LLM-R + 1 Python handoffs saved")

    state.add_phase("handoffs", {"llm_texts": [llm_text], "py_texts": [py_text],
                                 "consolidated": py_single})

    # ── P-R-J phase complete ──
    log(f"\n{'='*60}")
    log(f"P-R-J 완료")
    log(f"{'='*60}")
    slack_send(f"[P-R-J] *Round {round_num}* P-R-J 완료. "
               f"결과: {prj_result.get('decision','?')} (consensus={prj_result.get('consensus','?')})")

    # ── STOP: PRJ complete. Now run night_verify ──
    summary = {
        "round": round_num, "with_rubric": with_rubric,
        "python_verify": {"issues_found": verify_result["issues_found"], "total": verify_result["total_findings"]},
        "day_verify": {"verdict": day_verify_result.get("final_verdict","?"), "confidence": day_verify_result.get("confidence",0)},
        "prj": [prj_result],
    }

    # ─────────────────────────────────────────────────────────────────
    # Phase 4: night_verify (production final gate)
    # ─────────────────────────────────────────────────────────────────
    # ── Trim handoffs for prepill defense ──────────────────────
    # Extract JSON portion from markdown-wrapped handoff texts
    def _extract_json_part(txt):
        """Return (header, json_str) from '## Header\\n{...}' handoff text."""
        brace = txt.find("{")
        if brace == -1:
            return txt, ""
        return txt[:brace], txt[brace:]

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

    log("\n--- Phase 4: night_verify ---")
    night_verify_context = state.build_context("final_verify") + "\n\n" + verifier_input
    night_verify_resp = call_one("night_verify", VERIFIER_SYSTEM_PROMPT + rubric_append,
                   night_verify_context, f"night_verify_{tag}")
    night_verify_result = (night_verify_resp or {}).get("result", {})
    save(f"night_verify_{tag}", tag, night_verify_resp)
    state.add_phase("night_verify", night_verify_result)
    night_verify_verdict = night_verify_result.get('final_verdict', '?')
    night_verify_confidence = night_verify_result.get('confidence', '?')
    handoff_comparison = night_verify_result.get('handoff_comparison', {})
    night_verify_feedback = night_verify_result.get('feedback', {})
    log(f"  night_verify verdict={night_verify_verdict} confidence={night_verify_confidence}")
    log(f"  night_verify handoff preference: {handoff_comparison.get('better_handoff', '?')}")
    for role_key, role_fb in night_verify_feedback.items():
        imp = role_fb.get("improvements", [])
        if imp:
            log(f"  feedback {role_key}: {imp[0][:80]}")
    slack_send(f"[P-R-J] *Round {round_num}* night_verify: *{night_verify_verdict}* "
               f"(conf={night_verify_confidence}) handoff={handoff_comparison.get('better_handoff','?')}")

    # ── Phase 5: Feedback loop — disabled ──
    fb_count = 0  # feedback disabled
    # Pull Phase 2 rubric results from state if available
    rubric_evals = state.data.get("rubric_evaluation", {}).get("evaluations", []) if not resume_state_path else []

    summary["night_verify"] = {"verdict": night_verify_result.get("final_verdict","?"), "confidence": night_verify_result.get("confidence",0),
                      "feedback": night_verify_feedback, "handoff_comparison": handoff_comparison,
                      "rubric_evaluation": night_verify_result.get("rubric_evaluation", {}),
                      "phase2_rubric": {"evaluations": rubric_evals,
                                        "count": len(rubric_evals)}}
    save(f"summary_{tag}", tag, summary)
    return summary



def _get_turn(turn_id):
    """Query turns table by UUID."""
    sql = (
        "SELECT id, user_turn, thinking, text, source_message_id, "
        "  created_at, conversation_id, seq "
        f"FROM turns WHERE id = '{esc_sql(turn_id)}'::uuid"
    )
    rows = psql_json(sql)
    if not rows:
        return None
    r = rows[0]
    return {
        "id": r["id"], "user_turn": r["user_turn"],
        "thinking": r.get("thinking") or None, "text": r["text"],
        "source_message_id": r.get("source_message_id", ""),
        "created_at": r["created_at"],
        "conversation_id": r["conversation_id"],
        "seq": r.get("seq", 0) or 0,
    }


def _get_facts(turn_id):
    """Query extracted facts for a turn from review_facts (non-marker)."""
    sql = (
        "SELECT fact_index, fact_type, evidence, extract_model, verdict "
        f"FROM review_facts "
        f"WHERE turn_id = '{esc_sql(turn_id)}'::uuid "
        f"  AND verdict != 'system' "
        "ORDER BY fact_index ASC"
    )
    rows = psql_json(sql)
    if not rows:
        return []
    facts = []
    for row in rows:
        facts.append({
            "fact_index": row.get("fact_index", 0) or 0,
            "fact_type": row.get("fact_type", ""),
            "evidence": row.get("evidence", ""),
            "extract_model": row.get("extract_model", ""),
            "verdict": row.get("verdict", ""),
        })
    return facts


def _read_pending_items(limit=5):
    """Read pending extract_results from activity_log, with optional day_review reference."""
    sql = (
        "SELECT al.id, al.body, al.title, al.summary, al.created_at, "
        "  dr.body as day_review_body "
        "FROM activity_log al "
        "LEFT JOIN LATERAL ("
        "  SELECT body FROM activity_log "
        "  WHERE type='day_review' "
        "    AND body->>'turn_id' = al.body->>'turn_id' "
        "  LIMIT 1"
        ") dr ON true "
        "WHERE al.queue_status='pending' AND al.type='extract_result' "
        "ORDER BY al.created_at ASC "
        f"LIMIT {limit}"
    )
    rows = psql_json(sql)
    if not rows:
        return []
    items = []
    for row in rows:
        body = row.get("body")
        if not isinstance(body, dict):
            continue
        items.append({
            "log_id": row.get("id", 0) or 0,
            "body": body,
            "title": row.get("title", ""),
            "summary": row.get("summary", ""),
            "created_at": row.get("created_at", ""),
            "day_review": row.get("day_review_body"),
        })
    return items


def _build_p_context(turn, facts, body, day_review=None):
    """Build P context from turn + facts + MCP metadata + optional day_review."""
    parts = [
        "=== TURN ===",
        f"User: {turn.get('user_turn', '')[:2000]}",
        f"Thinking: {(turn.get('thinking') or '')[:2000]}",
        f"Response: {(turn.get('text') or '')[:2000]}",
        "",
        f"=== EXTRACTED FACTS ({len(facts)}) ===",
    ]
    for f in facts:
        parts.append(f"  [{f.get('fact_type','?')}] {f.get('evidence','')[:300]}")
    mcp = body.get("mcp", {})
    if mcp:
        parts.extend(["", "=== MCP METADATA ===",
                      f"  tldr: {mcp.get('tldr', '')}",
                      f"  intent: {mcp.get('intent', '')}"])
        ents = mcp.get("entities", {})
        if ents:
            files = ents.get("files", [])[:5]
            funcs = ents.get("functions", [])[:5]
            parts.append(f"  entities: files={len(ents.get('files',[]))}, "
                         f"funcs={len(ents.get('functions',[]))}")
            if files:
                for f in files:
                    parts.append(f"    file: {f}")
            if funcs:
                for f in funcs:
                    parts.append(f"    func: {f}")
        tags = mcp.get("tags", [])
        if tags:
            parts.append(f"  tags: {tags[:10]}")

    if day_review:
        jr = day_review.get("J_results", {})
        dr_findings = day_review.get("P_results", [])
        dr_verdicts = day_review.get("R_results", [])
        parts.extend([
            "",
            "=== DAY PRE-REVIEW REFERENCE ===",
            f"  [note: day review by day_p+day_r, may contain hallucinations]",
            f"  P_score={jr.get('P_score','?')} R_score={jr.get('R_score','?')}",
            f"  decision={jr.get('decision','?')}",
            f"  approved={jr.get('approved',[])}",
            f"  rejected={jr.get('rejected',[])}",
        ])
        if dr_findings:
            parts.append(f"  Day findings ({len(dr_findings)}):")
            for f in dr_findings[:5]:
                parts.append(f"    [{f.get('severity','?')}] {f.get('description','')[:120]}")
        if dr_verdicts:
            parts.append(f"  Day verdicts ({len(dr_verdicts)}):")
            for v in dr_verdicts[:5]:
                parts.append(f"    {v.get('id','?')}: {v.get('verdict','?')}")
        parts.extend([
            "",
            "Perform your OWN independent review. Day results are reference only.",
            "Do NOT rely on day findings — verify everything yourself.",
            "",
        ])
    return "\n".join(parts)


def _batch_p(items, rubric_append):
    """P batch: load once, review all items."""
    log(f"\n--- P Batch Review ({len(items)} items) ---")
    if DRY_RUN:
        log("  [DRY] mock P batch")
        MOCK = [{"id": "M001", "severity": "medium", "category": "quality",
                  "description": "Dry-run P finding for extract review", "file": "extract"}]
        return [MOCK for _ in items]

    ok = ensure_model(PROPOSER_MODEL)
    if not ok:
        log(f"  FAILED to load {PROPOSER_MODEL}")
        return [[] for _ in items]

    results = []
    for idx, item in enumerate(items):
        turn_id = item["body"].get("turn_id", "")
        turn = _get_turn(turn_id)
        if not turn:
            log(f"  [{idx+1}/{len(items)}] Turn not found: {turn_id[:8]}")
            results.append([])
            continue
        facts = _get_facts(turn_id)
        log(f"  [{idx+1}/{len(items)}] {turn_id[:8]}: {len(facts)} facts")
        ctx = _build_p_context(turn, facts, item["body"], day_review=item.get("day_review"))
        resp = llm_call(
            [{"role": "system", "content": PROPOSER_SYSTEM_PROMPT + rubric_append},
             {"role": "user", "content": ctx}],
            model=PROPOSER_MODEL, max_tokens=4096, label=f"P_queue_{idx}")
        findings = resp.get("result", {}).get("findings", [])
        log(f"    P: {len(findings)} findings")
        results.append(findings)
    return results


def _batch_r(items, p_results, rubric_append):
    """R(night_reflector) batch: load once, reflect on all P findings."""
    log(f"\n--- R(night_reflector) Batch Reflection ({len(items)} items) ---")
    if DRY_RUN:
        log("  [DRY] mock R batch")
        MOCK = [{"id": "M001", "verdict": "accept", "reason": "Dry-run R verdict"}]
        return [MOCK for _ in items]

    ok = ensure_model(REFLECTOR_MODEL)
    if not ok:
        log(f"  FAILED to load {REFLECTOR_MODEL}")
        return [[] for _ in items]

    results = []
    for idx, (item, p_findings) in enumerate(zip(items, p_results)):
        if not p_findings:
            results.append([])
            continue
        ctx = f"Proposer findings:\n{json.dumps(p_findings, ensure_ascii=False, indent=2)[:4000]}"
        resp = llm_call(
            [{"role": "system", "content": REFLECTOR_SYSTEM_PROMPT + rubric_append},
             {"role": "user", "content": ctx}],
            model=REFLECTOR_MODEL, max_tokens=2048, label=f"R_queue_{idx}")
        verdicts = resp.get("result", {}).get("verdicts", [])
        log(f"    R: {len(verdicts)} verdicts")
        results.append(verdicts)
    return results


def _batch_j(items, p_results, r_results, rubric_append):
    """J(night_judge) batch: load once, score all P-R pairs."""
    log(f"\n--- J(night_judge) Batch Scoring ({len(items)} items) ---")
    if DRY_RUN:
        log("  [DRY] mock J batch")
        MOCK = {"P_score": 25, "R_score": 22, "decision": "APPROVED",
                "consensus_score": 85, "approved": ["M001"], "rejected": []}
        return [MOCK for _ in items]

    ok = ensure_model(JUDGE_MODEL)
    if not ok:
        log(f"  FAILED to load {JUDGE_MODEL}")
        return [None for _ in items]

    results = []
    for idx, (item, p_findings, r_verdicts) in enumerate(zip(items, p_results, r_results)):
        ctx_parts = [
            f"P findings ({len(p_findings)}):\n",
            json.dumps(p_findings, ensure_ascii=False, indent=2)[:2000],
            f"\nR verdicts ({len(r_verdicts)}):\n",
            json.dumps(r_verdicts, ensure_ascii=False, indent=2)[:2000],
        ]
        resp = llm_call(
            [{"role": "system", "content": JUDGE_SYSTEM_PROMPT + rubric_append},
             {"role": "user", "content": "\n".join(ctx_parts)}],
            model=JUDGE_MODEL, max_tokens=2048, label=f"J_queue_{idx}")
        jr = resp.get("result", {})
        log(f"    J: P_score={jr.get('P_score','?')} R_score={jr.get('R_score','?')} "
            f"decision={jr.get('decision','?')}")
        results.append(jr if jr.get("decision") in ("APPROVED", "REJECT") else None)
    return results


def run_queue_mode(limit=5):
    """Read pending extract_results, run batched P-R-J, set queue_status='reviewed'."""
    log("=" * 60)
    log("P-R-J Queue Consumer Mode")
    log("=" * 60)

    rubric_append = f"\n\n{RUBRIC}"
    items = _read_pending_items(limit)
    if not items:
        log("  No pending extract_results found")
        return {"processed": 0, "total": 0}

    log(f"  Found {len(items)} pending item(s)")
    p_results = _batch_p(items, rubric_append)
    r_results = _batch_r(items, p_results, rubric_append)
    j_results = _batch_j(items, p_results, r_results, rubric_append)

    processed = 0
    for idx, (item, judge_result) in enumerate(zip(items, j_results)):
        log_id = item["log_id"]
        if judge_result:
            divergence = (judge_result.get("P_score", 0) > 25
                          and len(judge_result.get("rejected", [])) > 3)
            body_update = {"prj_result": judge_result}
            if divergence:
                body_update["high_divergence"] = True
            psql_ok(
                f"UPDATE activity_log "
                f"SET queue_status='reviewed', "
                f"  body = body || '{json.dumps(body_update, ensure_ascii=False)}'::jsonb "
                f"WHERE id={log_id} AND queue_status='pending'"
            )
            log(f"  [{idx+1}/{len(items)}] Log#{log_id}: queue_status→reviewed "
                f"({judge_result.get('decision','?')})")
            processed += 1
        else:
            log(f"  [{idx+1}/{len(items)}] Log#{log_id}: SKIP (P-R-J incomplete), "
                f"will retry next night")

    log(f"\nQueue mode complete: {processed}/{len(items)} processed")
    return {"processed": processed, "total": len(items)}


# ── Main ──────────────────────────────────────────────────────────────

def run_extract(with_rubric, mcp_model="day_mcp"):
    """Phase -1: extract → Python verify.

    Pod A(7B reviewer:8082) 재시작 → day_verify/MCP 준비.
    Pod B(3B extractor:8080)는 running 상태 유지 (extract LLM call).
    skip_mcp=True면 MCP 생략 (extract 결과만 반환)."""
    log("\n--- Phase -1: extract (Pod A 7B reviewer 준비, Pod B 3B extractor running) ---")
    if DRY_RUN:
        log("  [DRY] Extract phase skipped")
        return

    log("  Pod A(7B reviewer:8082) 시작 → verify/MCP 준비, Pod B(3B extractor:8080) 유지...")
    ok = start_pod_a_only("day", 8082)
    if not ok:
        log("  Pod A not ready for extract")
        slack_send(":warning: Extract phase — Pod A not ready")
        return
    log("  :8082 ready (reviewer, verify/MCP 준비)")

    # Pod B(3B extractor:8080)가 running 상태인지 확인
    log("  Checking Pod B(3B extractor:8080) health...")
    try:
        req = urllib.request.Request("http://127.0.0.1:8080/health")
        with urllib.request.urlopen(req, timeout=5) as r:
            if r.status != 200:
                raise Exception(f"health status {r.status}")
    except Exception as e:
        log(f"  Pod B:8080 not healthy ({e}) — starting Pod B day mode...")
        from lib.pod_manager import start_pod_b
        ok2 = start_pod_b("day", 8080)
        if not ok2:
            log("  Pod B not ready — extract impossible")
            slack_send(":warning: Extract phase — Pod B(extractor) not ready")
            return
        log("  :8080 ready (extractor)")

    from pipelines.extract import extract_pipeline
    result = extract_pipeline(
        turn_id=None,
        dry_run=False,
        mcp_model=mcp_model,
        skip_mcp=True,
    )
    log(f"  Extract result: {result['processed']} processed, "
        f"{result['failed']} failed, {result['facts']} facts")

def main():
    preflight_checks("prj_cycle.py")
    if "--queue" in sys.argv:
        limit = 5
        for i, a in enumerate(sys.argv):
            if a == "--limit" and i + 1 < len(sys.argv):
                limit = int(sys.argv[i + 1])
        result = run_queue_mode(limit=limit)
        log(f"Queue mode complete: {result['processed']}/{result['total']} processed")
        return

    if RESUME_PRJ:
        log("RESUME PRJ MODE")
        log("기존 state 로드, Python/day_verify 건너뛰고 P-R-J 1회 패스부터 재개\n")
        resume_path = os.path.join(EXPER_DIR, "pipeline_state_r1_norubric.json")
        if not os.path.exists(resume_path):
            log(f"ERROR: resume state not found: {resume_path}")
            sys.exit(1)
        slack_send(":repeat: *P-R-J 재개* — day_verify 결과 유지, P-R-J 1회 패스부터 재시작")
        start_time = time.monotonic()
        r1 = run_round(1, with_rubric=False, resume_state_path=resume_path)
    else:
        log("P-R-J 고정 역할 실험 시작")
        log("파이프라인: 추출 → Python 검증 → day_verify → Rubric 평가 → P-R-J(night_proposer,night_reflector,night_judge) → night_verify → 피드백 루프(재검증)")
        log("메모리 관리: phase 전환마다 podman stop -> 필요한 컨테이너만 시작 (Pod B 순차 swap)")
        log("P-R-J: night_proposer + night_reflector + night_judge + day_verify + night_verify\n")
        slack_send(
            ":hammer: *P-R-J 고정 역할 실험 시작*\n"
            "추출 → Python검증 → day_verify → P-R-J → night_verify → 피드백루프 → 재검증"
        )
        start_time = time.monotonic()

        # Phase -1: day_extract
        if "--skip-extract" not in sys.argv:
            run_extract(with_rubric=False)
        else:
            log("  --skip-extract: extract phase skipped")

        r1 = run_round(1, with_rubric=False)

    r1_prj = r1.get("prj", [])

    elapsed_total = (time.monotonic() - start_time) / 60

    log(f"\n{'='*60}")
    log(f"P-R-J 고정 역할 실험 Round 1 완료 (runtime: {elapsed_total:.0f}분)")
    log(f"{'='*60}")
    for r in r1_prj:
        log(f"  P={r.get('p_model','?')}({r.get('P_score','?')})"
            f" R={r.get('r_model','?')}({r.get('R_score','?')})"
            f" J={r.get('j_model','?')} → consensus={r.get('consensus','?')} {r.get('decision','?')}")

    # Rubric evaluation summary
    rubric_meta = r1.get('night_verify', {}).get('phase2_rubric', {})
    rubric_evals_list = rubric_meta.get('evaluations', [])
    if rubric_evals_list:
        scores = [r.get('weighted_score', 0) for r in rubric_evals_list if r.get('weighted_score') is not None]
        if scores:
            low_n = sum(1 for s in scores if s < 5.0)
            log(f"  Rubric: avg={sum(scores)/len(scores):.2f} low(<5.0)={low_n}/{len(scores)}")
    night_verify_result = r1.get('night_verify', {})
    if night_verify_result:
        log(f"  night_verify: {night_verify_result.get('verdict','?')} (confidence={night_verify_result.get('confidence','?')})")

    slack_send(
        f"[P-R-J] *Round 1 완료* (control)\n"
        f"Python verify: {r1.get('python_verify',{}).get('issues_found',0)} issues\n"
        f"day_verify: {r1.get('day_verify',{}).get('verdict','?')} ({r1.get('day_verify',{}).get('confidence','?')})\n"
        f"P-R-J: {r1_prj[0].get('decision','?') if r1_prj else 'N/A'}\n"
        + (f"night_verify: {night_verify_result.get('verdict','?')} (conf={night_verify_result.get('confidence','?')})\n" if night_verify_result else "")
        + f"실행시간: {elapsed_total:.0f}분"
    )

    fpath = save("summary", "final", r1)

    log(f"\nResults: {EXPER_DIR}/")
    log(f"Summary: {fpath}")
    log("Done.")
    log("Done.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise  # allow abort() (sys.exit) to work normally
    except Exception as e:
        log(f"UNHANDLED ERROR: {e}")
        import traceback
        log(traceback.format_exc())
        slack_send(f":no_entry: *P-R-J 실험 중단* — 예상치 못한 오류\n> `{e}`")
        sys.exit(1)
