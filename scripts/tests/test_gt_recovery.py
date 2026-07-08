#!/usr/bin/env python3
# Status: experimental
"""Ground-truth extraction recovery comparison harness.

Loads GT definitions from scripts/tests/ground_truths/*.json, runs the
extraction pipeline on each test case, and reports recall/precision per
section (user/text)."""

import json, os, re, subprocess, sys, time, uuid
from typing import Any, Dict, List, Optional, Tuple

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
            tc.setdefault("text", "")
            tc["_file"] = fn
        cases.extend(data.get("test_cases", []))
    return cases


def _fact_key(fact: Dict) -> str:
    subj = (fact.get("subject") or "").lower().strip()
    pred = (fact.get("predicate") or "").lower().strip()
    obj = (fact.get("object") or "").lower().strip()
    return f"{subj} | {pred} | {obj}"


def _subj_obj_match(gt: Dict, fact: Dict) -> bool:
    src = ((fact.get("subject") or "") + " " + (fact.get("object") or "")).lower()
    if not src:
        return False
    subj = (gt.get("subject") or "").lower()
    obj = (gt.get("object") or "").lower()
    obj_also = (gt.get("object_also") or "").lower()
    pred = (gt.get("predicate") or "").lower()
    if subj not in src:
        return False
    if obj and obj not in src:
        return False
    if obj_also:
        if not re.search(obj_also.replace(".", "\\.").replace("*", ".*"), src):
            return False
    if pred:
        fact_pred = (fact.get("predicate") or "").lower()
        if pred not in fact_pred:
            return False
    return True


def _match_facts(
    got: List[Dict], expected: List[Dict]
) -> Tuple[List[int], List[int], int]:
    matched_got: set = set()
    matched_gt: set = set()
    for gi, gt in enumerate(expected):
        for fi, fact in enumerate(got):
            if fi in matched_got:
                continue
            if _subj_obj_match(gt, fact):
                matched_got.add(fi)
                matched_gt.add(gi)
                break
    recall = len(matched_gt)
    precision = len(matched_got)
    return sorted(matched_gt), sorted(matched_got), recall, precision


