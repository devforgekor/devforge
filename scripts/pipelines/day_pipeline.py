#!/usr/bin/env python3
# Status: experimental
# Path: called by — day_runner.py (subprocess), night_cycle.sh (future)
"""Day Pipeline: extract → py_verify → activity_log 저장 (classify.py P-R-J 별도).

NOTE: Port assignments follow MODEL_METADATA standards.
Standard: Pod B(extractor:8082), Pod A(operator:8080)."""

Usage:
  python3 day_pipeline.py [--skip-extract] [--tag r1]
"""

import json, os, subprocess, sys
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.infra.container_manager import start_pod_a
from lib.pipeline_common import (
    PipelineState, load_input, log, log_phase_header, save,
)
from lib.pod_manager import start_pod_b, wait_health

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"


def python_verify(data, tag):
    """Phase 0: Python 구조 검증 (no LLM)."""
    log_phase_header("Phase 0: Python 구조 검증")
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
    save(f"pyverify_{tag}", tag, result)  # → data/pipeline_run/exp_pyverify_{tag}.json
    log(f"  -> {len(issues)} issues, {total} findings checked")
    return result

def main():
    tag = "r1"
    skip_extract = "--skip-extract" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--tag" and i + 1 < len(sys.argv):
            tag = sys.argv[i + 1]

    log("=" * 60)
    log("DAY PIPELINE")
    log(f"Tag: {tag}")
    log("=" * 60)

    # Ensure Pod A (reserved:8080) + Pod B (7B extractor:8082)
    log("Ensuring Pod A (reserved:8080)...")
    start_pod_a(120)
    log("Ensuring Pod B (7B extractor:8082)...")
    start_pod_b(120)

    # Load input
    data = load_input()
    state = PipelineState(1, False, data)
    log(f"  Input: {len(data.get('findings', []))} findings")

    # Phase -1: Extract → review_facts (DB) + activity_log type='extract_result' (DB)
    if not skip_extract:
        log_phase_header("Phase -1: Extract")
        from pipelines.extract import extract_pipeline
        result = extract_pipeline(
            turn_id=None, dry_run=False, mcp_model="day_mcp", skip_mcp=True,
        )
        log(f"  Extract result: {result['processed']} processed, "
            f"{result['failed']} failed, {result['facts']} facts")
    else:
        log("  --skip-extract: extract phase skipped")

    # Phase 0: Python verify
    verify_result = python_verify(data, tag)
    state.add_phase("python_verify", verify_result)

    # Save to activity_log (type='day_review') → DB
    log_phase_header("Saving day_review to activity_log (via classify.py P-R-J)")
    day_handoff = {
        "tag": tag,
        "pipeline_state_path": state.path,
        "phase": 0,
        "python_verify": verify_result,
        "schema_version": 2,
    }
    body_json = json.dumps(day_handoff, ensure_ascii=False).replace("'", "''")
    run_id = f"day_{tag}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    sql = (
        "INSERT INTO activity_log "
        "(type, source, title, summary, body, run_id, exec_status) "
        "VALUES ("
        f"'day_review', 'day_pipeline', 'Day Review: {tag}', "
        f"'py_verify={len(verify_result.get(\"issues\",[]))} issues', "
        f"'{body_json}'::jsonb, '{run_id}', 'DONE'"
        ")"
    )
    r = subprocess.run(
        ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
         "-d", "devforge_app", "-c", sql],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode == 0:
        log(f"  day_review saved (log_id via run_id={run_id})")
    else:
        log(f"  DB save failed (non-fatal): {r.stderr[:200]}")

    log(f"\n{'='*60}")
    log("DAY PIPELINE COMPLETE")
    log(f"{'='*60}")


if __name__ == "__main__":
    main()
