#!/usr/bin/env python3
# Status: production
# Path: none — library
"""
Nightly extraction faithfulness test.

Runs extraction on a fixed set of 15 turns against http://127.0.0.1:8082.
Measures faithfulness rate, stores results for regression tracking.

Usage:
  python3 nightly_extract_test.py                              # default: Qwen3-4B model label
  python3 nightly_extract_test.py --model-label LFM2-8B-A1B    # label for results file
  python3 nightly_extract_test.py --dry-run                    # no save
"""

import csv
import http.client as hc
import io
import json
import os
import sys
import time
import subprocess as sp
import re
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "extract_tests")
os.makedirs(RESULTS_DIR, exist_ok=True)

# Fixed 15 test turn IDs (manually selected for manageable length)
TEST_TURN_IDS = [
    "1c9d6db4-12bf-4173-82a4-2e2fecae7514",
    "f94822dd-d363-4ec4-a436-3d9e90a4fdac",
    "a0c1130d-361a-42c6-835a-698b7b5861fc",
    "192be626-3118-4e0d-a822-bc6bc87514bd",
    "5fb1b5d3-244a-4d66-8838-dbcb85989733",
    "6dcc8b92-665a-4fd3-b4fd-449a29a4f04c",
    "78f4bb68-b0e2-4d6a-ae63-77d83ce3c353",
    "237ad900-146d-4819-8535-800851ba1615",
    "a0487275-1295-4eb0-8993-af17fef00f3c",
    "e290568a-43a7-448b-804a-0d38e1d0ac7b",
    "a2044da0-31f4-49fc-bb84-7b206e8b39e1",
    "826f3463-3f12-43d4-97c7-a1807edc9b27",
    "fbd6a3af-3a05-45a6-a249-84b6d02c8fbb",
    "60f1bd2d-8679-4605-8afb-869fcf635de9",
    "a6f13194-1356-40ce-b67c-23f3febc3ea0",
]

EXTRACT_SYSTEM = """\
You are a fact extractor for a developer-assistant conversation turn.
Each turn has three parts: user_turn (the user's message), thinking (the
model's internal reasoning, may be empty), and text (the model's response).

Extract key factual statements that are EXPLICITLY present in the text.
Do NOT infer, summarize, or add information not present in the source.

Output STRICT JSON:
{
  "extractions": [
    {
      "fact_type": "user|thinking|text",
      "evidence": "Exact quote or close paraphrase from the source",
      "category": "requirement|decision|explanation|code|reasoning|other"
    }
  ]
}

Rules:
- fact_type must match which source field the evidence came from
- evidence must be directly traceable to the source text
- Skip thinking if it is empty or contains only formatting
- Extract at most 5 facts per fact_type
- If nothing extractable, return {"extractions": []}"""


def fetch_turns():
    ids_literal = ", ".join(f"'{tid}'::uuid" for tid in TEST_TURN_IDS)
    sql = (
        "SELECT id, user_turn, thinking, text, "
        "  LENGTH(text) AS text_len, "
        "  LENGTH(COALESCE(user_turn,'')) AS user_len, "
        "  LENGTH(COALESCE(thinking,'')) AS think_len "
        f"FROM turns WHERE id IN ({ids_literal})"
    )
    r = sp.run(
        ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
         "-d", "devforge_app", "--csv", "-c", sql],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0 or not r.stdout.strip():
        print("DB query failed"); sys.exit(1)

    turns = {}
    for row in csv.DictReader(io.StringIO(r.stdout)):
        turns[row["id"]] = {
            "user_turn": row.get("user_turn", ""),
            "thinking": row.get("thinking", ""),
            "text": row.get("text", ""),
            "text_len": int(row.get("text_len", 0)),
            "user_len": int(row.get("user_len", 0)),
            "think_len": int(row.get("think_len", 0)),
        }
    return turns