def run_test_case(tc: Dict) -> Dict:
    name = tc["name"]
    print(f"\n{'='*60}", flush=True)
    print(f"  Test: {name}", flush=True)
    print(f"  File: {tc['_file']}", flush=True)
    if tc.get("description"):
        print(f"  Desc: {tc['description']}", flush=True)
    print(f"{'='*60}", flush=True)

    user_turn = tc.get("user_turn", "")
    text = tc.get("text", "")
    expected = tc.get("expected", {})
    expected_user = expected.get("user", [])
    expected_text = expected.get("text", [])
    total_expected = len(expected_user) + len(expected_text)

    if not user_turn:
        print(f"  SKIP: no user_turn", flush=True)
        return {"name": name, "status": "skip", "reason": "no user_turn"}

    tid = str(uuid.uuid4())
    cid = str(uuid.uuid4())
    esc = lambda s: s.replace("'", "''")

    psql_ok(
        f"INSERT INTO conversations (id,title,source,created_at) "
        f"VALUES ('{cid}'::uuid,'gt-{name}','gt-test','2026-07-08T00:00:00Z') "
        f"ON CONFLICT DO NOTHING"
    )
    seq = int(time.time() * -1000) % 100000
    psql_ok(
        f"INSERT INTO turns (id,user_turn,text,thinking,pipeline_state,"
        f"conversation_id,source_message_id,created_at,detected_lang,est_chars,seq) "
        f"VALUES ('{tid}'::uuid,'{esc(user_turn)}','{esc(text)}','','scanned',"
        f"'{cid}'::uuid,'gt-{name}-{tid[:8]}','2026-07-08T00:00:00Z','en',"
        f"{len(user_turn) + len(text)},{seq}) "
        f"ON CONFLICT DO NOTHING"
    )

    t0 = time.monotonic()
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

    user_facts = [f for f in facts if f.get("fact_type") == "user"]
    text_facts = [f for f in facts if f.get("fact_type") == "text"]
    g = sum(1 for f in facts if f.get("nli_verdict") == "GROUNDED")

    result["total_facts"] = len(facts)
    result["grounded"] = g
    result["user_facts"] = len(user_facts)
    result["text_facts"] = len(text_facts)

    # User section matching
    gt_user_matched, got_user_matched, user_recall, user_precision = _match_facts(
        user_facts, expected_user
    )

    # Text section matching
    gt_text_matched, got_text_matched, text_recall, text_precision = _match_facts(
        text_facts, expected_text
    )

    total_recall = user_recall + text_recall
    total_precision = user_precision + text_precision
    total_possible = total_expected

    result["recall"] = {
        "user": f"{user_recall}/{len(expected_user)}",
        "text": f"{text_recall}/{len(expected_text)}",
        "total": f"{total_recall}/{total_possible}",
    }
    result["precision"] = {
        "user": f"{user_precision}/{len(user_facts)}",
        "text": f"{text_precision}/{len(text_facts)}",
        "total": f"{total_precision}/{len(facts)}",
    }

    # Hallucination check
    hallu_keywords = ["i would", "i'd", "i am", "i have", "my request", "comprehensive review"]
    hallu = []
    for f in user_facts:
        src = ((f.get("subject") or "") + " " + (f.get("object") or "")).lower()
        for kw in hallu_keywords:
            if kw in src:
                hallu.append(f)
                break
    result["hallucinations"] = len(hallu)
    if hallu:
        result["hallucination_details"] = [
            f"{f.get('subject','')!r} | {f.get('predicate','')!r} | {f.get('object','')!r}"
            for f in hallu
        ]

    # Print report
    print(f"\n  ── Results for {name} ──", flush=True)
    print(f"  Time: {elapsed:.0f}s", flush=True)
    print(f"  Facts: {len(facts)} total, {g} grounded", flush=True)
    print(f"  User facts: {len(user_facts)}, Text facts: {len(text_facts)}", flush=True)
    print(f"", flush=True)
    print(f"  RECALL:", flush=True)
    print(f"    User: {user_recall}/{len(expected_user)}", flush=True)
    print(f"    Text: {text_recall}/{len(expected_text)}", flush=True)
    print(f"    Total: {total_recall}/{total_possible}", flush=True)
    print(f"", flush=True)
    print(f"  PRECISION:", flush=True)
    print(f"    User: {user_precision}/{len(user_facts)}", flush=True)
    print(f"    Text: {text_precision}/{len(text_facts)}", flush=True)
    print(f"    Total: {total_precision}/{len(facts)}", flush=True)

    if hallu:
        print(f"\n  ⚠ HALLUCINATIONS ({len(hallu)}):", flush=True)
        for h in hallu:
            print(f"    {h.get('subject','')!r} | {h.get('predicate','')!r} | {h.get('object','')!r}", flush=True)
    else:
        print(f"\n  ✅ No hallucinations", flush=True)

    # Missed GT facts
    missed_user = [expected_user[i] for i in range(len(expected_user)) if i not in gt_user_matched]
    missed_text = [expected_text[i] for i in range(len(expected_text)) if i not in gt_text_matched]
    if missed_user:
        print(f"\n  MISSED (user):", flush=True)
        for m in missed_user:
            loc = f"subj={m.get('subject','')!r} obj={m.get('object','')!r}"
            print(f"    {loc}  # {m.get('note','')}", flush=True)
    if missed_text:
        print(f"\n  MISSED (text):", flush=True)
        for m in missed_text:
            loc = f"subj={m.get('subject','')!r} obj={m.get('object','')!r}"
            print(f"    {loc}  # {m.get('note','')}", flush=True)

    # Extra facts (in extraction but not matched to any GT)
    extra_user = [user_facts[i] for i in range(len(user_facts)) if i not in got_user_matched]
    extra_text = [text_facts[i] for i in range(len(text_facts)) if i not in got_text_matched]
    if extra_user:
        print(f"\n  EXTRA (user, {len(extra_user)} unmatched):", flush=True)
        for e in extra_user[:5]:
            print(f"    {e.get('subject','')!r} | {e.get('predicate','')!r} | {e.get('object','')!r}", flush=True)
        if len(extra_user) > 5:
            print(f"    ... and {len(extra_user)-5} more", flush=True)
    if extra_text:
        print(f"\n  EXTRA (text, {len(extra_text)} unmatched):", flush=True)
        for e in extra_text[:5]:
            print(f"    {e.get('subject','')!r} | {e.get('predicate','')!r} | {e.get('object','')!r}", flush=True)
        if len(extra_text) > 5:
            print(f"    ... and {len(extra_text)-5} more", flush=True)

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
        exp = tc.get("expected", {})
        n_user = len(exp.get("user", []))
        n_text = len(exp.get("text", []))
        print(f"  - {tc['name']}: {n_user} user + {n_text} text GT facts", flush=True)

    all_results = []
    for tc in cases:
        res = run_test_case(tc)
        all_results.append(res)

    print(f"\n{'='*60}", flush=True)
    print(f"  SUMMARY", flush=True)
    print(f"{'='*60}", flush=True)
    for res in all_results:
        name = res["name"]
        if res["status"] == "skip":
            print(f"  {name}: SKIP ({res.get('reason','')})", flush=True)
            continue
        recall = res.get("recall", {})
        precision = res.get("precision", {})
        hallu = res.get("hallucinations", 0)
        print(
            f"  {name}: recall={recall.get('total','?')} "
            f"precision={precision.get('total','?')} "
            f"hallu={hallu} "
            f"({res.get('elapsed_s',0)}s)",
            flush=True,
        )

    # Save results
    out_path = os.path.join(SCRIPTS_DIR, "tests", "gt_recovery_results.json")
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to {out_path}", flush=True)

    # Final verdict
    passed = all(
        r["status"] == "done" for r in all_results if r["status"] != "skip"
    )
    print(f"\n  >>> {'ALL PASS' if passed else 'SOME FAILED'} <<<", flush=True)


if __name__ == "__main__":
    main()
