#!/usr/bin/env python3
# Status: experimental
# Path: none — prototype: 4B full extraction → 7B vs 9B review
"""4B full pipeline (dual + FactArbiter + LLM conflict) → 7B/9B review comparison.

Phase 1: 4B dual extract (Strict:8082 + Xplore:8083 per section)
         → FactArbiter merge (0.95) → LLM conflict resolve → cross-section dedup
Phase 2: 7B unified review (WorkStealer dual ports, max 2 new)
Phase 3: 9B unified review (WorkStealer dual ports, max 2 new)

No Strict/Xplore split for review — single SYSTEM_REVIEW prompt, WorkStealer."""

import sys

sys.path.insert(0, "/opt/projects/server/scripts")

from lib.arbiter import FactArbiter
from lib.llm.json_parser import parse_llm_json
from lib.llm_client import call_llm_with_retry
from lib.pod_manager import ensure_dual
from lib.work_steal import WorkStealer
from pipelines.extract_llm import (
    _CONFLICT_RESOLVER_4B,
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

SYSTEM_REVIEW = """\
You are a fact extraction reviewer. Your goal: find factual triples that a
previous extractor genuinely MISSED in the SOURCE TEXT.

Follow these steps in order:

Step 1 — DECOMPOSE: Break the SOURCE TEXT into atomic factual claims
(each claim = one subject-predicate-object triple).

Step 2 — COMPARE: For each atomic claim, check against the EXISTING FACTS list.
Mark each as:
  COVERED   = same meaning exists in EXISTING FACTS (exact or semantic match)
  MISSED   = no matching fact in EXISTING FACTS

Step 3 — VERIFY (Chain-of-Verification): For each MISSED claim, ask:
  "Does the source text explicitly state this fact?"
  If uncertain → discard.  If confirmed → keep.

Step 4 — SELECT: Pick up to 2 MISSED facts with highest confidence.
Return empty list if nothing was genuinely missed.

Rules:
- Only add facts with explicit source text support (include evidence snippet).
- Quality over quantity — false positives hurt more than misses.
- Max 2 new facts per review.

Return ONLY valid JSON:
{"new_facts": [{"subject": "...", "predicate": "...", "object": "...", "evidence": "≤100 chars verbatim from source"}], "reasoning": "≤30 chars"}\
"""


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
    return (result.get("extractions", []) if result else []), meta.get("elapsed", 0)


def resolve_conflict(fact_a, fact_b, model_key):
    """LLM conflict resolver: 4B resolves CONFLICT ref -> verdict."""
    b_ev = fact_b.get("evidence", "") or ""
    msg = (
        f"Fact A: [{fact_a.get('subject', '?')}] {fact_a.get('predicate', '?')} = {fact_a.get('object', '?')}\n"
        f"Fact B: [{fact_b.get('subject', '?')}] {fact_b.get('predicate', '?')} = {fact_b.get('object', '?')}"
    )
    if b_ev:
        msg += f"\nEvidence B: {b_ev[:300]}"
    meta = call_llm_with_retry(
        [{"role": "system", "content": _CONFLICT_RESOLVER_4B}, {"role": "user", "content": msg}],
        model=model_key,
        max_tokens=128,
        temperature=0.0,
        timeout=30,
        json_mode=True,
        return_meta=True,
    )
    return parse_llm_json(meta["content"])


def review(source, existing_facts, model_key, system_prompt):
    fact_lines = "\n".join(
        f"  {i + 1}. [{f['subject']}] {f['predicate']} = {f['object']}"
        for i, f in enumerate(existing_facts)
    )
    prompt = f"=== SOURCE TEXT ===\n{source}\n\n=== EXISTING FACTS ===\n{fact_lines}\n\n=== TASK ===\nReview source text against existing facts. Missing factual triples?"
    total_chars = len(source) + len(prompt)
    effective_max_tokens = _calc_max_tokens(total_chars) or 256
    timeout = _calc_timeout(total_chars, effective_max_tokens)
    meta = call_llm_with_retry(
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
        model=model_key,
        max_tokens=min(effective_max_tokens, 256),
        temperature=0.0,
        timeout=timeout,
        json_mode=True,
        return_meta=True,
    )
    result = parse_llm_json(meta["content"])
    return result, meta.get("elapsed", 0)


def _triple_key(f):
    return (f.get("subject", ""), f.get("predicate", ""), f.get("object", ""))


def _section_dedup(new_facts, existing):
    """Dedup new facts against existing set, return unique ones."""
    seen = {_triple_key(f) for f in existing}
    out = []
    for f in new_facts:
        if _triple_key(f) not in seen:
            seen.add(_triple_key(f))
            out.append(f)
    return out


def _norm(s):
    """Normalize string: lower, strip, collapse whitespace."""
    import re

    return re.sub(r"\s+", " ", s.lower().strip())


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
                """SELECT id, user_turn_clean, text_clean
FROM turns WHERE id IN (
  'abe91c0c-f311-48ea-a0a6-893b2ce663b9',
  'd5ce280d-ed12-4b7e-a490-a6f9a5a28607',
  'deb06ca2-9010-48cf-b964-6ae26ce3863b'
) ORDER BY created_at;""",
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


def extract_full_pipeline(turns):
    """Full 4B extraction: dual + FactArbiter + LLM conflict + cross-section dedup."""
    from difflib import SequenceMatcher

    print("\n[Phase 1] 4B full pipeline (extract → merge → LLM conflict → dedup)...")
    ensure_dual("day-extractor", "day-extractor-b", skip_if_healthy=False)
    arbiter = FactArbiter(threshold=0.95)
    final_facts = {}

    for t in turns:
        eid = str(t["id"])[:8]

        # ── Section: user ──
        user_all = []
        if len(t["user"]) > 50:
            sf, _ = extract(_SYSTEM_USER_EXTRACT_STRICT, t["user"], "day_extract")
            xf, _ = extract(_SYSTEM_USER_EXTRACT_XPLORE, t["user"], "day_extract_b")
            refs = arbiter.consolidate(sf, xf)
            conflict_refs = [r for r in refs if r.status.name == "CONFLICT"]
            for cr in conflict_refs:
                b_match = next(
                    (
                        fb
                        for fb in xf
                        if SequenceMatcher(
                            None,
                            _norm(cr.fact.get("subject", "") + " " + cr.fact.get("predicate", "")),
                            _norm(fb.get("subject", "") + " " + fb.get("predicate", "")),
                        ).ratio()
                        >= 0.80
                    ),
                    None,
                )
                if b_match:
                    verdict = resolve_conflict(cr.fact, b_match, "day_extract")
                    v = verdict.get("verdict") if verdict else None
                    if v == "CONSENSUS":
                        cr.status = type("FS", (), {"name": "CONSENSUS"})()
                        cr.fact = dict(b_match)
                    elif v == "DIFFERENT_ASPECT":
                        cr.status = type("FS", (), {"name": "UNIQUE_A"})()
            for ref in refs:
                if ref.status.name != "CONFLICT":
                    user_all.append(dict(ref.fact))
            print(f"    [{eid}] user: A={len(sf)} B={len(xf)} → {len(user_all)} merged")

        # ── Section: text ──
        text_all = []
        if len(t["text"]) > 50:
            sf, _ = extract(_SYSTEM_TEXT_EXTRACT_STRICT, t["text"], "day_extract")
            xf, _ = extract(_SYSTEM_TEXT_EXTRACT_XPLORE, t["text"], "day_extract_b")
            refs = arbiter.consolidate(sf, xf)
            conflict_refs = [r for r in refs if r.status.name == "CONFLICT"]
            for cr in conflict_refs:
                b_match = next(
                    (
                        fb
                        for fb in xf
                        if SequenceMatcher(
                            None,
                            _norm(cr.fact.get("subject", "") + " " + cr.fact.get("predicate", "")),
                            _norm(fb.get("subject", "") + " " + fb.get("predicate", "")),
                        ).ratio()
                        >= 0.80
                    ),
                    None,
                )
                if b_match:
                    verdict = resolve_conflict(cr.fact, b_match, "day_extract")
                    v = verdict.get("verdict") if verdict else None
                    if v == "CONSENSUS":
                        cr.status = type("FS", (), {"name": "CONSENSUS"})()
                        cr.fact = dict(b_match)
                    elif v == "DIFFERENT_ASPECT":
                        cr.status = type("FS", (), {"name": "UNIQUE_A"})()
            for ref in refs:
                if ref.status.name != "CONFLICT":
                    text_all.append(dict(ref.fact))
            print(f"    [{eid}] text: A={len(sf)} B={len(xf)} → {len(text_all)} merged")

        # ── Cross-section dedup (user + text) ──
        merged = _section_dedup(user_all + text_all, [])
        final_facts[eid] = merged
        print(
            f"    [{eid}] final: user={len(user_all)} + text={len(text_all)} → {len(merged)} unique"
        )

    return final_facts


def run_dual_review(label, model_a, model_b, route_a, route_b, system_prompt, turns, final_facts):
    """Dual port review via WorkStealer — find missed facts in source."""
    print(f"\n[Phase {label}] {label} WorkStealer review...")
    ensure_dual(model_a, model_b, skip_if_healthy=False)

    route_map = {8082: route_a, 8083: route_b}
    items = [
        {
            "eid": str(t["id"])[:8],
            "source": t["user"] + "\n" + t["text"],
            "existing": final_facts.get(str(t["id"])[:8], []),
            "system_prompt": system_prompt,
        }
        for t in turns
    ]

    def process_fn(port, item, timeout):
        route = route_map[port]
        result, lat = review(item["source"], item["existing"], route, item["system_prompt"])
        new_facts = result.get("new_facts", []) if result else []
        return {
            "ok": True,
            "eid": item["eid"],
            "new_facts": new_facts,
            "lat": lat,
            "existing_count": len(item["existing"]),
        }

    stealer = WorkStealer(ports=[8082, 8083], item_timeout=300)
    raw_results = stealer.run(items, process_fn)

    new_facts = {}
    for r in raw_results:
        new_facts[r["eid"]] = r["new_facts"]
        print(
            f"  [{label}] {r['eid']}: {r['existing_count']} final → {len(r['new_facts'])} new ({r['lat']:.0f}s) via :{r.get('port', '?')}"
        )
    print(f"  [{label}] WorkStealer: {stealer.worker_report()}")
    return new_facts


def test():
    print("=" * 60)
    print("4B full pipeline → 7B vs 9B review comparison")
    print("=" * 60)
    turns = load_turns()
    print(f"  {len(turns)} turns loaded\n")

    # Phase 1: Full 4B extraction (extract → merge → conflict resolve → dedup)
    final = extract_full_pipeline(turns)

    # Phase 2: 7B unified review (WorkStealer dual ports)
    seven = run_dual_review(
        "7B",
        "day-verifier",
        "day-verifier-b",
        "day_verify",
        "day_verify_b",
        SYSTEM_REVIEW,
        turns,
        final,
    )
    # Phase 3: 9B unified review (WorkStealer dual ports)
    nine = run_dual_review(
        "9B",
        "day-enricher",
        "day-enricher-b",
        "day_enrich",
        "day_enrich_b",
        SYSTEM_REVIEW,
        turns,
        final,
    )

    # Comparison
    print("\n" + "=" * 60)
    print("COMPARISON: 7B vs 9B (review after full 4B pipeline)")
    print("=" * 60)
    header = f"{'Turn':<12} {'4B-Mg':>6} | {'7B':>6} {'9B':>6}"
    print(f"\n{header}")
    print("-" * len(header))

    tm = t7 = t9 = 0
    for t in turns:
        eid = str(t["id"])[:8]
        fm = len(final.get(eid, []))
        n7 = len(seven.get(eid, []))
        n9 = len(nine.get(eid, []))
        print(f"{eid:<12} {fm:>6} | {n7:>6} {n9:>6}")
        tm += fm
        t7 += n7
        t9 += n9

    print("-" * len(header))
    print(f"{'TOTAL':<12} {tm:>6} | {t7:>6} {t9:>6}")

    print("\n--- Summary ---")
    print(f"  7B adds: {t7} new facts")
    print(f"  9B adds: {t9} new facts")
    if t7 > t9:
        print(f"  Winner: 7B (+{t7 - t9} more)")
    elif t9 > t7:
        print(f"  Winner: 9B (+{t9 - t7} more)")
    else:
        print("  Tie")

    # Detail
    print(f"\n{'=' * 60}")
    print("4B FINAL FACTS (after extract → merge → LLM conflict → dedup)")
    for eid, facts in final.items():
        print(f"\n  [{eid}] {len(facts)} facts:")
        for f in facts:
            print(
                f"    [{f.get('subject', '?')}] {f.get('predicate', '?')} = {f.get('object', '?')}"
            )

    print(f"\n{'=' * 60}")
    print("7B NEW FACTS")
    for eid, facts in seven.items():
        print(f"\n  [{eid}] {len(facts)} new:")
        for f in facts:
            print(
                f"    [{f.get('subject', '?')}] {f.get('predicate', '?')} = {f.get('object', '?')}"
            )

    print(f"\n{'=' * 60}")
    print("9B NEW FACTS")
    for eid, facts in nine.items():
        print(f"\n  [{eid}] {len(facts)} new:")
        for f in facts:
            print(
                f"    [{f.get('subject', '?')}] {f.get('predicate', '?')} = {f.get('object', '?')}"
            )

    print("\nDone.")


if __name__ == "__main__":
    test()