def call_llm(messages, timeout=600):
    body = json.dumps({"messages": messages, "temperature": 0.1, "max_tokens": 2048, "stream": False})
    try:
        conn = hc.HTTPConnection("127.0.0.1", 8082, timeout=timeout)
        conn.request("POST", "/v1/chat/completions", body, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        msg = data.get("choices", [{}])[0].get("message", {})
        raw = msg.get("content", "") or msg.get("reasoning_content", "")
        return raw, data.get("usage", {})
    except Exception as e:
        return None, {"error": str(e)}


def extract_json(raw):
    cleaned = re.sub(r"<think[^>]*>.*?</think>", "", raw, flags=re.DOTALL).strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if m:
        cleaned = m.group(1)
    if not cleaned.startswith("{"):
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            cleaned = m.group(0)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


def check_faithful(evidence, source):
    if not evidence or not source:
        return False
    return " ".join(evidence.split()).lower() in " ".join(source.split()).lower()


def run_test(model_label="Qwen3-4B"):
    print(f"# Nightly Extraction Faithfulness Test — {model_label}")
    print(f"# Time: {datetime.now(timezone.utc).isoformat()}")
    print(f"# Turns: {len(TEST_TURN_IDS)} fixed\n")

    turns_data = fetch_turns()
    results = []
    errors = 0

    for i, tid in enumerate(TEST_TURN_IDS):
        if tid not in turns_data:
            print(f"[{i+1}/{len(TEST_TURN_IDS)}] {tid[:12]} — SKIP (not found)")
            errors += 1
            continue

        d = turns_data[tid]
        label = f"T={d['text_len']}"
        if d["think_len"] > 100: label += f" Th={d['think_len']}"
        if d["user_len"] > 50: label += f" U={d['user_len']}"

        print(f"[{i+1}/{len(TEST_TURN_IDS)}] {tid[:12]} — {label}")
        sys.stdout.flush()

        parts = [
            "=== user_turn ===", d["user_turn"] or "(empty)", "",
            "=== thinking ===", d["thinking"] or "(empty)", "",
            "=== text ===", d["text"] or "(empty)",
        ]
        messages = [{"role": "system", "content": EXTRACT_SYSTEM},
                     {"role": "user", "content": "\n".join(parts)}]

        t0 = time.monotonic()
        raw, usage = call_llm(messages)
        elapsed = round(time.monotonic() - t0, 1)

        if raw is None:
            print(f"  ERROR: {usage.get('error', 'unknown')}\n")
            results.append({"turn_id": tid, "label": label, "status": "error", "error": usage.get("error", "?")})
            errors += 1
            continue

        parsed = extract_json(raw)
        if parsed is None:
            print(f"  ERROR: JSON parse fail\n")
            results.append({"turn_id": tid, "label": label, "status": "error", "error": "json_parse"})
            errors += 1
            continue

        extractions = parsed.get("extractions", [])
        if not isinstance(extractions, list):
            print(f"  ERROR: extractions not list\n")
            results.append({"turn_id": tid, "label": label, "status": "error", "error": "not_list"})
            errors += 1
            continue

        turn_results = []
        for ex in extractions:
            ft = ex.get("fact_type", "?")
            ev = ex.get("evidence", "")
            cat = ex.get("category", "?")
            src_map = {"user": d["user_turn"], "thinking": d["thinking"], "text": d["text"]}
            faithful = check_faithful(ev, src_map.get(ft, ""))
            turn_results.append({"fact_type": ft, "category": cat, "evidence_len": len(ev), "faithful": faithful})

        faithful_count = sum(1 for r in turn_results if r["faithful"])
        total = len(turn_results)
        rate = f"{faithful_count}/{total} ({faithful_count/max(total,1)*100:.0f}%)"
        print(f"  {elapsed}s | {usage.get('prompt_tokens',0)}P+{usage.get('completion_tokens',0)}C | Extr={rate}")

        for tr in turn_results:
            m = "+" if tr["faithful"] else "-"
            print(f"    [{m}] {tr['fact_type']}/{tr['category']}")

        results.append({
            "turn_id": tid, "label": label, "status": "ok",
            "elapsed": elapsed, "tokens_prompt": usage.get("prompt_tokens", 0),
            "tokens_completion": usage.get("completion_tokens", 0),
            "total": total, "faithful": faithful_count,
            "unfaithful": total - faithful_count,
            "details": turn_results,
        })
        print()

    # Summary
    ok_results = [r for r in results if r["status"] == "ok"]
    total_e = sum(r["total"] for r in ok_results)
    total_f = sum(r["faithful"] for r in ok_results)
    total_t = sum(r["elapsed"] for r in ok_results)

    summary = {
        "model": model_label,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "turns_total": len(TEST_TURN_IDS),
        "turns_ok": len(ok_results),
        "turns_error": errors,
        "total_extractions": total_e,
        "total_faithful": total_f,
        "faithfulness_rate": round(total_f / max(total_e, 1), 3),
        "total_elapsed_seconds": round(total_t, 1),
        "results": results,
    }

    print("=" * 60)
    print(f"Model        : {model_label}")
    print(f"Turns OK     : {len(ok_results)}/{len(TEST_TURN_IDS)} (errors: {errors})")
    print(f"Total Extr   : {total_e}")
    print(f"Faithful     : {total_f} ({total_f/max(total_e,1)*100:.1f}%)")
    print(f"Unfaithful   : {total_e - total_f} ({(total_e-total_f)/max(total_e,1)*100:.1f}%)")
    print(f"Avg per turn : {total_e/max(len(ok_results),1):.1f}")
    print(f"Total time   : {summary['total_elapsed_seconds']:.0f}s")
    print("=" * 60)

    # Save results
    safe_model = model_label.lower().replace(".", "_").replace("-", "_")
    fname = f"extract_test_{safe_model}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    fpath = os.path.join(RESULTS_DIR, fname)
    with open(fpath, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved: {fpath}")

    # Compare with previous result for same model
    prev = _find_previous(safe_model)
    if prev and prev.get("total_extractions"):
        prev_rate = prev.get("faithfulness_rate", 0)
        drop = prev_rate - summary["faithfulness_rate"]
        sign = "↓" if drop > 0 else "↑" if drop < 0 else "="
        print(f"Previous run ({prev.get('timestamp','?')[:10]}): "
              f"{prev.get('total_faithful',0)}/{prev.get('total_extractions',0)} "
              f"({prev_rate*100:.1f}%)")
        print(f"Change: {sign} {abs(drop)*100:.1f}pp")
        if drop > 0.05:
            print("WARNING: Faithfulness dropped >5% from previous run!")

    return summary


def _find_previous(safe_model):
    import glob
    pattern = os.path.join(RESULTS_DIR, f"extract_test_{safe_model}_*.json")
    files = sorted(glob.glob(pattern))
    if len(files) < 2:
        return None
    try:
        with open(files[-2]) as f:
            return json.load(f)
    except (json.JSONDecodeError, IndexError, IOError):
        return None


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Nightly extraction faithfulness test")
    ap.add_argument("--model-label", default="Qwen3-4B", help="Label for results (e.g. Qwen3-4B, LFM2-8B-A1B)")
    ap.add_argument("--dry-run", action="store_true", help="Print only, no save")
    args = ap.parse_args()

    result = run_test(args.model_label)
    if args.dry_run:
        print("\n(DRY RUN — results not saved)")
    return 0 if result["turns_error"] < len(TEST_TURN_IDS) else 1


if __name__ == "__main__":
    sys.exit(main())
