#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Feedback provider — extracts recent fix patterns from activity_log and returns
them as few-shot message arrays for injection into LLM conversations.

Architecture::

    call_llm(messages, model)
      → _inject_feedback(messages, model)
        → get_feedback_for_model(model)
          → _fetch_review_results()     # SELECT activity_log
          → _extract_findings()          # gold_standard / edge_case
          → _check_rollback()            # quality gate
          → _patterns_to_messages()      # [user, assistant] pairs

Gold Standard rollback::

    Each new pattern set is a "generation". If pass rate drops below the
    previous generation's rate, patterns auto-revert. The state file at
    ``data/feedback_state.json`` tracks generational performance.

    Edge cases are framed as self-test challenges rather than warnings.
"""

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


FEEDBACK_WINDOW_HOURS = 48
MAX_CONTENT_LENGTH = 300

SEVERITY_ORDER: Dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}
STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "feedback_state.json")
STATE_FILE = os.path.abspath(STATE_FILE)

# Rollback: if current gen's pass rate drops below previous gen's by this margin
ROLLBACK_THRESHOLD = 0.05       # 5% absolute drop triggers revert
ROLLBACK_MIN_SAMPLES = 10       # min verification_items to trust a rate



def _pattern_fingerprint(patterns: List[Dict]) -> str:
    """Deterministic hash of pattern content. Same patterns → same fingerprint."""
    raw = json.dumps(patterns, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _load_state() -> Dict:
    """Load feedback generation state from JSON file."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"generation": 0, "active_fingerprint": None, "generations": {}}


