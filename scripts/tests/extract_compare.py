#!/usr/bin/env python3
# Status: experimental
# Path: none — 3-model comparison: 3B Q8_0 vs 7B Q4_K_M vs 7B Q8_0
"""동일 5개 turn으로 추출 정확도/속도 비교 + MiniCheck NLI 검증"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ['TOKENIZERS_PARALLELISM'] = 'false'

# Same prompts and constants as extract.py
from pipelines.extract import (
    _parse_json, _cosine_faithfulness, SYSTEM_DAY_EXTRACT,
    COSINE_FAITHFUL, COSINE_UNFAITHFUL,
)
from lib.test_common import test_setup, test_heartbeat, test_complete, log, call_llm, psql_json

# NLI model (loaded once)
from minicheck.minicheck import MiniCheck
NLI = MiniCheck(model_name='flan-t5-large', cache_dir='/opt/ai_data/models')

TURN_IDS = [
    "82a482c7-d19a-473e-bd8f-531a81a75490",
    "c4e2dedb-bc5c-4a39-b3b6-10df79738751",
    "616f67b5-431a-420d-9c12-60205853c491",
    "c1c1aac7-909f-4fbc-a328-a5ab2086bd2e",
    "43db506a-d6d1-44f4-b3a2-f6604e63bd3b",
]

MINICHECK_SUPPORT_THRESHOLD = 0.3  # prob >= 0.3 = supported

def _minicheck_nli(evidence: str, source: str) -> dict:
    """MiniCheck NLI: returns {faithful, prob, elapsed_s}."""
    if not evidence or not source:
        return {"faithful": False, "prob": 0.0, "elapsed_s": 0}
    t0 = time.monotonic()
    label, prob, _, _ = NLI.score(docs=[source], claims=[evidence])
    elapsed = time.monotonic() - t0
    return {
        "faithful": label[0] == 1 and prob[0] >= MINICHECK_SUPPORT_THRESHOLD,
        "prob": round(prob[0], 3),
        "elapsed_s": round(elapsed, 2),
    }

def check_faithfulness(evidence: str, source: str) -> dict:
    """3-tier + MiniCheck NLI."""
    if not evidence or not source:
        return {"faithful": False, "score": 0, "method": "empty", "nli": None}

    # Tier 1: embedding cosine
    cos = _cosine_faithfulness(evidence, source)
    score = round(cos * 100, 1)
    if cos >= COSINE_FAITHFUL:
        return {"faithful": True, "score": score, "method": "embed", "nli": None}
    if cos < COSINE_UNFAITHFUL:
        return {"faithful": False, "score": score, "method": "embed", "nli": None}

    # Tier 2: MiniCheck NLI (replaces old 7B LLM prompt)
    nli_result = _minicheck_nli(evidence, source)
    return {
        "faithful": nli_result["faithful"],
        "score": score,
        "method": "nli" if nli_result["faithful"] else "nli_reject",
        "nli": nli_result,
    }

def extract_turn(turn):
    """Run day_extract on a single turn."""
    ut, th, tx = turn["user_turn"] or "", turn["thinking"] or "", turn["text"] or ""
    parts = ["=== user_turn ===", ut, "", "=== thinking ===", th, "", "=== text ===", tx]

    t0 = time.monotonic()
    try:
        meta = call_llm(
            [{"role": "system", "content": SYSTEM_DAY_EXTRACT},
             {"role": "user", "content": "\n".join(parts)}],
            model="day_extract",
            max_tokens=512, temperature=0.0, timeout=900,
            json_mode=True, return_meta=True,
        )
        elapsed = time.monotonic() - t0
    except Exception as e:
        return {"error": str(e), "elapsed_s": round(time.monotonic()-t0, 1)}

    raw = meta["content"]
    parsed = _parse_json(raw, "day_extract", attempt=1)
    result = {"elapsed_s": round(elapsed, 1), "parse_ok": parsed is not None,
              "usage": meta.get("usage", {})}

    if parsed:
        extractions = parsed.get("extractions", [])
        if not isinstance(extractions, list):
            result["error"] = f"extractions not a list"
            return result
        source_map = {"user": ut, "thinking": th, "text": tx}
        faithful, unfaithful, details, nli_time = 0, 0, [], 0
        for ex in extractions:
            ft = ex.get("fact_type", "")
            ev = ex.get("evidence", "")
            verdict = check_faithfulness(ev, source_map.get(ft, ""))
            if verdict.get("nli"):
                nli_time += verdict["nli"].get("elapsed_s", 0)
            details.append({
                "fact_type": ft, "evidence": ev[:80],
                "faithful": verdict["faithful"], "score": verdict["score"],
                "method": verdict["method"],
                "nli_prob": verdict.get("nli", {}).get("prob") if verdict.get("nli") else None,
            })
            if verdict["faithful"]:
                faithful += 1
            else:
                unfaithful += 1
        result["facts"] = len(extractions)
        result["faithful"] = faithful
        result["unfaithful"] = unfaithful
        result["details"] = details
        result["nli_time"] = round(nli_time, 1)
    else:
        result["error"] = "json_parse_failed"
    return result

# ── Main ──
TEST = test_setup("extract_compare", "3-model comparison with MiniCheck NLI verification")
model_file = os.environ.get('MODEL_FILE', '?')
print(f"{'='*70}")
print(f"Model: {model_file}  |  NLI=MiniCheck(flan-t5-large)  |  Pod A NLI=OFF")
print(f"{'='*70}")

turns = []
for turn_id in TURN_IDS:
    rows = psql_json(f"SELECT t.id, t.user_turn, t.thinking, t.text FROM turns t WHERE t.id='{turn_id}'::uuid")
    if rows:
        turns.append(rows[0])

print(f"Loaded {len(turns)}/{len(TURN_IDS)} turns\n")

all_stats = {"facts": 0, "faithful": 0, "unfaithful": 0, "elapsed": 0, "nli_time": 0, "failed": 0}
nli_total = 0
for turn in turns:
    turn_id = turn["id"][:8]
    ut_len = len(turn.get("user_turn") or "")
    th_len = len(turn.get("thinking") or "")
    tx_len = len(turn.get("text") or "")
    print(f"[{turn_id}] user={ut_len}ch think={th_len}ch text={tx_len}ch")

    r = extract_turn(turn)
    if r.get("error"):
        print(f"  ❌ ERROR: {r['error']}")
        all_stats["failed"] += 1
        continue

    f, u = r.get("faithful", 0), r.get("unfaithful", 0)
    elap = r.get("elapsed_s", 0)
    nli_t = r.get("nli_time", 0)
    print(f"  → {elap}s | {r.get('facts',0)} facts ({f}F/{u}U) | NLI={nli_t}s | parse={'OK' if r.get('parse_ok') else 'FAIL'}")

    all_stats["facts"] += r.get("facts", 0)
    all_stats["faithful"] += f
    all_stats["unfaithful"] += u
    all_stats["elapsed"] += elap
    all_stats["nli_time"] += nli_t

    for d in r.get("details", []):
        m = "✅" if d["faithful"] else "❌"
        nli_str = f" nli_p={d['nli_prob']:.2f}" if d.get("nli_prob") is not None else ""
        print(f"    {m} [{d['fact_type']}] m={d['method']} s={d['score']}{nli_str} {d['evidence'][:60]}")

    nli_total += sum(1 for d in r.get("details", []) if d.get("nli_prob") is not None)

print(f"\n{'='*70}")
n = len(turns) - all_stats["failed"]
if n > 0:
    acc = all_stats["faithful"] / (all_stats["faithful"] + all_stats["unfaithful"]) * 100 if (all_stats["faithful"] + all_stats["unfaithful"]) > 0 else 0
    print(f"SUMMARY: {n} turns, {all_stats['facts']} facts, "
          f"acc={acc:.0f}% ({all_stats['faithful']}F/{all_stats['unfaithful']}U), "
          f"avg={all_stats['elapsed']/n:.1f}s/turn, "
          f"{all_stats['facts']/n:.1f} facts/turn, "
          f"NLI={all_stats['nli_time']:.0f}s total (on {nli_total} calls)")
print(f"{'='*70}")
test_complete("extract comparison done")
