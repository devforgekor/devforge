#!/usr/bin/env python3
# Status: experimental
# Path: tests/test_extraction_faithfulness.py — pytest
"""Extraction faithfulness test: Qwen3-4B vs Qwen2.5-Coder-3B baseline."""

import csv
import http.client
import io
import json
import re
import subprocess
import sys

# ---------------------------------------------------------------------------
# 1. Fetch the turn from postgres
# ---------------------------------------------------------------------------

TURN_ID = "23eece7d-0e94-4c62-914c-a895ac714791"
CMD = [
    "podman", "exec", "postgres", "psql",
    "-U", "postgres", "-d", "devforge_app",
    "--csv",
    "-c", f"SELECT id, user_turn, thinking, text FROM turns WHERE id = '{TURN_ID}'",
]

result = subprocess.run(CMD, capture_output=True, text=True, timeout=30)
if result.returncode != 0:
    print(f"DB query failed:\n{result.stderr}")
    sys.exit(1)

reader = csv.DictReader(io.StringIO(result.stdout))
rows = list(reader)
if not rows:
    print(f"Turn {TURN_ID} not found.")
    sys.exit(1)

row = rows[0]
turn_id = row["id"]
user_turn = row.get("user_turn") or ""
thinking = row.get("thinking") or ""
text = row.get("text") or ""

print(f"Turn: {turn_id}")
print(f"  user_turn length: {len(user_turn)} chars")
print(f"  thinking  length: {len(thinking)} chars")
print(f"  text      length: {len(text)} chars")
print()

# ---------------------------------------------------------------------------
# 2. SYSTEM_EXTRACT_3B prompt (from extract_pipeline.py)
# ---------------------------------------------------------------------------