def _save_state(state: Dict):
    """Persist feedback generation state to JSON file."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _get_pass_rate() -> Optional[float]:
    """Measure pass rate from recent verify_result.verification_items.

    Queries done items with verify_result from the last FEEDBACK_WINDOW_HOURS.
    Returns ratio of 'pass' to total items, or None if insufficient data.
    """
    import subprocess as sp

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=FEEDBACK_WINDOW_HOURS)).isoformat()
    sql = (
        "SELECT body::text FROM activity_log "
        "WHERE queue_status = 'done' "
        "  AND type IN ('review', 'debate_result', 'verify_result', 'extract_result') "
        f"  AND created_at > '{cutoff}'::timestamptz "
        "  AND body->'verify_result' IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 50"
    )
    r = sp.run(
        ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
         "-d", "devforge_app", "--no-align", "--tuples-only", "--quiet", "-c", sql],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None

    total = 0
    passed = 0
    for line in r.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            body = json.loads(line)
        except json.JSONDecodeError:
            continue
        v_result = body.get("verify_result", {})
        if not isinstance(v_result, dict):
            continue
        items = v_result.get("verification_items", [])
        if not isinstance(items, list):
            continue
        for vi in items:
            result = vi.get("result", "")
            total += 1
            if result == "pass":
                passed += 1

    if total < ROLLBACK_MIN_SAMPLES:
        return None
    return passed / total


def _register_generation(patterns: List[Dict]) -> str:
    """Register a new pattern generation if fingerprint differs from active.

    Returns the active fingerprint (may be new or previous).
    """
    state = _load_state()
    fp = _pattern_fingerprint(patterns)
    active = state.get("active_fingerprint")

    if fp == active:
        return fp  # same patterns, nothing to do

    # Is this a genuinely new set?
    existing = state.get("generations", {})
    if fp in existing:
        # Previously seen — just switch to it
        state["active_fingerprint"] = fp
        _save_state(state)
        return fp

    # New patterns — create generation record
    prev_rate = None
    prev_fp = active
    if prev_fp and prev_fp in existing:
        prev_rate = existing[prev_fp].get("pass_rate")

    state.setdefault("generations", {})[fp] = {
        "patterns": patterns,
        "pass_rate": None,
        "sample_size": 0,
        "prev_fingerprint": prev_fp,
        "prev_rate": prev_rate,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    state["active_fingerprint"] = fp
    state["generation"] = state.get("generation", 0) + 1
    _save_state(state)
    return fp


def _check_rollback(active_fp: str) -> Optional[str]:
    """Check if current generation underperforms previous. If so, revert.

    Returns fingerprint to use (may be the active one or a rolled-back one).
    """
    state = _load_state()
    gens = state.get("generations", {})
    gen = gens.get(active_fp)
    if not gen:
        return active_fp

    prev_fp = gen.get("prev_fingerprint")
    if not prev_fp or prev_fp not in gens:
        return active_fp  # no predecessor to compare

    prev = gens[prev_fp]
    prev_rate = prev.get("pass_rate")
    if prev_rate is None:
        return active_fp  # no baseline

    # Measure current gen's pass rate
    current_rate = _get_pass_rate()
    if current_rate is None:
        return active_fp  # not enough data yet

    # Update state with latest measurement
    gen["pass_rate"] = current_rate
    gen["sample_size"] = max(gen.get("sample_size", 0), ROLLBACK_MIN_SAMPLES)
    _save_state(state)

    if current_rate >= prev_rate - ROLLBACK_THRESHOLD:
        return active_fp  # performance acceptable

    # Rollback! Enqueue analysis request, then switch to previous generation
    print(f"  FEEDBACK ROLLBACK: gen {active_fp[:8]} pass_rate={current_rate:.1%} "
          f"< prev {prev_fp[:8]} pass_rate={prev_rate:.1%} (threshold {ROLLBACK_THRESHOLD:.0%})")

    _enqueue_rollback_analysis(active_fp, prev_fp, current_rate, prev_rate, gen)
    state["active_fingerprint"] = prev_fp
    _save_state(state)
    return prev_fp


def _enqueue_rollback_analysis(
    failed_fp: str, prev_fp: str,
    current_rate: float, prev_rate: float,
    gen: Dict,
) -> bool:
    """Enqueue an analysis_request so review_worker + review_consumer
    identify why the new patterns caused regression."""
    patterns = gen.get("patterns", [])
    prev_gen = _load_state().get("generations", {}).get(prev_fp, {})
    prev_patterns = prev_gen.get("patterns", [])

    body = {
        "analysis_type": "feedback_rollback",
        "failed_fingerprint": failed_fp,
        "failed_pass_rate": round(current_rate, 3),
        "prev_fingerprint": prev_fp,
        "prev_pass_rate": round(prev_rate, 3),
        "failed_patterns": [
            {"issue": p["issue"], "fix": p["fix"], "classification": p.get("classification", "")}
            for p in patterns
        ],
        "previous_patterns": [
            {"issue": p["issue"], "fix": p["fix"], "classification": p.get("classification", "")}
            for p in prev_patterns
        ],
    }
    import subprocess as sp
    body_json = json.dumps(body, ensure_ascii=False).replace("'", "''")
    title = f"feedback rollback: {failed_fp[:8]} ({current_rate:.0%}) < {prev_fp[:8]} ({prev_rate:.0%})"
    sql = (
        "INSERT INTO activity_log "
        "(type, source, title, summary, body, model, summary_status, queue_status, exec_status) "
        "VALUES ("
        f"'analysis_request', 'feedback', '{title.replace(chr(39), chr(39)+chr(39))}', "
        f"'Gold Standard pass_rate dropped from {prev_rate:.0%} to {current_rate:.0%}', "
        f"'{body_json}', 'deepseek-v4-flash', 'raw', 'reviewed', 'DONE'"
        ")"
    )
    r = sp.run(
        ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
         "-d", "devforge_app", "-c", sql],
        capture_output=True, text=True, timeout=15,
    )
    ok = r.returncode == 0
    if ok:
        print(f"  Rollback analysis enqueued (analysis_request)")
    return ok



def _fetch_review_results(since_hours: int = FEEDBACK_WINDOW_HOURS) -> List[Dict[str, Any]]:
    """Fetch completed review/verify results from activity_log.

    Uses --csv output to safely handle JSON body containing pipes/commas.
    """
    import csv
    import io
    import subprocess as sp

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    sql = (
        f"SELECT id, type, title, body::text, model "
        f"FROM activity_log "
        f"WHERE queue_status = 'done' "
        f"  AND type IN ('review', 'verify_result', 'debate_result', 'extract_result')"
        f"  AND created_at > '{cutoff}'::timestamptz "
        f"ORDER BY created_at DESC "
        f"LIMIT 30"
    )
    r = sp.run(
        ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
         "-d", "devforge_app", "--csv", "-c", sql],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return []

    results: List[Dict[str, Any]] = []
    for row in csv.DictReader(io.StringIO(r.stdout)):
        if not row:
            continue
        try:
            body = json.loads(row["body"]) if row.get("body") else {}
        except (json.JSONDecodeError, KeyError):
            body = {}
        results.append({
            "id": row.get("id", "").strip(),
            "type": row.get("type", "").strip(),
            "title": row.get("title", "").strip(),
            "body": body,
            "model": row.get("model", "").strip(),
        })
    return results



def _extract_findings(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract patterns from review result bodies.

    classification='gold_standard' → correct behavior to reinforce
    classification='edge_case'     → tricky situations, model weaknesses
    """
    patterns: List[Dict[str, Any]] = []
    for r in results:
        body = r.get("body", {})
        if not body:
            continue

        # Primary: findings / approved_findings (known issues → edge cases)
        for key in ("findings", "approved_findings"):
            findings = body.get(key, [])
            if not isinstance(findings, list):
                continue
            for f in findings:
                if not isinstance(f, dict):
                    continue
                desc = f.get("description", "") or f.get("summary", "")
                fix = f.get("fix", "") or f.get("solution", "") or f.get("diff", "")
                severity = f.get("severity", "medium")
                category = f.get("category", "general")
                if desc:
                    patterns.append({
                        "issue": desc[:MAX_CONTENT_LENGTH],
                        "fix": fix[:MAX_CONTENT_LENGTH] if fix else "Applied fix as described.",
                        "severity": severity,
                        "category": category,
                        "classification": "edge_case",
                    })

        # Secondary: quality_notes / feedback_notes → edge cases
        for key in ("quality_notes", "feedback_notes"):
            notes = body.get(key, [])
            if not isinstance(notes, list):
                continue
            for note in notes:
                if isinstance(note, dict) and note.get("issue"):
                    patterns.append({
                        "issue": note["issue"][:MAX_CONTENT_LENGTH],
                        "fix": note.get("fix", "Addressed.")[:MAX_CONTENT_LENGTH],
                        "severity": "low",
                        "category": "quality",
                        "classification": "edge_case",
                    })

        # Tertiary: direct issue/resolution → edge case
        if body.get("issue") and body.get("resolution"):
            patterns.append({
                "issue": body["issue"][:MAX_CONTENT_LENGTH],
                "fix": body["resolution"][:MAX_CONTENT_LENGTH],
                "severity": body.get("severity", "medium"),
                "category": body.get("category", "general"),
                "classification": "edge_case",
            })

        # Quaternary: verification_items (pass → gold, fail/partial → edge)
        vi_sources = [body.get("verification_items", [])]
        verify_result = body.get("verify_result", {})
        if isinstance(verify_result, dict):
            vi_sources.append(verify_result.get("verification_items", []))
        for items in vi_sources:
            if not isinstance(items, list):
                continue
            for vi in items:
                if not isinstance(vi, dict):
                    continue
                check = vi.get("check", "") or vi.get("description", "")
                detail = vi.get("detail", "") or vi.get("explanation", "")
                result = vi.get("result", "partial")
                severity_map = {"pass": "low", "fail": "critical", "partial": "medium"}
                severity = severity_map.get(result, "medium")
                if check:
                    patterns.append({
                        "issue": check[:MAX_CONTENT_LENGTH],
                        "fix": detail[:MAX_CONTENT_LENGTH] if detail else "Verified.",
                        "severity": severity,
                        "category": "verification",
                        "classification": "gold_standard" if result == "pass" else "edge_case",
                    })

        # Quininary: deepseek_audit (agree → gold, disagree → edge)
        deepseek_audit = body.get("deepseek_audit", {})
        if isinstance(deepseek_audit, dict):
            for fi in deepseek_audit.get("feedback_items", []):
                if not isinstance(fi, dict):
                    continue
                check = fi.get("check", "") or fi.get("description", "")
                reason = fi.get("audit_reason", "")
                audit_result = fi.get("audit_result", "agree")
                severity = "high" if audit_result == "disagree" else "medium"
                category = "deepseek_audit"
                if check:
                    patterns.append({
                        "issue": check[:MAX_CONTENT_LENGTH],
                        "fix": (
                            reason[:MAX_CONTENT_LENGTH]
                            if reason
                            else "Addressed in re-verification."
                        ),
                        "severity": severity,
                        "category": category,
                        "classification": "gold_standard" if audit_result == "agree" else "edge_case",
                    })

        # Senary: analysis_result from 27B rollback analysis
        analysis_result = body.get("analysis_result", {})
        if isinstance(analysis_result, dict):
            for fi in analysis_result.get("findings", []):
                if not isinstance(fi, dict):
                    continue
                issue = fi.get("issue", "") or fi.get("problem", "")
                fix = fi.get("corrected_fix", "")
                if issue:
                    patterns.append({
                        "issue": issue[:MAX_CONTENT_LENGTH],
                        "fix": fix[:MAX_CONTENT_LENGTH] if fix else "Addressed in corrected patterns.",
                        "severity": "high",
                        "category": "analysis_result",
                        "classification": "edge_case",
                    })
            for vp in analysis_result.get("verified_patterns", []):
                if not isinstance(vp, dict):
                    continue
                issue = vp.get("issue", "")
                fix = vp.get("fix", "")
                if issue:
                    patterns.append({
                        "issue": issue[:MAX_CONTENT_LENGTH],
                        "fix": fix[:MAX_CONTENT_LENGTH] if fix else "Confirmed standard.",
                        "severity": "low",
                        "category": "analysis_result",
                        "classification": "gold_standard",
                    })

    return patterns



