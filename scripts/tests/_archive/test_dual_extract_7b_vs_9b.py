#!/usr/bin/env python3
# Status: experimental
# Path: none — prototype: 7B dual vs 9B dual extraction comparison
"""7B dual vs 9B dual extraction: which model/persona combo performs best.

Tests both models with Strict (precise, max 4) and Xplore (high recall, max 4)
personas on the same 3 turns. Generates comparison table."""

import sys

sys.path.insert(0, "/opt/projects/server/scripts")

from lib.llm.json_parser import parse_llm_json
from lib.llm_client import call_llm_with_retry
from lib.pod_manager import ensure_dual
from pipelines.extract_llm import (
    _STRUCTURED_FIELDS,
    _calc_max_tokens,
    _calc_timeout,
)
from pipelines.extract_llm import (
    _SYSTEM_TEXT_EXTRACT_FREE as _SYSTEM_TEXT_EXTRACT_STRICT,
)
from pipelines.extract_llm import (
    _SYSTEM_TEXT_EXTRACT_XPLORE_FREE as _SYSTEM_TEXT_EXTRACT_XPLORE,
)
from pipelines.extract_llm import (
    _SYSTEM_USER_EXTRACT_FREE as _SYSTEM_USER_EXTRACT_STRICT,
)
from pipelines.extract_llm import (
    _SYSTEM_USER_EXTRACT_XPLORE_FREE as _SYSTEM_USER_EXTRACT_XPLORE,
)


def extract(prompt, source_text, model_key):
    prompt_text = prompt + "\n\n" + _STRUCTURED_FIELDS
    total_chars = len(source_text) + len(prompt_text)
    effective_max_tokens = _calc_max_tokens(total_chars) or 512
    timeout = _calc_timeout(total_chars, effective_max_tokens)
    meta = call_llm_with_retry(
        [{"role": "system", "content": prompt_text}, {"role": "user", "content": source_text}],
        model=model_key,
        max_tokens=effective_max_tokens,
        temperature=0.0,
        timeout=timeout,
        json_mode=True,
        return_meta=True,
    )
    result = parse_llm_json(meta["content"])
    facts = result.get("extractions", []) if result else []
    return facts, meta.get("elapsed", 0)


def dedup(facts_a, facts_b):
    seen = set()
    merged = []
    for f in facts_a + facts_b:
        key = (f.get("subject", ""), f.get("predicate", ""), f.get("object", ""))
        if key not in seen:
            seen.add(key)
            merged.append(f)
    return merged


def load_turns():
    import subprocess
    import uuid

    raw = (
        subprocess.check_output(
            [
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
                "-F",
                "|",
                "-c",
                """
        SELECT id, user_turn_clean, text_clean
        FROM turns WHERE id IN (
          'abe91c0c-f311-48ea-a0a6-893b2ce663b9',
          'd5ce280d-ed12-4b7e-a490-a6f9a5a28607',
          'deb06ca2-9010-48cf-b964-6ae26ce3863b'
        ) ORDER BY created_at;
    """,
            ]
        )
        .decode()
        .strip()
    )
    turns = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) >= 3:
            turns.append(
                {
                    "id": uuid.UUID(parts[0].strip()),
                    "user": parts[1].strip() or "",
                    "text": parts[2].strip() or "",
                }
            )
    return turns


