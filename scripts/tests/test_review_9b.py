#!/usr/bin/env python3
# Status: experimental
# Path: none — prototype: sequential dual 4B -> 9B review completeness check
"""Sequential dual 4B (Strict+Xplore) -> 9B review on real DB turns."""

import sys

sys.path.insert(0, "/opt/projects/server/scripts")

from lib.llm.json_parser import parse_llm_json
from lib.llm_client import call_llm_with_retry
from lib.pod_manager import ensure_dual, ensure_model
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

SYSTEM_9B_REVIEW = """\
You are a fact extraction reviewer. Given the SOURCE TEXT and facts already
extracted by a smaller model, identify factual triples that were MISSED.

Rules:
1. Scan source text for explicit factual claims.
2. Compare each claim against the EXISTING FACTS list.
3. Only add facts that have NO match in existing list.
4. Max 2 new facts — quality over quantity.
5. Return empty list if nothing was missed.

Return ONLY valid JSON:
{"new_facts": [{"subject": "...", "predicate": "...", "object": "..."}], "reasoning": "≤30 chars"}\
"""


def extract(prompt, section_type, source_text, model_key):
    prompt_text = prompt + "\n\n" + _STRUCTURED_FIELDS
    total_chars = len(source_text) + len(prompt_text)
    max_tok = _calc_max_tokens(total_chars) or 512
    timeout = _calc_timeout(total_chars, max_tok)
    meta = call_llm_with_retry(
        [{"role": "system", "content": prompt_text}, {"role": "user", "content": source_text}],
        model=model_key,
        max_tokens=max_tok,
        temperature=0.0,
        timeout=timeout,
        json_mode=True,
        return_meta=True,
    )
    result = parse_llm_json(meta["content"])
    return (result.get("extractions", []) if result else []), meta.get("elapsed", 0)


def review(source, existing_facts):
    fact_lines = "\n".join(
        f"  {i + 1}. [{f['subject']}] {f['predicate']} = {f['object']}"
        for i, f in enumerate(existing_facts)
    )
    prompt = f"=== SOURCE TEXT ===\n{source}\n\n=== EXISTING FACTS ===\n{fact_lines}\n\n=== TASK ===\nReview source text against existing facts. Missing factual triples?"
    total_chars = len(source) + len(prompt)
    max_tok = _calc_max_tokens(total_chars) or 256
    timeout = _calc_timeout(total_chars, max_tok)
    meta = call_llm_with_retry(
        [{"role": "system", "content": SYSTEM_9B_REVIEW}, {"role": "user", "content": prompt}],
        model="day_enrich",
        max_tokens=min(max_tok, 256),
        temperature=0.0,
        timeout=timeout,
        json_mode=True,
        return_meta=True,
    )
    result = parse_llm_json(meta["content"])
    return result, meta.get("elapsed", 0)


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


