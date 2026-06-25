#!/usr/bin/env python3
"""Retry think_long_4k B (failed due to 8082 crash)."""
import json, os, sys, time

SCRIPTS_DIR = "/opt/projects/server/scripts"
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(SCRIPTS_DIR)

from lib.pod_manager import ensure_model
from pipelines.extract import _extract_for_turn
from lib.common import log

log("Reloading 8082 fresh...")
ensure_model('day-extractor', skip_if_healthy=False)

turn = {
    "id": "00000000-0000-0000-0000-000000000000",
    "user_turn": "이슈가 뭐야?",
    "thinking": "The user is asking about the issue. Let me trace through the error. The error occurred in the enrichment phase. The NLI server at port 8085 returned a 404 error. This is because we removed the DeBERTa-v3 NLI server from Pod A but forgot to update the enrich pipeline. The enrich.py was still calling _call_nli_server(port=8085) which doesn't exist anymore. We fixed this by switching to LLM self-verify NLI instead. The fix was to use call_llm with day_enrich model and max_tokens=16, temperature=0.0, with a 60s timeout. This actually works better because it catches logical contradictions that the reranker would miss. The reranker only measures topical relevance, not logical entailment.",
    "text": "NLI 서버 8085 미배포 문제였습니다. enrich.py가 존재하지 않는 DeBERTa-v3 서버를 호출하고 있었고, LLM self-verify로 교체했습니다.",
}

t0 = time.monotonic()
_, result, error = _extract_for_turn(turn)
elapsed = time.monotonic() - t0

if error:
    log(f"B result: {error}")
    log(f"B elapsed: {elapsed:.0f}s")
    ex = []
    by_type = {}
    if result and result.get("extractions"):
        ex = result["extractions"]
else:
    ex = result.get("extractions", []) if result else []
    by_type = {}
    for e in ex:
        ft = e.get("fact_type", "unknown")
        by_type[ft] = by_type.get(ft, 0) + 1

log(f"think_long_4k B: user={by_type.get('user',0)} think={by_type.get('thinking',0)} text={by_type.get('text',0)} total={len(ex)} time={elapsed:.0f}s")

report = {"think_long_4k": {"B": {"by_type": by_type, "total": len(ex), "elapsed_s": round(elapsed, 1)}}}
path = f"data/eval/extract_ab_think_long_4k_B_retry.json"
with open(path, "w") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)
log(f"Saved: {path}")