def run_dual_extraction(label, meta_key_a, meta_key_b, route_strict, route_xplore, turns):
    """Extract from all turns using dual mode.

    A = Strict persona (port 8082)
    B = Xplore persona (port 8083)
    """
    print(f"\n{'=' * 60}")
    print(f"[{label}] Dual extraction: {route_strict}(Strict) + {route_xplore}(Xplore)")
    print(f"{'=' * 60}")

    ensure_dual(meta_key_a, meta_key_b, skip_if_healthy=False)

    strict = {}
    xplore = {}
    elapsed_strict = 0.0
    elapsed_xplore = 0.0

    for t in turns:
        eid = str(t["id"])[:8]
        sf = []
        if len(t["user"]) > 50:
            f, lat = extract(_SYSTEM_USER_EXTRACT_STRICT, t["user"], route_strict)
            sf.extend(f)
            elapsed_strict += lat
            print(f"  [{label} Strict] {eid} user: {len(f)} ({lat:.0f}s)")
        if len(t["text"]) > 50:
            f, lat = extract(_SYSTEM_TEXT_EXTRACT_STRICT, t["text"], route_strict)
            sf.extend(f)
            elapsed_strict += lat
            print(f"  [{label} Strict] {eid} text: {len(f)} ({lat:.0f}s)")
        strict[eid] = {"facts": sf, "elapsed": elapsed_strict}
        print(f"  [{label} Strict] {eid} total: {len(sf)}")

    print()

    for t in turns:
        eid = str(t["id"])[:8]
        xf = []
        if len(t["user"]) > 50:
            f, lat = extract(_SYSTEM_USER_EXTRACT_XPLORE, t["user"], route_xplore)
            xf.extend(f)
            elapsed_xplore += lat
            print(f"  [{label} Xplore] {eid} user: {len(f)} ({lat:.0f}s)")
        if len(t["text"]) > 50:
            f, lat = extract(_SYSTEM_TEXT_EXTRACT_XPLORE, t["text"], route_xplore)
            xf.extend(f)
            elapsed_xplore += lat
            print(f"  [{label} Xplore] {eid} text: {len(f)} ({lat:.0f}s)")
        xplore[eid] = {"facts": xf, "elapsed": elapsed_xplore}
        print(f"  [{label} Xplore] {eid} total: {len(xf)}")

    merged = {}
    for eid in strict:
        s = strict[eid]["facts"]
        x = xplore.get(eid, {"facts": []})["facts"]
        m = dedup(s, x)
        merged[eid] = m
        print(f"  [{label}] merged {eid}: strict={len(s)} xplore={len(x)} merged={len(m)}")

    return strict, xplore, merged


def print_facts(label, data):
    for eid, entry in data.items():
        print(
            f"  [{label}] {eid}: {len(entry['facts'])} facts ({entry.get('elapsed', 0):.0f}s cumulative)"
        )
        for f in entry["facts"]:
            print(
                f"    [{f.get('subject', '?')}] {f.get('predicate', '?')} = {f.get('object', '?')}"
            )


