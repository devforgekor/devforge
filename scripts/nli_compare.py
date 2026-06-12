#!/usr/bin/env python3
# Status: experimental
# Path: none — MiniCheck vs 7B LLM NLI comparison test
"""MiniCheck(flan-t5-large, 770M) vs Qwen2.5-Coder-7B NLI 비교.
동일 extraction 결과로 두 NLI 방식의 정확도/속도/일치율 비교."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['TOKENIZERS_PARALLELISM'] = 'false'

from pipelines.extract import (
    _parse_json, _cosine_faithfulness, SYSTEM_DAY_EXTRACT,
    _check_faithfulness, COSINE_FAITHFUL, COSINE_UNFAITHFUL,
)
from lib.llm_client import call_llm
from lib.db import psql_json
from minicheck.minicheck import MiniCheck

TURN_IDS = [
    "82a482c7-d19a-473e-bd8f-531a81a75490",
    "c4e2dedb-bc5c-4a39-b3b6-10df79738751",
    "616f67b5-431a-420d-9c12-60205853c491",
    "c1c1aac7-909f-4fbc-a328-a5ab2086bd2e",
    "43db506a-d6d1-44f4-b3a2-f6604e63bd3b",
]

# ── MiniCheck setup ─────────────────────────────────────────────
MINICHECK_SUPPORT_THRESHOLD = 0.3
NLI = MiniCheck(model_name="flan-t5-large", cache_dir="/opt/ai_data/models")

def minicheck_nli(evidence: str, source: str) -> dict:
    """MiniCheck NLI: ~2s/call."""
    t0 = time.monotonic()
    label, prob, _, _ = NLI.score(docs=[source], claims=[evidence])
    elapsed = time.monotonic() - t0
    return {
        "faithful": bool(label[0] == 1 and prob[0] >= MINICHECK_SUPPORT_THRESHOLD),
        "prob": round(prob[0], 3),
        "elapsed_s": round(elapsed, 2),
    }

# ── 7B NLI prompt ────────────────────────────────────────────
SYSTEM_NLI_EXTRACT = """You are a strict fact-checker. Determine if the EVIDENCE is directly supported by the SOURCE text.

Classification rules:
- ENTAILMENT: The evidence can be directly verified from the source text
- NEUTRAL: The evidence is not addressed or cannot be confirmed by the source
- CONTRADICTION: The evidence contradicts what the source says

Output STRICT JSON:
{"verdict": "ENTAILMENT|NEUTRAL|CONTRADICTION", "reason": "one-line explanation"}

