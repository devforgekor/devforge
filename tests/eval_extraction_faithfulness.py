#!/usr/bin/env python3
# Status: experimental
# Path: none — standalone runner (python3 eval_extraction_faithfulness.py)
"""Qwen3-4B extraction faithfulness test across 20 diverse turns."""
import csv
import http.client as hc
import io
import json
import sys
import time
import subprocess as sp
import re

EXTRACT_3B_SYSTEM = """\
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


def fetch_turns(count=20):
    """Fetch balanced turns from DB: variety of lengths, some with user/thinking."""
    sql = (
        "SELECT id, LENGTH(text) AS text_len, "
        "  LENGTH(COALESCE(user_turn,'')) AS user_len, "
        "  LENGTH(COALESCE(thinking,'')) AS think_len "
        "FROM turns "
        "WHERE LENGTH(text) BETWEEN 100 AND 5000 "
        "  AND text NOT LIKE '{%' "
        "  AND text NOT LIKE '|%' "
        "ORDER BY RANDOM()"
        f" LIMIT {count*3}"
    )
    r = sp.run(
        ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
         "-d", "devforge_app", "--csv", "-c", sql],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0 or not r.stdout.strip():
        print("DB query failed")
        sys.exit(1)

    rows = []
    for row in csv.DictReader(io.StringIO(r.stdout)):
        rows.append({
            "id": row["id"],
            "text_len": int(row["text_len"]),
            "user_len": int(row["user_len"]),
            "think_len": int(row["think_len"]),
        })

    # Stratified selection: 5 short-text, 5 med-text, 5 long-text, 5 with user/think
    short = [r for r in rows if 100 <= r["text_len"] < 500][:5]
    med = [r for r in rows if 500 <= r["text_len"] < 1200][:5]
    long = [r for r in rows if 1200 <= r["text_len"] <= 5000][:5]
    with_user = [r for r in rows if r["user_len"] > 50 and r["id"] not in {x["id"] for x in short + med + long}][:5]

    selected = short + med + long + with_user
    for s in selected:
        parts = []
        if s["text_len"]:
            parts.append(f"T={s['text_len']}")
        if s["think_len"] > 100:
            parts.append(f"Th={s['think_len']}")
        if s["user_len"] > 50:
            parts.append(f"U={s['user_len']}")
        s["label"] = ", ".join(parts) if parts else f"T={s['text_len']}"
        # Tag group for summary breakdown
        if s in short:
            s["group"] = "short"
        elif s in med:
            s["group"] = "med"
        elif s in long:
            s["group"] = "long"
        else:
            s["group"] = "user"
    return selected


def fetch_turn_text(turn_id):
    r = sp.run(
        ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
         "-d", "devforge_app", "--csv", "-c",
         f"SELECT user_turn, thinking, text FROM turns WHERE id = '{turn_id}'"],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    for row in csv.DictReader(io.StringIO(r.stdout)):
        return {
            "user_turn": row.get("user_turn", ""),
            "thinking": row.get("thinking", ""),
            "text": row.get("text", ""),
        }
    return None


def call_llm(messages, timeout=240):
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


def test_turn(ti):
    turn_id = ti["id"]
    data = fetch_turn_text(turn_id)
    if not data:
        return {"id": turn_id, "label": ti["label"], "_group": ti.get("group", ""), "error": "fetch failed"}

    user_turn = data["user_turn"]
    thinking = data["thinking"]
    text = data["text"]

    parts = [
        "=== user_turn ===", user_turn or "(empty)", "",
        "=== thinking ===", thinking or "(empty)", "",
        "=== text ===", text or "(empty)",
    ]
    messages = [
        {"role": "system", "content": EXTRACT_3B_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]

    t0 = time.monotonic()
    raw, usage = call_llm(messages)
    elapsed = time.monotonic() - t0

    if raw is None:
        return {"id": turn_id, "label": ti["label"], "_group": ti.get("group", ""), "error": f"LLM fail: {usage.get('error')}"}

    parsed = extract_json(raw)
    if parsed is None:
        return {"id": turn_id, "label": ti["label"], "_group": ti.get("group", ""), "error": "JSON parse fail", "raw": raw[:150]}

    extractions = parsed.get("extractions", [])
    if not isinstance(extractions, list):
        return {"id": turn_id, "label": ti["label"], "_group": ti.get("group", ""), "error": "extractions not list", "raw": raw[:150]}

    results = []
    for ex in extractions:
        ft = ex.get("fact_type", "?")
        ev = ex.get("evidence", "")
        cat = ex.get("category", "?")
        src_map = {"user": user_turn, "thinking": thinking, "text": text}
        faithful = check_faithful(ev, src_map.get(ft, ""))
        results.append({"fact_type": ft, "category": cat, "evidence": ev[:120], "evidence_len": len(ev), "faithful": faithful})

    faithful_count = sum(1 for r in results if r["faithful"])
    return {
        "id": turn_id,
        "_group": ti.get("group", ""),
        "label": ti["label"],
        "elapsed": round(elapsed, 1),
        "tokens_prompt": usage.get("prompt_tokens", 0),
        "tokens_completion": usage.get("completion_tokens", 0),
        "total": len(results),
        "faithful": faithful_count,
        "unfaithful": len(results) - faithful_count,
        "details": results,
        "unfaithful_list": [r for r in results if not r["faithful"]],
    }


def main():
    turns = fetch_turns(20)
    print("# =============================================")
    print(f"# Qwen3-4B Extraction Faithfulness Test")
    print(f"# Turns: {len(turns)}")
    print(f"# Short(100-500ch), Med(500-1200ch), Long(1200+ch), User(has user_turn)")
    print(f"# =============================================\n")

    results = []
    for i, ti in enumerate(turns):
        print(f"[{i+1}/{len(turns)}] {ti['id'][:12]} — {ti['label']}")
        sys.stdout.flush()
        r = test_turn(ti)
        results.append(r)

        if "error" in r:
            print(f"  ERROR: {r['error']}\n")
            continue

        rate = f"{r['faithful']}/{r['total']} ({r['faithful']/max(r['total'],1)*100:.0f}%)"
        print(f"  {r['elapsed']}s | {r['tokens_prompt']}P+{r['tokens_completion']}C | "
              f"Extr={rate}")
        for d in r["details"]:
            m = "+" if d["faithful"] else "-"
            print(f"    [{m}] {d['fact_type']}/{d['category']} ({d['evidence_len']}ch) {d['evidence'][:90]}")
        print()

    # Summary
    print("#" * 60)
    print("# SUMMARY")
    print("#" * 60)
    valid = [r for r in results if "error" not in r]
    total_e = sum(r["total"] for r in valid)
    total_f = sum(r["faithful"] for r in valid)
    print(f"Turns: {len(valid)}/{len(results)} successful")
    print(f"Total extractions: {total_e}")
    print(f"Faithful: {total_f} ({total_f/max(total_e,1)*100:.1f}%)")
    print(f"Unfaithful: {total_e - total_f} ({(total_e-total_f)/max(total_e,1)*100:.1f}%)")
    print(f"Avg per turn: {total_e/len(valid):.1f} extractions")
    print()

    # Breakdown by turn profile
    print("# BY TURN PROFILE")
    print(f"{'Profile':<18} {'Turns':>6} {'Extr':>6} {'F':>5} {'UF':>5} {'Rate':>7}")
    print("-" * 50)
    for g_label, g_key in [("Short (<500ch)", "short"), ("Med (500-1200ch)", "med"),
                            ("Long (1200+ch)", "long"), ("Has user_turn", "user")]:
        subset = [r for r in valid if r.get("_group") == g_key]
        if not subset:
            continue
        se = sum(r["total"] for r in subset)
        sf = sum(r["faithful"] for r in subset)
        print(f"{g_label:<18} {len(subset):>6} {se:>6} {sf:>5} {se-sf:>5} {sf/max(se,1)*100:>6.1f}%")

    # Comparative against Qwen2.5-Coder-3B historical
    print()
    print("# VS QWEN2.5-CODER-3B (previous session)")
    print("Turn 23eece7d: 3B=40%, Qwen3-4B=60% (+20pp)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