def test():
    print("=" * 60)
    print("7B vs 9B Dual Extraction Comparison (Strict + Xplore)")
    print("=" * 60)

    turns = load_turns()
    print(f"  {len(turns)} turns loaded\n")

    # ── Phase 1: 7B dual ──
    strict_7b, xplore_7b, merged_7b = run_dual_extraction(
        "7B",
        "day-verifier",
        "day-verifier-b",
        "day_verify",
        "day_verify_b",
        turns,
    )

    # ── Phase 2: 9B dual ──
    strict_9b, xplore_9b, merged_9b = run_dual_extraction(
        "9B",
        "day-enricher",
        "day-enricher-b",
        "day_enrich",
        "day_enrich_b",
        turns,
    )

    # ── Phase 3: Comparison ──
    print("\n" + "=" * 60)
    print("COMPARISON TABLE: 7B vs 9B per persona")
    print("=" * 60)

    # Per-turn breakdown
    print(
        f"\n{'Turn':<16} {'7B-Strict':>10} {'7B-Xplore':>10} {'7B-Merged':>10} | {'9B-Strict':>10} {'9B-Xplore':>10} {'9B-Merged':>10}"
    )
    print(f"{'-' * 16} {'-' * 10} {'-' * 10} {'-' * 10} {'-' * 10} {'-' * 10} {'-' * 10}")

    total_s7b = total_x7b = total_m7b = 0
    total_s9b = total_x9b = total_m9b = 0

    for t in turns:
        eid = str(t["id"])[:8]
        s7 = len(strict_7b.get(eid, {}).get("facts", []))
        x7 = len(xplore_7b.get(eid, {}).get("facts", []))
        m7 = len(merged_7b.get(eid, []))
        s9 = len(strict_9b.get(eid, {}).get("facts", []))
        x9 = len(xplore_9b.get(eid, {}).get("facts", []))
        m9 = len(merged_9b.get(eid, []))
        total_s7b += s7
        total_x7b += x7
        total_m7b += m7
        total_s9b += s9
        total_x9b += x9
        total_m9b += m9
        print(f"{eid:<16} {s7:>10} {x7:>10} {m7:>10} | {s9:>10} {x9:>10} {m9:>10}")

    print(f"{'-' * 16} {'-' * 10} {'-' * 10} {'-' * 10} {'-' * 10} {'-' * 10} {'-' * 10}")
    print(
        f"{'TOTAL':<16} {total_s7b:>10} {total_x7b:>10} {total_m7b:>10} | {total_s9b:>10} {total_x9b:>10} {total_m9b:>10}"
    )

    # Strict comparison: 7B vs 9B
    print("\n--- Strict comparison ---")
    if total_s7b > total_s9b:
        print(f"  7B Strict wins by quantity: {total_s7b} vs {total_s9b}")
    elif total_s9b > total_s7b:
        print(f"  9B Strict wins by quantity: {total_s9b} vs {total_s7b}")
    else:
        print(f"  Tie: {total_s7b} facts each")

    # Xplore comparison
    print("\n--- Xplore comparison ---")
    if total_x7b > total_x9b:
        print(f"  7B Xplore wins by quantity: {total_x7b} vs {total_x9b}")
    elif total_x9b > total_x7b:
        print(f"  9B Xplore wins by quantity: {total_x9b} vs {total_x7b}")
    else:
        print(f"  Tie: {total_x7b} facts each")

    # Merged comparison
    print("\n--- Overall (merged) comparison ---")
    diff_m = total_m7b - total_m9b
    if diff_m > 0:
        print(f"  7B wins overall: {total_m7b} vs {total_m9b} (+{diff_m})")
    elif diff_m < 0:
        print(f"  9B wins overall: {total_m9b} vs {total_m7b} ({diff_m})")
    else:
        print(f"  Tie: {total_m7b} facts each")

    # Speed comparison
    final_strict_7b_elapsed = max((v.get("elapsed", 0) for v in strict_7b.values()), default=0)
    final_xplore_7b_elapsed = max((v.get("elapsed", 0) for v in xplore_7b.values()), default=0)
    final_strict_9b_elapsed = max((v.get("elapsed", 0) for v in strict_9b.values()), default=0)
    final_xplore_9b_elapsed = max((v.get("elapsed", 0) for v in xplore_9b.values()), default=0)
    total_7b_elapsed = final_strict_7b_elapsed + final_xplore_7b_elapsed
    total_9b_elapsed = final_strict_9b_elapsed + final_xplore_9b_elapsed

    print("\n--- Speed comparison ---")
    print(
        f"  7B: Strict {final_strict_7b_elapsed:.0f}s + Xplore {final_xplore_7b_elapsed:.0f}s = {total_7b_elapsed:.0f}s total"
    )
    print(
        f"  9B: Strict {final_strict_9b_elapsed:.0f}s + Xplore {final_xplore_9b_elapsed:.0f}s = {total_9b_elapsed:.0f}s total"
    )

    # Dedicated fact listing per persona
    print(f"\n{'=' * 60}")
    print("DETAILED FACTS: 7B")
    print(f"{'=' * 60}")
    print("\n--- 7B Strict ---")
    print_facts("7B-Strict", strict_7b)
    print("\n--- 7B Xplore ---")
    print_facts("7B-Xplore", xplore_7b)
    print("\n--- 7B Merged ---")
    for eid, facts in merged_7b.items():
        print(f"  [7B] {eid}: {len(facts)} facts")
        for f in facts:
            print(
                f"    [{f.get('subject', '?')}] {f.get('predicate', '?')} = {f.get('object', '?')}"
            )

    print(f"\n{'=' * 60}")
    print("DETAILED FACTS: 9B")
    print(f"{'=' * 60}")
    print("\n--- 9B Strict ---")
    print_facts("9B-Strict", strict_9b)
    print("\n--- 9B Xplore ---")
    print_facts("9B-Xplore", xplore_9b)
    print("\n--- 9B Merged ---")
    for eid, facts in merged_9b.items():
        print(f"  [9B] {eid}: {len(facts)} facts")
        for f in facts:
            print(
                f"    [{f.get('subject', '?')}] {f.get('predicate', '?')} = {f.get('object', '?')}"
            )

    print("\nDone.")


if __name__ == "__main__":
    test()