Faithfulness: ENTAILMENT ONLY. NEUTRAL and CONTRADICTION are UNFAITHFUL."""

def llm_extract_nli(evidence: str, source: str) -> dict:
    """7B LLM NLI via Pod A :8082 (day_verify model). ~30-60s/call."""
    if not evidence or not source:
        return {"faithful": False, "verdict": "N/A", "reason": "empty input", "elapsed_s": 0}
    t0 = time.monotonic()
    try:
        meta = call_llm(
            [{"role": "system", "content": SYSTEM_NLI_EXTRACT},
             {"role": "user", "content": f"SOURCE:\n{source}\n\nEVIDENCE:\n{evidence}\n\nClassify: ENTAILMENT, NEUTRAL, or CONTRADICTION?"}],
            model="day_verify", max_tokens=128, temperature=0.0, timeout=120,
            json_mode=True, return_meta=True,
        )
        elapsed = time.monotonic() - t0
        raw = meta["content"]
        verdict, reason = "NEUTRAL", "parse_failed"
        try:
            parsed = json.loads(raw)
            verdict = parsed.get("verdict", "NEUTRAL").upper()
            reason = parsed.get("reason", "")
        except json.JSONDecodeError:
            pass
        return {
            "faithful": verdict == "ENTAILMENT",
            "verdict": verdict,
            "reason": reason,
            "elapsed_s": round(elapsed, 1),
            "usage": meta.get("usage", {}),
        }
    except Exception as e:
        elapsed = time.monotonic() - t0
        return {"faithful": False, "verdict": "ERROR", "reason": str(e), "elapsed_s": round(elapsed, 1)}

# ── Extraction ─────────────────────────────────────────────────
def extract_turn(turn):
    """Run day_extract on one turn."""
    ut, th, tx = turn["user_turn"] or "", turn["thinking"] or "", turn["text"] or ""
    parts = ["=== user_turn ===", ut, "", "=== thinking ===", th, "", "=== text ===", tx]
    t0 = time.monotonic()
    try:
        meta = call_llm(
            [{"role": "system", "content": SYSTEM_DAY_EXTRACT},
             {"role": "user", "content": "\n".join(parts)}],
            model="day_extract", max_tokens=512, temperature=0.0, timeout=900,
            json_mode=True, return_meta=True,
        )
        elapsed = time.monotonic() - t0
    except Exception as e:
        return {"error": str(e), "elapsed_s": round(time.monotonic()-t0, 1)}
    raw = meta["content"]
    parsed = _parse_json(raw, "day_extract", attempt=1)
    if not parsed:
        return {"error": "json_parse_failed", "elapsed_s": round(elapsed, 1)}
    extractions = parsed.get("extractions", [])
    if not isinstance(extractions, list):
        return {"error": "extractions not a list", "elapsed_s": round(elapsed, 1)}
    source_map = {"user": ut, "thinking": th, "text": tx}
    results = []
    for ex in extractions:
        ft = ex.get("fact_type", "")
        ev = ex.get("evidence", "")
        src = source_map.get(ft, "")
        # Only test NLI on ambiguous range (Tier 3 candidates)
        cos = _cosine_faithfulness(ev, src)
        needs_nli = cos < COSINE_FAITHFUL and cos >= COSINE_UNFAITHFUL
        mc = minicheck_nli(ev, src) if needs_nli else None
        llm = llm_extract_nli(ev, src) if needs_nli else None
        results.append({
            "fact_type": ft,
            "evidence": ev[:80],
            "cosine": round(cos, 3),
            "needs_nli": needs_nli,
            "minicheck": mc,
            "llm_7b": llm,
        })
    return {"turns": 1, "extractions": len(extractions), "results": results, "elapsed_s": round(elapsed, 1)}

# ── Report helpers ──────────────────────────────────────────────
def print_header(s):
    print(f"\n{'='*70}")
    print(f"  {s}")
    print(f"{'='*70}")

# ── Main ────────────────────────────────────────────────────────
print_header("MiniCheck vs 7B LLM NLI Comparison")
print(f"  Extractor: Qwen2.5-Coder-7B-Instruct.Q8_0 (Pod B :8082)")
print(f"  7B NLI:    Qwen2.5-Coder-7B-Instruct.Q8_0 (Pod A :8082, model=day_verify)")
print(f"  MiniCheck: flan-t5-large (770M, ~2s/call)")
print(f"  Thresholds: cos>={COSINE_FAITHFUL}=faithful, cos<{COSINE_UNFAITHFUL}=unfaithful, between→NLI")
print()

turns = []
for tid in TURN_IDS:
    rows = psql_json(f"SELECT t.id, t.user_turn, t.thinking, t.text FROM turns t WHERE t.id='{tid}'::uuid")
    if rows:
        turns.append(rows[0])
print(f"Loaded {len(turns)}/{len(TURN_IDS)} turns\n")

all_pairs = []
mc_total, llm_total = 0, 0
mc_faithful, llm_faithful = 0, 0
mc_elapsed, llm_elapsed = 0.0, 0.0
agree, disagree = 0, 0

for turn in turns:
    tid = turn["id"][:8]
    print(f"\n── [{tid}] ──")
    r = extract_turn(turn)
    if r.get("error"):
        print(f"  ERROR: {r['error']}")
        continue
    print(f"  Extractions: {r['extractions']} ({r['elapsed_s']}s)")
    for ex in r["results"]:
        if not ex["needs_nli"]:
            icon = "✅" if ex["cosine"] >= COSINE_FAITHFUL else "❌"
            print(f"    {icon} [{ex['fact_type']}] cos={ex['cosine']:.2f} — NLI skip ({'embed' if ex['cosine'] >= COSINE_FAITHFUL else 'embed_reject'}) {ex['evidence'][:50]}")
            continue
        mc, llm = ex["minicheck"], ex["llm_7b"]
        mc_f = mc["faithful"] if mc else "?"
        llm_f = llm["faithful"] if llm else "?"
        mc_verdict = "ENTAIL" if mc_f else "NEUTRL"
        llm_verdict = llm["verdict"] if llm else "?"
        same = (mc_f == llm_f) if (mc and llm) else None
        mc_t, llm_t = (mc["elapsed_s"] if mc else 0), (llm["elapsed_s"] if llm else 0)

        if same is True:
            agree += 1
        elif same is False:
            disagree += 1

        if mc_f is True: mc_faithful += 1
        if llm_f is True: llm_faithful += 1
        mc_elapsed += mc_t
        llm_elapsed += llm_t
        mc_total += 1
        llm_total += 1

        print(f"    ┌─ [{ex['fact_type']}] cos={ex['cosine']:.2f} {ex['evidence'][:50]}")
        print(f"    ├─ MC: {mc_verdict} (p={mc['prob']:.2f}, {mc_t}s)")
        print(f"    └─ 7B: {llm_verdict} ({llm_t}s) {'reason:'+llm['reason'][:40] if llm else ''}")
        if same is not None:
            tag = "✓ AGREE" if same else "✗ DISAGREE"
            print(f"       → {tag}")

# ── Summary ─────────────────────────────────────────────────────
print_header("SUMMARY")
nli_pairs = mc_total
disagree_pct = round(disagree / nli_pairs * 100, 1) if nli_pairs else 0
print(f"  NLI pairs tested: {nli_pairs}")
print(f"  Agreement: {agree}/{nli_pairs} ({round(100-disagree_pct,1)}%)")
print(f"  Disagreement: {disagree}/{nli_pairs} ({disagree_pct}%)")
print()
print(f"  MiniCheck (770M):")
print(f"    Faithful: {mc_faithful}/{mc_total} ({round(mc_faithful/mc_total*100) if mc_total else 0}%)")
print(f"    Avg time: {round(mc_elapsed/mc_total, 1) if mc_total else 0}s/call")
print(f"    Total: {round(mc_elapsed, 1)}s")
print()
print(f"  7B LLM (Qwen2.5-Coder-7B):")
print(f"    Faithful: {llm_faithful}/{llm_total} ({round(llm_faithful/llm_total*100) if llm_total else 0}%)")
print(f"    Avg time: {round(llm_elapsed/llm_total, 1) if llm_total else 0}s/call")
print(f"    Total: {round(llm_elapsed, 1)}s")
print()
print(f"  Speed ratio: 7B is {round((llm_elapsed/mc_elapsed) if mc_elapsed else 0, 1)}x slower than MiniCheck")
print(f"{'='*70}")
