#!/usr/bin/env python3
# Status: experimental
# Path: none — test script
"""Re-run long/B-chunked-large only. Previous attempt failed due to stale code."""
import json, os, sys, time

SCRIPTS_DIR = "/opt/projects/server/scripts"
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(SCRIPTS_DIR)

from lib.common import log

exec(open("tests/compare_extract_approaches.py").read().split("# ── Main")[0])

u = TEST_TURNS["long"]["user_turn"]
t = TEST_TURNS["long"]["thinking"]
x = TEST_TURNS["long"]["text"]

log("--- long / B-chunked-large (sequential, max_chars=3000) ---")
res = extract_facts_chunked(u, t, x, PROMPT_C, max_chars=3000, timeout=1200)
ex = res.get("extractions", [])
by_type = {}
for e in ex:
    ft = e.get("fact_type", "unknown")
    by_type[ft] = by_type.get(ft, 0) + 1
log(f"  facts: {by_type} total={len(ex)}")
log(f"  time: {res.get('elapsed_s',0):.1f}s")
log(f"  chunks: {res.get('chunks', 'N/A')}")

report = f"data/eval/extract_compare_long_B.json"
with open(report, "w") as f:
    json.dump({"results": {"long": {"B-chunked-large": {
        "fact_types": by_type, "total_facts": len(ex),
        "elapsed_s": res.get("elapsed_s"), "chunks": res.get("chunks")}}}},
        f, indent=2, ensure_ascii=False)
log(f"Report: {report}")