def _patterns_to_messages(
    patterns: List[Dict[str, Any]],
    max_gold: int = 2,
    max_edge: int = 2,
) -> List[Dict[str, str]]:
    """Convert patterns into user/assistant message pairs.

    Gold Standard first — "this is the correct approach, apply this standard".
    Edge Cases second — framed as self-test challenges that probe weaknesses.
    """
    if not patterns:
        return []

    # Dedup by issue
    seen: set = set()
    unique: List[Dict[str, Any]] = []
    for p in patterns:
        key = p["issue"][:100]
        if key not in seen:
            seen.add(key)
            unique.append(p)

    gold = [p for p in unique if p.get("classification") == "gold_standard"]
    edge = [p for p in unique if p.get("classification") != "gold_standard"]

    gold.sort(key=lambda p: SEVERITY_ORDER.get(p.get("severity", "medium"), 2))
    edge.sort(key=lambda p: SEVERITY_ORDER.get(p.get("severity", "medium"), 2))

    gold = gold[:max_gold]
    edge = edge[:max_edge]

    messages: List[Dict[str, str]] = []

    for p in gold:
        messages.append({
            "role": "user",
            "content": (
                f"[GOLD STANDARD] {p['issue']}\n"
                f"This is the correct approach. Apply this standard to similar cases."
            ),
        })
        messages.append({
            "role": "assistant",
            "content": f"Standard confirmed: {p['fix']}",
        })

    for p in edge:
        messages.append({
            "role": "user",
            "content": (
                f"[EDGE CASE CHALLENGE] {p['issue']}\n"
                f"Probe this case for weaknesses. If the model's logic is sound, "
                f"confirm it. If a flaw is found, explain how to fix it."
            ),
        })
        messages.append({
            "role": "assistant",
            "content": f"Challenge assessed. {p['fix']}",
        })

    return messages