SYSTEM_EXTRACT_3B = """\
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

user_message = (
    f"user_turn:\n{user_turn}\n\n"
    f"thinking:\n{thinking}\n\n"
    f"text:\n{text}"
)

body = {
    "messages": [
        {"role": "system", "content": SYSTEM_EXTRACT_3B},
        {"role": "user", "content": user_message},
    ],
    "max_tokens": 2048,
    "temperature": 0.1,
    "stream": False,
}

# ---------------------------------------------------------------------------
# 3. Call Qwen3-4B at port 8082
# ---------------------------------------------------------------------------

print("Calling Qwen3-4B on port 8082 ... ", end="", flush=True)

conn = http.client.HTTPConnection("127.0.0.1", 8082, timeout=600)
try:
    body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    conn.request("POST", "/v1/chat/completions", body=body_bytes,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    raw_resp = resp.read().decode("utf-8")
    raw = json.loads(raw_resp)
except Exception as e:
    print(f"FAILED\nLLM call error: {e}")
    sys.exit(1)
finally:
    conn.close()

print("OK")

choices = raw.get("choices", [])
if not choices:
    print(f"No choices in response: {raw}")
    sys.exit(1)

reply_text = (choices[0]["message"].get("content") or "").strip()
usage = raw.get("usage", {})
print(f"Usage: {json.dumps(usage, indent=2)}")
print()

print(f"Raw model reply ({len(reply_text)} chars):")
print(reply_text[:600])
if len(reply_text) > 600:
    print("...(truncated)")
print()

# ---------------------------------------------------------------------------
# 4. Robust JSON extraction
# ---------------------------------------------------------------------------

def extract_extractions_lenient(text: str) -> list:
    """Extract individual extraction objects from possibly-broken JSON.

    Tries:
      1. json.loads on whole text
      2. json.loads on code-fenced content
      3. Regex-based per-object extraction (handles truncated entries)
    """
    # -- Attempt 1: strict parse --
    for candidate_text in [text]:
        try:
            data = json.loads(candidate_text)
            exs = data.get("extractions", [])
            if isinstance(exs, list) and len(exs) > 0:
                return exs
        except (json.JSONDecodeError, ValueError):
            pass

    # -- Attempt 2: extract from code fence --
    for m in re.finditer(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL):
        try:
            data = json.loads(m.group(1))
            exs = data.get("extractions", [])
            if isinstance(exs, list):
                return exs
        except (json.JSONDecodeError, ValueError):
            pass

    # -- Attempt 3: extract each object from the extractions array via regex --
    print("  [lenient mode] falling back to regex-based extraction...")
    # Match each {fact_type, evidence, category} object individually.
    # Handle escaped quotes inside evidence by allowing \\" sequences.
    pattern = (
        r'\{\s*'
        r'"fact_type"\s*:\s*"(.*?)"\s*,'
        r'\s*"evidence"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,'
        r'\s*"category"\s*:\s*"(.*?)"\s*'
        r'\}'
    )
    objs = re.findall(pattern, text, re.DOTALL)
    if objs:
        return [
            {"fact_type": ft.strip(), "evidence": ev.strip(), "category": cat.strip()}
            for ft, ev, cat in objs
        ]

    # -- Attempt 4: try to find any top-level keys --
    try:
        # Maybe the whole response is just the extractions array
        data = json.loads(text)
        if isinstance(data, dict):
            return data.get("extractions", [])
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    return []


extractions = extract_extractions_lenient(reply_text)
print(f"Total extractions parsed: {len(extractions)}")
print()

if not extractions:
    print("No extractions to evaluate.")
    sys.exit(0)

# ---------------------------------------------------------------------------
# 5. Faithfulness check
# ---------------------------------------------------------------------------

source_map = {
    "user": user_turn,
    "thinking": thinking,
    "text": text,
}

faithful = []
unfaithful = []

for i, ex in enumerate(extractions):
    fact_type = ex.get("fact_type", "?")
    evidence = ex.get("evidence", "")
    category = ex.get("category", "?")
    source_text = source_map.get(fact_type, "")

    if not source_text:
        reason = f"fact_type '{fact_type}' has no source text (empty field in turn)"
        unfaithful.append((i, evidence, fact_type, category, reason))
        continue

    ev_lower = evidence.lower().strip()
    src_lower = source_text.lower()

    if not ev_lower:
        unfaithful.append((i, evidence, fact_type, category, "empty evidence"))
    elif ev_lower in src_lower:
        faithful.append((i, evidence, fact_type, category))
    else:
        # Try removing trailing punctuation before checking again
        cleaned = ev_lower.rstrip(".,;:!?")
        if cleaned in src_lower:
            faithful.append((i, evidence, fact_type, category))
        else:
            unfaithful.append((i, evidence, fact_type, category,
                               "evidence not found verbatim in source"))

print(f"Faithful:   {len(faithful)}")
print(f"Unfaithful: {len(unfaithful)}")
print()

if faithful:
    print("--- Faithful extractions ---")
    for i, ev, ft, cat in faithful:
        print(f"  [{i}] ({ft}/{cat}) {ev[:150]}")
    print()

if unfaithful:
    print("--- UNFAITHFUL extractions ---")
    for item in unfaithful:
        i, ev, ft, cat, reason = item
        print(f"  [{i}] ({ft}/{cat}) {ev[:150]}")
        print(f"       Reason: {reason}")
    print()

# ---------------------------------------------------------------------------
# 6. Summary
# ---------------------------------------------------------------------------

total = len(extractions)
pct = (len(faithful) / total * 100) if total else 0
print(f"{'=' * 60}")
print("EXTRACTION FAITHFULNESS TEST RESULTS")
print(f"{'=' * 60}")
print("Model:             Qwen3-4B (port 8082)")
print("Model ID:          Qwen3-4B-Instruct-2507.Q4_K_M.gguf")
print(f"Turn ID:           {turn_id}")
print(f"Source fields:     user_turn={'empty' if not user_turn else 'present'}, "
      f"thinking={'empty' if not thinking else 'present'}, "
      f"text={'present' if text else 'empty'}")
print(f"Total extractions: {total}")
print(f"Faithful:          {len(faithful)} ({pct:.1f}%)")
print(f"Unfaithful:        {len(unfaithful)} ({100 - pct:.1f}%)")
print()

if unfaithful:
    print("Unfaithful details:")
    for item in unfaithful:
        i, ev, ft, cat, reason = item
        print(f"  [{i}] {reason}: \"{ev[:120]}\"")

# Print baseline comparison reference
print()
print("--- Baseline (Qwen2.5-Coder-3B) reference ---")
print("The previous model (Qwen2.5-Coder-3B) was not re-tested here.")
print("Compare these results against historical data from earlier runs.")
print()
print("Test script: /opt/projects/server/scripts/test_extraction_faithfulness.py")
