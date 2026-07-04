#!/usr/bin/env python3
"""Phase 3 re-run with fixed DB sampling (char_length not est_chars)."""

import sys

sys.path.insert(0, "/opt/projects/server/scripts")
import json
import subprocess

from lib.test_common import log, test_complete, test_heartbeat, test_setup
from lib.test_runner import BANNED_PREDICATES, PodBClient, RetryConfig

BASE_PROMPT = """You are a fact extractor for a developer conversation. Extract factual triples (subject, predicate, object) that are EXPLICITLY present in the text.

Do NOT infer, summarize, or add information not present in the source.

RULES:
1. ATOMIC CLAIM: Each evidence MUST contain exactly ONE atomic claim.
2. SELF-CONTAINED: Resolve pronouns and implicit references.
3. SUBJECT-PREDICATE-OBJECT: Every fact MUST have all three. The predicate is a snake_case verb phrase describing the relation (NOT is/has/does/was/equals/exists).
4. CONCISE: Keep evidence under 12 words. Short, direct sentences only.
5. FAITHFULNESS: Directly traceable to source text. NO inference.
6. NO INVENT: If nothing extractable, return empty array. Do NOT force extraction."""

SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "extraction",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "extractions": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "evidence": {"type": "string", "maxLength": 120},
                            "subject": {"type": "string", "maxLength": 30},
                            "predicate": {"type": "string", "maxLength": 40},
                            "object": {"type": "string", "maxLength": 60},
                        },
                        "required": ["evidence", "subject", "predicate", "object"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["extractions"],
            "additionalProperties": False,
        },
    },
}


def get_samples(lo, hi, limit):
    cmd = [
        "podman",
        "exec",
        "postgres",
        "psql",
        "-U",
        "devforge",
        "-d",
        "devforge_app",
        "-t",
        "-A",
        "-c",
        f"SELECT row_to_json(t) FROM (SELECT id::text, text FROM turns WHERE char_length(COALESCE(text,'')) BETWEEN {lo} AND {hi} ORDER BY RANDOM() LIMIT {limit}) t",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    samples = []
    for line in r.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if row.get("text"):
                samples.append((row["id"], row["text"]))
        except json.JSONDecodeError:
            continue
    return samples


def run_one(client, text, label):
    messages = [{"role": "system", "content": BASE_PROMPT}, {"role": "user", "content": text}]
    result = client.retry(
        client.call_llm,
        messages,
        schema=SCHEMA,
        max_tokens=1000,
        temperature=0.0,
        label=label,
        retry=RetryConfig(max_retries=2, backoff=15),
    )
    obs = json.dumps(
        {
            "domain": "extraction-test",
            "label": label,
            "status": result.status,
            "facts": result.num_facts,
            "elapsed": round(result.elapsed, 1),
            "tokens": result.total_tokens,
            "error": result.error,
        },
        ensure_ascii=False,
    )
    log(f"  [OBS] {obs}")
    return result


TEST = test_setup("phase3_rerun", "Phase 3 re-run with fixed sampling")
client = PodBClient("devforge-inference", 8082, timeout=600)

# Health
h = client.health_check(timeout=15)
if not h.ok:
    log(f"  Health: {h.message}")
    # Model should be 9B Q8 already loaded — try anyway
else:
    log(f"  Health OK ({h.latency:.1f}s)")

all_results = {}
buckets = [("short", 200, 500, 4), ("medium", 500, 1500, 4), ("long", 1500, 100000, 4)]
total_good = total_mixed = total_fail = 0
total_samples = 0

for bname, lo, hi, n in buckets:
    samples = get_samples(lo, hi, n)
    log(f"\n--- {bname}: {len(samples)} samples ---")
    for i, (sid, text) in enumerate(samples, 1):
        label = f"P3-{bname}-{i}"
        log(f"\n[{i}/{n}] {label} ({len(text)} chars)")
        test_heartbeat(f"Phase 3: {label} ({len(text)}c)")
        result = run_one(client, text[:3000], label)
        key = f"{bname}_{i}"
        all_results[key] = {"id": sid, "bucket": bname, "chars": len(text), "result": result}
        total_samples += 1
        if result.status == "GOOD":
            total_good += 1
        elif result.status == "MIXED":
            total_mixed += 1
        elif result.status == "FAIL":
            total_fail += 1
        if not result.error:
            log(result.summary())
            for line in result.facts_table(BANNED_PREDICATES):
                log(line)

# Summary
log("\n" + "=" * 60)
log("PHASE 3 FINAL RESULTS")
log("=" * 60)
for bname, _, _, _ in buckets:
    items = {k: v for k, v in all_results.items() if v["bucket"] == bname}
    g = sum(1 for v in items.values() if v["result"].status == "GOOD")
    m = sum(1 for v in items.values() if v["result"].status == "MIXED")
    f = sum(1 for v in items.values() if v["result"].status == "FAIL")
    log(f"  {bname}: {g} GOOD, {m} MIXED, {f} FAIL (n={len(items)})")

if total_samples:
    log(f"\n  TOTAL: {total_good}/{total_samples} GOOD ({100 * total_good // total_samples}%)")
    log(
        f"  USABLE: {total_good + total_mixed}/{total_samples} usable ({(total_good + total_mixed) * 100 // total_samples}%)"
    )

test_complete("Phase 3 re-run complete")
sys.exit(0 if total_good > 0 else 1)