def get_feedback_for_model(
    model: str,
    max_gold: int = 2,
    max_edge: int = 2,
) -> List[Dict[str, str]]:
    """Return few-shot message pairs from recent activity_log patterns.

    Includes automatic rollback: if a new pattern generation causes pass rate
    to drop, previous known-good patterns are restored. The caller never sees
    this — it gets whatever generation passes the quality gate.

    Args:
        model:     Key in MODEL_REGISTRY (e.g. "reviewer", "extractor").
        max_gold:  Max gold-standard examples to include.
        max_edge:  Max edge-case examples to include.

    Returns:
        List of {"role": …, "content": …} dicts (alternating user/assistant).
        Empty list if no feedback available.
    """
    try:
        results = _fetch_review_results()
    except Exception:
        return []

    if not results:
        return []

    patterns = _extract_findings(results)
    if not patterns:
        return []

    # Generation tracking + quality gate
    active_fp = _register_generation(patterns)
    active_fp = _check_rollback(active_fp)

    # If rollback happened, load the previous generation's patterns
    if active_fp != _pattern_fingerprint(patterns):
        state = _load_state()
        gen = state.get("generations", {}).get(active_fp, {})
        rolled_back = gen.get("patterns", [])
        if rolled_back:
            patterns = rolled_back

    return _patterns_to_messages(patterns, max_gold, max_edge)

