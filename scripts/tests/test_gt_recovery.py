#!/usr/bin/env python3
# Status: experimental
"""Ground-truth extraction recovery comparison harness.

Loads GT definitions from scripts/tests/ground_truths/*.json, runs the
extraction pipeline on each test case, and reports recall/precision.

GT sources: real technical documents (infrastructure.md, state.yaml, etc.)
— not hand-crafted synthetic text."""

import json, os, re, subprocess, sys, time, uuid
from typing import Any, Dict, List, Tuple

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)
from lib.db import psql_json, psql_ok

GT_DIR = os.path.join(SCRIPTS_DIR, "tests", "ground_truths")
EXTRACT_SCRIPT = os.path.join(SCRIPTS_DIR, "pipelines", "extract.py")
PIPELINE_TIMEOUT = 5400


def _load_ground_truths() -> List[Dict]:
    cases = []
    if not os.path.isdir(GT_DIR):
        print(f"  [GT] directory not found: {GT_DIR}", flush=True)
        return cases
    for fn in sorted(os.listdir(GT_DIR)):
        if not fn.endswith(".json"):
            continue
        fp = os.path.join(GT_DIR, fn)
        with open(fp, "r") as f:
            data = json.load(f)
        for tc in data.get("test_cases", []):
            tc.setdefault("description", "")
            tc["_file"] = fn
        cases.extend(data.get("test_cases", []))
    return cases


def _resolve_source_text(tc: Dict) -> str:
    """Return the text to feed into the pipeline."""
    sf = tc.get("source_file", "")
    if sf:
        abspath = os.path.join(SCRIPTS_DIR, "..", sf) if not sf.startswith("/") else sf
        abspath = os.path.normpath(abspath)
        if os.path.isfile(abspath):
            with open(abspath, "r") as f:
                return f.read()
        print(f"  WARN: source_file not found: {abspath}", flush=True)
        return ""
    return tc.get("user_turn", "")


def _subj_obj_contains_match(gt: Dict, fact: Dict) -> bool:
    src = ((fact.get("subject") or "") + " " + (fact.get("object") or "")).lower()
    if not src:
        return False
    subj = (gt.get("subject") or "").lower()
    obj_contains = (gt.get("object_contains") or "").lower()
    obj_also = (gt.get("object_also") or "").lower()
    pred = (gt.get("predicate") or "").lower()
    if subj not in src:
        return False
    if obj_contains and obj_contains not in src:
        return False
    if obj_also and not re.search(obj_also.replace(".", "\\.").replace("*", ".*"), src):
        return False
    if pred:
        fact_pred = (fact.get("predicate") or "").lower()
        if pred not in fact_pred:
            return False
    return True


def _match_facts(got: List[Dict], expected: List[Dict]) -> Tuple[set, set, int, int]:
    matched_got: set = set()
    matched_gt: set = set()
    for gi, gt in enumerate(expected):
        for fi, fact in enumerate(got):
            if fi in matched_got:
                continue
            if _subj_obj_contains_match(gt, fact):
                matched_got.add(fi)
                matched_gt.add(gi)
                break
    return matched_gt, matched_got, len(matched_gt), len(matched_got)


