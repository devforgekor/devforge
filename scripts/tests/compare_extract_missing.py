#!/usr/bin/env python3
# Status: experimental
# Path: none — test script
"""Run missing extract approach comparisons: medium/C, long/A, long/C, long/B-large-chunk."""
import json, os, sys, time

SCRIPTS_DIR = "/opt/projects/server/scripts"
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(SCRIPTS_DIR)

from lib.common import log

# Reuse test data and prompts from compare_extract_approaches.py
exec(open("tests/compare_extract_approaches.py").read().split("# ── Main")[0])

log("=" * 70)
log("RESUME: missing extract combos (sequential chunked)")
log("=" * 70)

results = {}
for turn_label, turn_data in TEST_TURNS.items():
    if turn_label == "short":
        continue
    u, t, x = turn_data["user_turn"], turn_data["thinking"], turn_data["text"]
    results[turn_label] = {}

    combos = []
    if turn_label == "medium":
        combos.append(("C-prompt_only", lambda: extract_facts(u, t, x, PROMPT_C, timeout=900)))
    elif turn_label == "long":
        combos.append(("A-current", lambda: extract_facts(u, t, x, PROMPT_A, timeout=900)))
        combos.append(("C-prompt_only", lambda: extract_facts(u, t, x, PROMPT_C, timeout=900)))
        combos.append(("B-chunked-large", lambda: extract_facts_chunked(u, t, x, PROMPT_C, max_chars=3000, timeout=1200)))

    for approach_label, fn in combos:
        log(f"\n--- {turn_label} / {approach_label} ---")
        res = fn()
        ex = res.get("extractions", [])
        by_type = {}
        for e in ex:
            ft = e.get("fact_type", "unknown")
            by_type.setdefault(ft, 0)
            by_type[ft] += 1
        log(f"  facts: {by_type} total={len(ex)}")
        log(f"  time: {res.get('elapsed_s',0):.1f}s")
        if res.get("chunks", "N/A") != "N/A":
            log(f"  chunks: {res['chunks']}")
        results[turn_label][approach_label] = res

log("\n" + "=" * 70)
log("SUMMARY")
log("=" * 70)
for turn_label in ["medium", "long"]:
    for approach_label in ["A-current", "C-prompt_only", "B-chunked-large"]:
        r = results.get(turn_label, {}).get(approach_label, {})
        if not r:
            continue
        ex = r.get("extractions", [])
        by_type = {}
        for e in ex:
            ft = e.get("fact_type", "unknown")
            by_type[ft] = by_type.get(ft, 0) + 1
        log(f"{turn_label:<8} {approach_label:<15} user={by_type.get('user',0)} think={by_type.get('thinking',0)} text={by_type.get('text',0)} total={len(ex)} time={r.get('elapsed_s',0):.0f}s")

out = {"results": results}
report = f"data/eval/extract_compare_resume_{time.strftime('%Y%m%d_%H%M%S')}.json"
with open(report, "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
log(f"\nReport: {report}")