def test():
    print("=" * 60)
    print("Sequential Dual 4B -> 9B Review")
    print("=" * 60)

    turns = load_turns()
    print(f"  {len(turns)} turns loaded\n")

    # Phase 1+2: Dual 4B (same as 7B test for fair comparison)
    print("[Phase 1+2] Dual 4B extraction (A-Strict:8082 + B-Xplore:8083)...")
    ensure_dual("day-extractor", "day-extractor-b", skip_if_healthy=False)
    strict_results = {}
    for t in turns:
        eid = str(t["id"])[:8]
        all_f = []
        if len(t["user"]) > 50:
            f, lat = extract(_SYSTEM_USER_EXTRACT_STRICT, "user", t["user"], "day_extract")
            all_f.extend(f)
            print(f"  {eid} user: {len(f)} ({lat:.0f}s)")
        if len(t["text"]) > 50:
            f, lat = extract(_SYSTEM_TEXT_EXTRACT_STRICT, "text", t["text"], "day_extract")
            all_f.extend(f)
            print(f"  {eid} text: {len(f)} ({lat:.0f}s)")
        strict_results[eid] = all_f
        print(f"  {eid} total: {len(all_f)}")

    print("\n[Phase 2] 4B XPLORE extraction (8083)...")
    xplore_results = {}
    for t in turns:
        eid = str(t["id"])[:8]
        all_f = []
        if len(t["user"]) > 50:
            f, lat = extract(_SYSTEM_USER_EXTRACT_XPLORE, "user", t["user"], "day_extract_b")
            all_f.extend(f)
            print(f"  {eid} user: {len(f)} ({lat:.0f}s)")
        if len(t["text"]) > 50:
            f, lat = extract(_SYSTEM_TEXT_EXTRACT_XPLORE, "text", t["text"], "day_extract_b")
            all_f.extend(f)
            print(f"  {eid} text: {len(f)} ({lat:.0f}s)")
        xplore_results[eid] = all_f
        print(f"  {eid} total: {len(all_f)}")

    print("\n[Phase 2b] Merging Strict + Xplore...")
    merged = {}
    for eid in strict_results:
        s = strict_results[eid]
        x = xplore_results.get(eid, [])
        m = dedup(s, x)
        merged[eid] = m
        print(f"  {eid}: strict={len(s)} xplore={len(x)} merged={len(m)}")

    # Phase 3: 9B review
    print("\n[Phase 3] 9B reviewer (day-enricher)...")
    ensure_model("day-enricher", skip_if_healthy=False)
    reviews = []
    for t in turns:
        eid = str(t["id"])[:8]
        mf = merged.get(eid, [])
        if not mf:
            print(f"  {eid}: skip (no facts)")
            reviews.append({"eid": eid, "new": 0, "lat": 0})
            continue
        all_source = t["user"] + "\n" + t["text"]
        result, lat = review(all_source, mf)
        nf = result.get("new_facts", []) if result else []
        print(f"  {eid}: {len(mf)} existing -> {len(nf)} new ({lat:.0f}s)")
        for f in nf:
            print(
                f"    [{f.get('subject', '?')}] {f.get('predicate', '?')} = {f.get('object', '?')}"
            )
        reviews.append({"eid": eid, "new": len(nf), "lat": lat, "new_facts": nf if result else []})

    # Summary with 7B comparison
    print("\n" + "=" * 60)
    total_new = sum(r["new"] for r in reviews)
    total_ex = sum(len(merged.get(r["eid"], [])) for r in reviews)
    print(f"Total: {total_ex} existing -> {total_new} new facts (9B)")
    print()

    # Hardcoded 7B results for comparison (from previous test run on same data)
    _7B_RESULTS = {"d5ce280d": 3, "deb06ca2": 3, "abe91c0c": 3}
    print("Comparison: 7B vs 9B new facts per turn")
    print(f"  {'Turn':<12} {'7B':>4} {'9B':>4} {'Diff':>5}")
    print(f"  {'-' * 12} {'-' * 4} {'-' * 4} {'-' * 5}")
    total_7b = 0
    for r in reviews:
        eid = r["eid"]
        v7 = _7B_RESULTS.get(eid, 0)
        v9 = r["new"]
        diff = v9 - v7
        sign = "+" if diff > 0 else ""
        print(f"  {eid:<12} {v7:>4} {v9:>4} {sign}{diff:>4}")
        total_7b += v7
    total_diff = total_new - total_7b
    sign = "+" if total_diff > 0 else ""
    print(f"  {'-' * 12} {'-' * 4} {'-' * 4} {'-' * 5}")
    print(f"  {'TOTAL':<12} {total_7b:>4} {total_new:>4} {sign}{total_diff:>4}")

    if total_new > total_7b:
        print("\nVERDICT: 9B outperforms 7B")
    elif total_new < total_7b:
        print("\nVERDICT: 7B outperforms 9B")
    else:
        print("\nVERDICT: Tie — equal performance")
    print("Done.")


if __name__ == "__main__":
    test()
