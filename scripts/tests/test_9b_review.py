#!/usr/bin/env python3
# Status: experimental
# Path: none — prototype of 9B review test for 4B extraction completeness
"""9B Extraction Review Test: 4B results → 9B finds missed facts."""

import sys

sys.path.insert(0, "/opt/projects/server/scripts")

from lib.llm.json_parser import parse_llm_json
from lib.llm_client import call_llm_with_retry
from lib.pod_manager import ensure_model

# ── Prompt ──
SYSTEM_9B_REVIEWER = """\
You are a Fact Extraction Reviewer. Given the original conversation turn and
the facts already extracted by a smaller model, your job is to determine if
any facts were MISSED.

Rules:
1. Read the conversation turn carefully.
2. Review the list of already-extracted facts.
3. Identify if there are any additional factual triples (Subject → Predicate = Object)
   that are CLEARLY present in the text but were not extracted.
4. Only add facts that meet ALL criteria:
   - Subject and predicate are explicitly stated or directly implied
   - The triple conveys a specific, verifiable claim
   - It's genuinely NEW (not a rephrasing of an already-extracted fact)
5. Do NOT add facts that are vague, speculative, or require significant inference.
6. If no facts were missed, return an empty list.

Return ONLY valid JSON:
{"new_facts": [{"subject": "...", "predicate": "...", "object": "..."}], "reasoning": "why you added or didn't add (≤20 words)"}"""

# ── Test Data (same as E2E) ──
SOURCE_TEXT = (
    "We run Pod B with day-extractor model (Qwen3.5-9B-Q4_K_M, port 8082): "
    "threads=4, cpus=0-2, context=8192. The day-extractor model runs two "
    "instances: 8082 and 8083. There is also a day-extractor that uses "
    "Qwen3.5-9B-Q4_K_M model. The extract pipeline processes sequentially "
    "which causes excessive latency. extract_llm.py applies WorkStealer to "
    "enable parallel user/text processing. Other files: raw_consumer.py, "
    "extract_verify.py, enrich.py use inline import. There is a known bug: "
    "embed_batch.py dimension mismatch."
)

SOURCE_TEXT_2 = (
    "day-extractor (Qwen3.5-9B-Q4_K_M, port 8082): threads=4, cpus=0-2, "
    "context=8192. The model generates 3.27 tok/s on average. "
    "extract pipeline processes sequentially causing excessive latency. "
    "extract_llm.py applies WorkStealer for parallel user/text processing."
)

# 4B extracted facts (from last E2E)
EXISTING_FACTS = [
    # user section
    {"subject": "day-extractor model", "predicate": "runs", "object": "two instances"},
    {"subject": "Pod B", "predicate": "runs_on", "object": "day-extractor model"},
    {"subject": "day-extractor", "predicate": "uses", "object": "Qwen3.5-9B-Q4_K_M model"},
    {"subject": "day-extractor model", "predicate": "configured_with", "object": "4 threads"},
    {
        "subject": "day-extractor model",
        "predicate": "operates_multiple_instances",
        "object": "ports 8082 and 8083",
    },
    {"subject": "extract pipeline", "predicate": "processes", "object": "sequentially"},
    {
        "subject": "extract_llm.py",
        "predicate": "applies_WorkStealer",
        "object": "enables parallel user/text processing",
    },
    # text section
    {"subject": "day-extractor", "predicate": "runs_on_port", "object": "8082"},
    {
        "subject": "extract pipeline",
        "predicate": "experiences_latency_degradation",
        "object": "due to sequential processing",
    },
    {
        "subject": "extract_llm.py",
        "predicate": "implements_work_stealing",
        "object": "for parallel user/text processing",
    },
    {"subject": "model", "predicate": "generates_tokens_per_second", "object": "3.27"},
]


def format_prompt(section_label, source, facts):
    fact_lines = "\n".join(
        f"  {i + 1}. [{f['subject']}] {f['predicate']} = {f['object']}" for i, f in enumerate(facts)
    )
    return f"""=== SECTION: {section_label} ===

=== SOURCE TEXT ===
{source}

=== ALREADY EXTRACTED FACTS ===
{fact_lines}

=== TASK ===
Review the source text. Were any factual triples missed by the extractor?"""


# ── Test Runner ──
def test_9b_review():
    print("=" * 60)
    print("Test: 9B Extraction Review")
    print("=" * 60)

    # Ensure 9B model on 8082
    print("\n[1] Ensuring 9B model (day-extractor)...")
    ensure_model("day-extractor", skip_if_healthy=False)
    print("  OK")

    # Test user section
    print("\n[2] Reviewing user section...")
    msg = format_prompt("user", SOURCE_TEXT, EXISTING_FACTS)
    meta = call_llm_with_retry(
        [
            {"role": "system", "content": SYSTEM_9B_REVIEWER},
            {"role": "user", "content": msg},
        ],
        model="day_enrich",
        max_tokens=512,
        temperature=0.0,
        timeout=120,
        json_mode=True,
        return_meta=True,
    )
    result = parse_llm_json(meta["content"])
    if result:
        new_facts = result.get("new_facts", [])
        reasoning = result.get("reasoning", "")
        print(f"  Reasoning: {reasoning}")
        print(f"  New facts found: {len(new_facts)}")
        for f in new_facts:
            print(
                f"    [{f.get('subject', '?')}] {f.get('predicate', '?')} = {f.get('object', '?')}"
            )
    else:
        print(f"  Parse failed. Raw: {meta['content'][:200]}")

    # Test text section
    print("\n[3] Reviewing text section...")
    msg2 = format_prompt("text", SOURCE_TEXT_2, EXISTING_FACTS)
    meta2 = call_llm_with_retry(
        [
            {"role": "system", "content": SYSTEM_9B_REVIEWER},
            {"role": "user", "content": msg2},
        ],
        model="day_enrich",
        max_tokens=512,
        temperature=0.0,
        timeout=120,
        json_mode=True,
        return_meta=True,
    )
    result2 = parse_llm_json(meta2["content"])
    if result2:
        new_facts2 = result2.get("new_facts", [])
        reasoning2 = result2.get("reasoning", "")
        print(f"  Reasoning: {reasoning2}")
        print(f"  New facts found: {len(new_facts2)}")
        for f in new_facts2:
            print(
                f"    [{f.get('subject', '?')}] {f.get('predicate', '?')} = {f.get('object', '?')}"
            )
    else:
        print(f"  Parse failed. Raw: {meta2['content'][:200]}")

    print("\nTotal LLM calls: 2" if result and result2 else "\nSome calls failed")
    print("Done.")


if __name__ == "__main__":
    test_9b_review()
