#!/usr/bin/env python3
# Status: experimental
# Path: none — library
"""3B Q8_0 vs Q4_K_M — extract faithfulness 비교 테스트"""
import csv, http.client, io, json, re, subprocess, sys, time

API = "http://127.0.0.1:8080/v1/chat/completions"
EXPER_DIR = "/opt/projects/server/data/experiment"

SYSTEM_EXTRACT_3B = """You are a fact extractor for a developer-assistant conversation turn.
Each turn has three parts: user_turn (the user's message), thinking (the
model's internal reasoning, may be empty), and text (the model's response).

Extract key factual statements that are EXPLICITLY present in the text.
Do NOT infer, summarize, or add information not present in the source.

=== EVALUATION RUBRIC (self-assessment) ===
Rate your OWN extractions on:
- Faithfulness (0-10): Is every extraction directly traceable to the source?
- Precision (0-10): Are extractions factual statements, not interpretations?
- Recall (0-10): Are all key facts captured (up to 5 per type)?
- Conciseness (0-10): Is evidence brief and to the point?

Output STRICT JSON:
{
  "extractions": [
    {
      "fact_type": "user|thinking|text",
      "evidence": "Exact quote or close paraphrase from the source",
      "category": "requirement|decision|explanation|code|reasoning|other"
    }
  ],
  "rubric_evaluation": {
    "faithfulness": "0-10",
    "faithfulness_justification": "...",
    "precision": "0-10",
    "precision_justification": "...",
    "recall": "0-10",
    "recall_justification": "...",
    "conciseness": "0-10",
    "conciseness_justification": "..."
  }
}

Rules:
- fact_type must match which source field the evidence came from
- evidence must be directly traceable to the source text
- Skip thinking if it is empty or contains only formatting
- Extract at most 5 facts per fact_type
- If nothing extractable, return {"extractions": []}"""

# Historical baseline (Qwen2.5-3B Q4_K_M)
BASELINE = {"faithfulness": 83.1, "total_extractions": 65, "turns": 15, "avg_per_turn": 4.3, "speed": 81.1}

