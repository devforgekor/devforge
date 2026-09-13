#!/usr/bin/env python3
# Status: experimental
# Path: tests/test_day_pipeline.py — pytest (통합)
"""Day pipeline tests: small verify (3B), extraction faithfulness, quantized extract.

Usage:  python3 -m pytest tests/test_day_pipeline.py -v
        python3 tests/test_day_pipeline.py
"""

import csv
import http.client
import io
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))


def parse_json_response(reply):
    """Parse LLM JSON response, stripping code fences if present."""
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", reply.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        return json.loads(m.group(0)) if m else {}


def log(msg):
    t = time.strftime("%H:%M:%S")
    print(f"[{t}] {msg}", flush=True)

# ═══════════════════════════════════════════════════════════════════════════
# Test: Extraction faithfulness (single turn, Qwen3-4B)
# ═══════════════════════════════════════════════════════════════════════════

SYSTEM_EXTRACT = """You are a fact extractor for a developer-assistant conversation turn.
Extract key factual statements that are EXPLICITLY present in the text.
Do NOT infer, summarize, or add information not present in the source.

Output STRICT JSON:
{"extractions": [{"fact_type": "user|thinking|text", "evidence": "Exact quote or close paraphrase", "category": "requirement|decision|explanation|code|reasoning|other"}]}

Rules:
- fact_type must match which source field the evidence came from
- evidence must be directly traceable to the source text
- Skip thinking if empty or formatting only
- Extract at most 5 facts per fact_type"""

TURN_ID = "23eece7d-0e94-4c62-914c-a895ac714791"


def test_extraction_faithfulness():
    """Single-turn extraction faithfulness: Qwen3-4B on :8082."""
    cmd = ["podman", "exec", "postgres", "psql",
           "-U", "postgres", "-d", "devforge_app",
           "--csv", "-c",
           f"SELECT id, user_turn, thinking, text FROM turns WHERE id = '{TURN_ID}'"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    reader = csv.DictReader(io.StringIO(result.stdout))
    rows = list(reader)
    if not rows:
        log(f"Turn {TURN_ID} not found")
        return
    row = rows[0]
    user_turn = row.get("user_turn") or ""
    thinking = row.get("thinking") or ""
    text = row.get("text") or ""

    user_msg = f"user_turn:\n{user_turn}\n\nthinking:\n{thinking}\n\ntext:\n{text}"
    body = {"messages": [{"role":"system","content":SYSTEM_EXTRACT},
                         {"role":"user","content":user_msg}],
            "max_tokens": 2048, "temperature": 0.1, "stream": False}

    conn = http.client.HTTPConnection("127.0.0.1", 8082, timeout=600)
    try:
        conn.request("POST", "/v1/chat/completions",
                     json.dumps(body, ensure_ascii=False).encode("utf-8"),
                     headers={"Content-Type":"application/json"})
        resp = conn.getresponse()
        raw = json.loads(resp.read().decode("utf-8"))
    finally:
        conn.close()

    reply = (raw.get("choices") or [{}])[0].get("message",{}).get("content","")
    data = parse_json_response(reply)
    extractions = data.get("extractions", []) if data else []

    source_map = {"user": user_turn, "thinking": thinking, "text": text}
    faithful = sum(1 for ex in extractions
                   if ex.get("evidence","").lower().strip() in source_map.get(ex.get("fact_type",""),"").lower())
    total = len(extractions)
    pct = faithful / total * 100 if total else 0
    log(f"Extraction faithfulness: {faithful}/{total} ({pct:.1f}%)")


# ═══════════════════════════════════════════════════════════════════════════
# Test: 3B Q8_0 vs Q4_K_M extract comparison
# ═══════════════════════════════════════════════════════════════════════════

BASELINE = {"faithfulness": 83.1, "total_extractions": 65, "turns": 15, "avg_per_turn": 4.3, "speed": 81.1}


def get_turns(limit=5):
    cmd = ["podman", "exec", "postgres", "psql",
           "-U", "devforge", "-d", "devforge_app", "--csv",
           "-c", f"SELECT id, user_turn, thinking, text FROM turns WHERE user_turn != '' AND text != '' ORDER BY random() LIMIT {limit}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return list(csv.DictReader(io.StringIO(r.stdout)))


def test_extract_quantized():
    """3B Q8_0 extract faithfulness vs Q4_K_M baseline."""
    turns = get_turns(5)
    log(f"Fetching {len(turns)} turns for extract comparison...")

    all_results = []
    for turn in turns:
        user_turn = turn.get("user_turn") or ""
        thinking = turn.get("thinking") or ""
        text = turn.get("text") or ""
        user_msg = f"user_turn:\n{user_turn}\n\nthinking:\n{thinking}\n\ntext:\n{text}"
        body = {"messages": [{"role":"system","content":SYSTEM_EXTRACT},
                             {"role":"user","content":user_msg}],
                "max_tokens": 1024, "temperature": 0.1, "stream": False}
        t0 = time.time()
        conn = http.client.HTTPConnection("127.0.0.1", 8080, timeout=600)
        try:
            conn.request("POST", "/v1/chat/completions",
                         json.dumps(body, ensure_ascii=False).encode("utf-8"),
                         headers={"Content-Type":"application/json"})
            raw = json.loads(conn.getresponse().read().decode("utf-8"))
        except Exception as e:
            log(f"  Turn failed: {e}")
            conn.close()
            continue
        conn.close()
        elapsed = time.time() - t0
        reply = (raw.get("choices") or [{}])[0].get("message",{}).get("content","")
        cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", reply.strip())
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", cleaned, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
        extractions = data.get("extractions", [])
        source_map = {"user": user_turn, "thinking": thinking, "text": text}
        faithful = sum(1 for ex in extractions
                       if ex.get("evidence","").lower() in source_map.get(ex.get("fact_type",""),"").lower())
        all_results.append({"extractions": len(extractions), "faithful": faithful, "elapsed": elapsed})

    total_ex = sum(r["extractions"] for r in all_results)
    total_faithful = sum(r["faithful"] for r in all_results)
    total_turns = len(all_results)
    avg_speed = sum(r["elapsed"] for r in all_results) / total_turns if total_turns else 0
    overall_pct = total_faithful / total_ex * 100 if total_ex else 0

    log("\n3B Q8_0 extract vs Q4_K_M baseline:")
    log(f"  Faithfulness: Q4={BASELINE['faithfulness']}% | Q8={overall_pct:.1f}%")
    log(f"  Speed: Q4={BASELINE['speed']}s/turn | Q8={avg_speed:.1f}s/turn")


if __name__ == "__main__":
    if "--faith" in sys.argv:
        test_extraction_faithfulness()
    if "--extract" in sys.argv:
        test_extract_quantized()
    if not any(f in sys.argv for f in ("--faith", "--extract")):
        log("Run day tests.")
        test_extraction_faithfulness()
        test_extract_quantized()