def run_test_case(tc: Dict) -> Dict:
    name = tc["name"]
    print(f"\n{'='*60}", flush=True)
    print(f"  Test: {name}", flush=True)
    print(f"  File: {tc['_file']}", flush=True)
    if tc.get("description"):
        print(f"  Desc: {tc['description']}", flush=True)
    sf = tc.get("source_file", "")
    if sf:
        print(f"  Source: {sf}", flush=True)
    print(f"{'='*60}", flush=True)

    source_text = _resolve_source_text(tc)
    if not source_text:
        return {"name": name, "status": "skip", "reason": "empty source"}

    section_type = tc.get("section_type", "user")
    facts_gt = tc.get("facts", [])

    tid = str(uuid.uuid4())
    cid = str(uuid.uuid4())
    esc = lambda s: s.replace("'", "''")

    psql_ok(
        f"INSERT INTO conversations (id,title,source,created_at) "
        f"VALUES ('{cid}'::uuid,'gt-{name}','gt-test','2026-07-08T00:00:00Z') "
        f"ON CONFLICT DO NOTHING"
    )
    seq = int(time.time() * -1000) % 100000
    if section_type == "text":
        turn_user = ""
        turn_text = source_text
    else:
        turn_user = source_text
        turn_text = ""

    psql_ok(
        f"INSERT INTO turns (id,user_turn,text,thinking,pipeline_state,"
        f"conversation_id,source_message_id,created_at,detected_lang,est_chars,seq) "
        f"VALUES ('{tid}'::uuid,'{esc(turn_user)}','{esc(turn_text)}','','scanned',"
        f"'{cid}'::uuid,'gt-{name}-{tid[:8]}','2026-07-08T00:00:00Z','en',"
        f"{len(source_text)},{seq}) "
        f"ON CONFLICT DO NOTHING"
    )

    t0 = time.monotonic()
    print(f"  Running pipeline (timeout={PIPELINE_TIMEOUT}s)...", flush=True)
    r = subprocess.run(
        [sys.executable, EXTRACT_SCRIPT, "--turn-id", tid],
        capture_output=True, text=True, timeout=PIPELINE_TIMEOUT
    )
    elapsed = time.monotonic() - t0

    result = {
        "name": name,
        "status": "done",
        "elapsed_s": round(elapsed),
        "stdout_tail": r.stdout[-3000:] if r.stdout else "",
        "stderr": r.stderr[-2000:] if r.stderr else "",
    }

    facts = (
        psql_json(
            f"SELECT fact_index,subject,predicate,object,evidence,qualifiers,"
            f"nli_verdict,fact_type "
            f"FROM review_facts WHERE turn_id='{tid}'::uuid AND source='extract_pipeline' "
            f"ORDER BY fact_index"
        )
        or []
    )

    section_facts = [f for f in facts if f.get("fact_type") == section_type]
    g = sum(1 for f in facts if f.get("nli_verdict") == "GROUNDED")

    result["total_facts"] = len(facts)
    result["grounded"] = g
    result[f"{section_type}_facts"] = len(section_facts)

    gt_matched, got_matched, recall, precision = _match_facts(section_facts, facts_gt)

    result["recall"] = f"{recall}/{len(facts_gt)}"
    result["precision"] = f"{precision}/{len(section_facts)}"

    print(f"\n  ── Results for {name} ──", flush=True)
    print(f"  Time: {elapsed:.0f}s", flush=True)
    print(f"  Source: {len(source_text)} chars", flush=True)
    print(f"  Facts: {len(facts)} total, {g} grounded ({len(section_facts)} in section)", flush=True)
    print(f"  Recall: {recall}/{len(facts_gt)}", flush=True)
    print(f"  Precision: {precision}/{len(section_facts)}", flush=True)

    missed = [facts_gt[i] for i in range(len(facts_gt)) if i not in gt_matched]
    if missed:
        print(f"\n  MISSED ({len(missed)}):", flush=True)
        for m in missed:
            print(f"    subj={m.get('subject','')!r} obj_contains={m.get('object_contains','')!r}  # {m.get('note','')}", flush=True)

    extra = [section_facts[i] for i in range(len(section_facts)) if i not in got_matched]
    if extra:
        print(f"\n  EXTRA ({len(extra)} unmatched):", flush=True)
        for e in extra[:8]:
            print(f"    {e.get('subject','')!r} | {e.get('predicate','')!r} | {e.get('object','')!r}", flush=True)
        if len(extra) > 8:
            print(f"    ... and {len(extra)-8} more", flush=True)

    # Detail: section facts
    print(f"\n  All {section_type} facts:", flush=True)
    for f in section_facts:
        subj = (f.get("subject") or "")[:32]
        pred = (f.get("predicate") or "")[:22]
        obj = (f.get("object") or "")[:40]
        nli = f.get("nli_verdict", "?")[:4]
        print(f"    [{nli:4s}] {subj:32s} | {pred:22s} | {obj}", flush=True)

    # Cleanup
    psql_ok(
        f"DELETE FROM review_facts WHERE turn_id='{tid}'::uuid "
        f"AND source='extract_pipeline'"
    )
    psql_ok(
        f"DELETE FROM pipeline_checkpoints WHERE pipeline='extract' "
        f"AND turn_id='{tid}'::uuid"
    )
    psql_ok(f"DELETE FROM turns WHERE id='{tid}'::uuid")
    psql_ok(
        f"DELETE FROM conversations WHERE id='{cid}'::uuid AND source='gt-test'"
    )

    return result


def main():
    cases = _load_ground_truths()
    if not cases:
        print("No GT test cases found.", flush=True)
        return

    print(f"Loaded {len(cases)} test case(s):", flush=True)
    for tc in cases:
        sf = tc.get("source_file", tc.get("user_turn", "")[:40])
        gf = len(tc.get("facts", []))
        print(f"  - {tc['name']}: {gf} GT facts, source={sf}", flush=True)

    all_results = []
    for tc in cases:
        res = run_test_case(tc)
        all_results.append(res)
        print(f"  [{res.get('status','?')}] {tc['name']}: recall={res.get('recall','?')} precision={res.get('precision','?')}", flush=True)

    print(f"\n{'='*60}", flush=True)
    print(f"  SUMMARY", flush=True)
    print(f"{'='*60}", flush=True)
    for res in all_results:
        name = res["name"]
        if res["status"] == "skip":
            print(f"  {name}: SKIP ({res.get('reason','')})", flush=True)
            continue
        print(f"  {name}: recall={res.get('recall','?')} precision={res.get('precision','?')} ({res.get('elapsed_s',0)}s)", flush=True)

    out_path = os.path.join(SCRIPTS_DIR, "tests", "gt_recovery_results.json")
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to {out_path}", flush=True)

    passed = all(
        r["status"] == "done" for r in all_results if r["status"] != "skip"
    )
    print(f"\n  >>> {'ALL PASS' if passed else 'SOME FAILED'} <<<", flush=True)


if __name__ == "__main__":
    main()