# ── Fetch turns ──
def get_turns(limit=5):
    cmd = [
        "podman", "exec", "postgres", "psql",
        "-U", "devforge", "-d", "devforge_app",
        "--csv",
        "-c", f"SELECT id, user_turn, thinking, text FROM turns WHERE user_turn != '' AND text != '' ORDER BY random() LIMIT {limit}",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    reader = csv.DictReader(io.StringIO(r.stdout))
    return list(reader)

turns = get_turns(5)
print(f"Turns fetched: {len(turns)}")
print(f"{'='*60}")
print(f"3B Q8_0 Extract Faithfulness Test")
print(f"{'='*60}")
print(f"Baseline (Q4_K_M): {BASELINE['faithfulness']}% faithful, {BASELINE['avg_per_turn']}/turn, {BASELINE['speed']}s/turn\n")

all_results = []
for turn in turns:
    tid = turn["id"]
    user_turn = turn.get("user_turn") or ""
    thinking = turn.get("thinking") or ""
    text = turn.get("text") or ""

    user_msg = f"user_turn:\n{user_turn}\n\nthinking:\n{thinking}\n\ntext:\n{text}"
    body = {
        "messages": [
            {"role": "system", "content": SYSTEM_EXTRACT_3B},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": 1024,
        "temperature": 0.1,
        "stream": False,
    }

    print(f"  Turn {tid[:8]}... ({len(user_msg)} chars) ... ", end="", flush=True)
    t0 = time.time()

    conn = http.client.HTTPConnection("127.0.0.1", 8080, timeout=600)
    try:
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        conn.request("POST", "/v1/chat/completions", body_bytes,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw_resp = resp.read().decode("utf-8")
        raw = json.loads(raw_resp)
        elapsed = time.time() - t0
    except Exception as e:
        print(f"FAIL: {e}")
        conn.close()
        continue
    conn.close()

    content = (raw.get("choices") or [{}])[0].get("message", {}).get("content", "")
    usage = raw.get("usage", {})
    timings = raw.get("timings", {})
    speed = ""
    if timings:
        speed = f" (p:{timings.get('prompt_per_second',0):.0f}g:{timings.get('predicted_per_second',0):.0f}t/s)"
    print(f"OK {elapsed:.0f}s{speed}")

    # Parse JSON
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", content.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                print(f"    JSON parse failed, skipping")
                continue
        else:
            print(f"    No JSON found, skipping")
            continue

    extractions = data.get("extractions", [])
    if not extractions:
        print(f"    No extractions")
        all_results.append({"turn": tid, "extractions": 0, "faithful": 0, "speed": elapsed})
        continue

    # Faithfulness check
    source_map = {"user": user_turn, "thinking": thinking, "text": text}
    faithful = 0
    unfaithful = 0
    for ex in extractions:
        ft = ex.get("fact_type", "")
        ev = ex.get("evidence", "").lower().strip()
        src = source_map.get(ft, "").lower()
        if not ev or not src:
            unfaithful += 1
        elif ev in src or ev.rstrip(".,;:!?") in src:
            faithful += 1
        else:
            unfaithful += 1

    total = len(extractions)
    pct = faithful / total * 100 if total else 0
    print(f"    extractions={total}, faithful={faithful} ({pct:.0f}%), unfaithful={unfaithful}")
    all_results.append({"turn": tid, "extractions": total, "faithful": faithful, "unfaithful": unfaithful, "speed": elapsed, "usage": usage})

# Summary
print(f"\n{'='*60}")
print(f"결과: 3B Q8_0 vs Q4_K_M Extract")
print(f"{'='*60}")
print(f"\n{'':<30} {'Q4_K_M':<15} {'Q8_0':<15}")
print(f"{'-'*60}")

total_ex = sum(r["extractions"] for r in all_results)
total_faithful = sum(r["faithful"] for r in all_results)
total_unfaithful = sum(r["unfaithful"] for r in all_results)
total_turns = len(all_results)
avg_speed = sum(r["speed"] for r in all_results) / total_turns if total_turns else 0
overall_pct = total_faithful / total_ex * 100 if total_ex else 0

# Estimate Q8 per-turn (scale baseline by speed ratio)
q4_per_turn = BASELINE["avg_per_turn"]
q8_per_turn = total_ex / total_turns if total_turns else 0

print(f"{'Turns':<30} {BASELINE['turns']:<15} {total_turns:<15}")
print(f"{'Total extractions':<30} {BASELINE['total_extractions']:<15} {total_ex:<15}")
print(f"{'Extractions/turn':<30} {BASELINE['avg_per_turn']:<15.1f} {q8_per_turn:<15.1f}")
print(f"{'Faithfulness':<30} {BASELINE['faithfulness']:<15.1f}% {overall_pct:<15.1f}%")
print(f"{'Speed (s/turn)':<30} {BASELINE['speed']:<15.1f} {avg_speed:<15.1f}")
print(f"{'Faithful/total':<30} {'54/65':<15} {total_faithful}/{total_ex}")

print(f"\nPer-turn detail:")
for r in all_results:
    pct = r["faithful"] / r["extractions"] * 100 if r["extractions"] else 0
    print(f"  {r['turn'][:8]}... ext={r['extractions']} faith={r['faithful']}/{r['unfaithful']} ({pct:.0f}%) {r['speed']:.0f}s")

# Save
out = {
    "meta": {"test": "3B Q4_K_M vs Q8_0 extract faithfulness", "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    "baseline": {"model": "Qwen2.5-3B (Q4_K_M)", **BASELINE},
    "q8_0": {"model": "Qwen2.5-Coder-3B-Instruct.Q8_0", "turns": total_turns, "total_extractions": total_ex, "total_faithful": total_faithful, "total_unfaithful": total_unfaithful, "faithfulness_rate": round(overall_pct / 100, 3), "avg_speed": round(avg_speed, 1)},
    "per_turn": all_results,
    "comparison": {"faithfulness_change": f"{overall_pct - BASELINE['faithfulness']:+.1f}%", "speed_change": f"{(BASELINE['speed'] - avg_speed) / BASELINE['speed'] * 100:+.0f}%" if avg_speed else "N/A"},
}
with open(f"{EXPER_DIR}/exp_extract_3b_q8_comparison.json", "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print(f"\n[SAVED] {EXPER_DIR}/exp_extract_3b_q8_comparison.json")
