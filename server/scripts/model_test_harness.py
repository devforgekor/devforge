#!/usr/bin/env python3
"""model_test_harness.py — batch extraction model comparison.

Phase 1: Extract facts from 100-turn testset with ALL candidate models sequentially.
Phase 2: Load Phi-4 ONCE, verify all extracted facts from all models in one batch.
Phase 3: Cross-validate with DeepSeek API.

Results stored per extract_model via UNIQUE (turn_id, fact_index, extract_model).
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List

from lib.db import psql, esc_sql
from review_worker import (
    MODELS, VERIFY_MODEL,
    get_extract_system, get_verify_system,
    swap_model, call_llm,
    build_extract_prompt, build_verify_prompt,
)

# Cross-validator: Claude/DeepSeek via API
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
CROSS_VERIFY_MODEL = "deepseek-chat"
CROSS_VERIFY_SYSTEM = """You are a critical fact checker. Review the extracted facts against the original turn content.

For each fact, check:
1. Is the evidence VERBATIM from the turn? Mark "hallucinated" if not.
2. Is the fact_type correct? Mark "mismatch" if type is wrong.
3. Is the fact truly self-contained? Mark "context_dependent" if it requires prior knowledge.

Return JSON:
{"reviews": [{"fact_index": 0, "verdict": "valid|hallucinated|mismatch|context_dependent", "reason": "short explanation"}]}"""

TESTSET_FILE = Path("/opt/projects/server/model_testset.json")

# Early termination thresholds
MIN_FACTS_PER_TURN = 0.3   # abort extraction if facts/turn below this at 40-turn mark
MAX_HALLU_RATE = 0.5       # abort verification if hallucination rate exceeds this at 20-verify mark


def load_testset(testset_file: Path = None) -> List[Dict]:
    """Load fixed test set of turns."""
    tf = testset_file or TESTSET_FILE
    if not tf.exists():
        print(f"ERROR: testset file not found: {tf}")
        return []

    turn_ids = json.loads(tf.read_text())
    id_list = [t["id"] for t in turn_ids]

    turns = []
    for tid in id_list:
        row = psql(f"""
        SELECT t.id, t.agent,
               regexp_replace(t.user_turn, E'[\\n\\r\\\\|]+', ' ', 'g'),
               regexp_replace(t.text, E'[\\n\\r\\\\|]+', ' ', 'g'),
               t.created_at
        FROM turns t WHERE t.id = '{tid}'
        """)
        if row and row.strip():
            parts = row.split("|")
            if len(parts) >= 5 and parts[0].strip():
                turns.append({
                    "id": parts[0],
                    "agent": parts[1],
                    "user_turn": parts[2][:300],
                    "text": parts[3][:300],
                    "created_at": parts[4],
                })
    return turns


def run_extraction(turns: List[Dict], model_key: str, skip_turn_ids: set = None) -> List[Dict]:
    """Phase 1: extract facts from all turns with given model."""
    model = MODELS[model_key]
    if not swap_model(model_key):
        print(f"FATAL: cannot load {model_key}")
        return []
    time.sleep(3)

    skip = skip_turn_ids or set()
    model_name = model["name"]
    to_extract = [t for t in turns if t["id"] not in skip]
    skipped = len(turns) - len(to_extract)
    results = []
    if skipped:
        print(f"  Extracting with {model_name} ({len(to_extract)} turns, {skipped} already in DB)...")
    else:
        print(f"  Extracting with {model_name} ({len(to_extract)} turns)...")
    for i, turn in enumerate(to_extract):
        prompt = build_extract_prompt(turn)
        result = call_llm(model_name, get_extract_system(model_key), prompt, max_tokens=512, timeout=300)
        facts = result.get("facts", []) if result else []
        results.append({
            "turn_index": i,
            "turn_id": turn["id"],
            "turn_data": turn,
            "facts": facts,
            "_extract_model": model_name,
        })
        if (i + 1) % 20 == 0 or i == len(to_extract) - 1:
            total_facts = sum(len(r["facts"]) for r in results)
            print(f"    {i+1}/{len(to_extract)} turns, {total_facts} facts so far")
            # Early termination: too few facts at 40-turn mark (only if total turns >= 40)
            if len(to_extract) >= 40 and (i + 1) == 40 and total_facts / 40 < MIN_FACTS_PER_TURN:
                reason = f"facts/turn={total_facts/40:.1f} below threshold {MIN_FACTS_PER_TURN}"
                print(f"    ABORT extraction: {reason}")
                results.append({"__abort__": reason, "_extract_model": model_name})
                return results
    return results


def run_verification(extracted: List[Dict]) -> Dict:
    """Phase 2: verify extracted facts with Phi-4. Each entry carries _extract_model. Returns per-model stats dict."""
    if not swap_model(VERIFY_MODEL):
        print("FATAL: cannot load verify model")
        return {}
    time.sleep(3)

    # Query already-verified (turn_id, extract_model) pairs for resume
    already = set()
    try:
        rows = psql("SELECT DISTINCT turn_id, extract_model FROM review_facts")
        if rows and rows.strip():
            for row in rows.strip().split("\n"):
                parts = row.split("|")
                if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
                    already.add((parts[0].strip(), parts[1].strip()))
    except Exception:
        pass

    per_model: Dict[str, Dict] = {}

    verify_model_name = MODELS[VERIFY_MODEL]["name"]
    print(f"  Verifying with {verify_model_name}...")

    # Seed per_model stats from DB for already-verified entries
    try:
        stat_rows = psql("SELECT extract_model, COUNT(*), "
                         "COUNT(*) FILTER (WHERE verdict='valid'), "
                         "COUNT(*) FILTER (WHERE verdict='hallucinated'), "
                         "COUNT(*) FILTER (WHERE verdict='mismatch'), "
                         "COUNT(*) FILTER (WHERE verdict='context_dependent') "
                         "FROM review_facts GROUP BY extract_model")
        if stat_rows and stat_rows.strip():
            for row in stat_rows.strip().split("\n"):
                parts = row.split("|")
                if len(parts) >= 6:
                    per_model[parts[0].strip()] = {
                        "total_facts": int(parts[1].strip()),
                        "valid": int(parts[2].strip()),
                        "hallucinated": int(parts[3].strip()),
                        "mismatch": int(parts[4].strip()),
                        "context_dependent": int(parts[5].strip()),
                    }
    except Exception:
        pass

    # Split: entries to verify vs already done
    to_verify = []
    skipped = 0
    for entry in extracted:
        if not entry.get("facts"):
            continue
        key = (entry["turn_id"], entry.get("_extract_model", "unknown"))
        if key in already:
            skipped += 1
        else:
            em = entry.get("_extract_model", "unknown")
            if em not in per_model:
                per_model[em] = {"total_facts": 0, "valid": 0, "hallucinated": 0,
                                 "mismatch": 0, "context_dependent": 0}
            to_verify.append(entry)

    if skipped:
        print(f"  Resume: {skipped} entries already in DB, {len(to_verify)} remaining")

    verified_entries = 0
    total_facts_new = 0
    is_resume = skipped > 0
    for entry in to_verify:
        extract_model_name = entry.get("_extract_model", "unknown")
        ms = per_model[extract_model_name]

        prompt = build_verify_prompt(entry["turn_data"], entry["facts"])
        result = call_llm(verify_model_name, get_verify_system(VERIFY_MODEL), prompt, max_tokens=512, timeout=180)
        reviews = result.get("reviews", []) if result else []

        # Store Phi-4 verdicts for cross-validation comparison
        entry["_phi4_verdicts"] = {}
        for review in reviews:
            v = review.get("verdict", "pending")
            entry["_phi4_verdicts"][str(review.get("fact_index", 0))] = v
            if v == "valid":
                ms["valid"] += 1
            elif v == "hallucinated":
                ms["hallucinated"] += 1
            elif v == "mismatch":
                ms["mismatch"] += 1
            elif v == "context_dependent":
                ms["context_dependent"] += 1
        ms["total_facts"] += len(entry["facts"])
        total_facts_new += len(entry["facts"])
        verified_entries += 1

        # Early termination: excessive hallucination at 20-verify mark (only for fresh runs)
        if not is_resume and verified_entries == 20 and total_facts_new > 0:
            total_hallu = sum(m["hallucinated"] for m in per_model.values())
            hallu_rate = total_hallu / sum(m["total_facts"] for m in per_model.values())
            if hallu_rate > MAX_HALLU_RATE:
                reason = f"hallucination_rate={hallu_rate:.1%} exceeds {MAX_HALLU_RATE:.0%}"
                print(f"    ABORT verification: {reason}")
                per_model["__abort__"] = reason
                return per_model

        # Save each fact
        for review in reviews:
            idx = review.get("fact_index", 0)
            fact = entry["facts"][idx] if idx < len(entry["facts"]) else {}
            evidence = esc_sql(fact.get("evidence", "")[:500])
            fact_type = esc_sql(fact.get("fact_type", ""))
            verdict = esc_sql(review.get("verdict", "pending"))
            reason = esc_sql(review.get("reason", "")[:500])

            psql(f"""
            INSERT INTO review_facts (turn_id, fact_index, evidence, fact_type, verdict, reason, extract_model, verify_model)
            VALUES ('{entry["turn_id"]}', {idx}, '{evidence}', '{fact_type}', '{verdict}', '{reason}',
                    '{esc_sql(extract_model_name)}', '{esc_sql(verify_model_name)}')
            ON CONFLICT (turn_id, fact_index, extract_model) DO UPDATE SET
                verdict = EXCLUDED.verdict,
                reason = EXCLUDED.reason,
                verify_model = EXCLUDED.verify_model
            """)

    if verified_entries:
        print(f"    {verified_entries} newly verified entries, {total_facts_new} facts")

    return per_model


def call_deepseek(system_prompt: str, user_prompt: str, max_tokens: int = 512, retries: int = 2) -> Optional[Dict]:
    """Call DeepSeek API for cross-validation."""
    payload = json.dumps({
        "model": CROSS_VERIFY_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }).encode()
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(DEEPSEEK_URL, data=payload, headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            })
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode()
                content = json.loads(body)["choices"][0]["message"]["content"]
                match = re.search(r'\{.*\}', content, re.DOTALL)
                if match:
                    return json.loads(match.group(0))
                return None
        except Exception as e:
            print(f"    DeepSeek API error: {e} (attempt {attempt+1}/{retries+1})")
            time.sleep(2 ** attempt)
    return None


def run_cross_validation(extracted: List[Dict]) -> Dict:
    """Phase 3: cross-validate facts using DeepSeek/Claude API."""
    if not DEEPSEEK_API_KEY:
        print("  SKIP cross-validation: no DEEPSEEK_API_KEY")
        return {}
    print(f"  Cross-validating with {CROSS_VERIFY_MODEL} ({len(extracted)} entries)...")

    # Load Phi-4 verdicts from DB for entries missing _phi4_verdicts (resumed runs)
    missing_verdicts = sum(1 for e in extracted if not e.get("_phi4_verdicts") and e.get("facts"))
    if missing_verdicts:
        print(f"  Loading {missing_verdicts} Phi-4 verdicts from DB...")
        for entry in extracted:
            if entry.get("_phi4_verdicts") or not entry.get("facts"):
                continue
            entry["_phi4_verdicts"] = {}
            tid = entry["turn_id"]
            em = esc_sql(entry.get("_extract_model", "unknown"))
            rows = psql(f"SELECT fact_index, verdict FROM review_facts "
                        f"WHERE turn_id='{tid}' AND extract_model='{em}'")
            if rows and rows.strip():
                for row in rows.strip().split("\n"):
                    parts = row.split("|")
                    if len(parts) >= 2:
                        entry["_phi4_verdicts"][parts[0].strip()] = parts[1].strip()

    xstats: Dict[str, Dict] = {}
    agree = 0
    disagree = 0

    for entry_idx, entry in enumerate(extracted):
        if not entry.get("facts"):
            continue
        em = entry.get("_extract_model", "unknown")
        if em not in xstats:
            xstats[em] = {"cross_valid": 0, "cross_hallu": 0, "cross_mismatch": 0,
                          "cross_context": 0, "cross_total": 0}
        xs = xstats[em]
        prompt = build_verify_prompt(entry["turn_data"], entry["facts"])
        result = call_deepseek(CROSS_VERIFY_SYSTEM, prompt, max_tokens=512)
        reviews = result.get("reviews", []) if result else []

        for review in reviews:
            v = review.get("verdict", "pending")
            if v == "valid":
                xs["cross_valid"] += 1
            elif v == "hallucinated":
                xs["cross_hallu"] += 1
            elif v == "mismatch":
                xs["cross_mismatch"] += 1
            elif v == "context_dependent":
                xs["cross_context"] += 1
        xs["cross_total"] += len(entry["facts"])

        # Compare with Phi-4 verdicts (stored in entry["facts"] as phi4_verdict)
        if reviews:
            for r in reviews:
                idx = r.get("fact_index", -1)
                phi4_v = entry.get("_phi4_verdicts", {}).get(str(idx), "")
                if phi4_v:
                    if r.get("verdict") == phi4_v:
                        agree += 1
                    else:
                        disagree += 1

        if (entry_idx + 1) % 20 == 0:
            print(f"    cross-verify {entry_idx+1}/{len(extracted)} entries, "
                  f"{agree} agree, {disagree} disagree so far")

    return {"per_model": xstats, "agree": agree, "disagree": disagree}


EXTRACT_CACHE = Path("/var/tmp/batch_extracted.json")  # overwritten per-testset in main()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Batch test extraction models — extract all then verify once")
    ap.add_argument("--models", nargs="+", default=["granite_8b", "qwen_coder_7b"],
                    help="Model keys to test (default: granite_8b qwen_coder_7b)")
    ap.add_argument("--skip-cross", action="store_true", help="Skip cross-validation phase")
    ap.add_argument("--testset", help="Path to custom test set JSON (default: model_testset.json)")
    ap.add_argument("--resume", metavar="FILE", help="Resume from saved extraction JSON (skip Phase 1)")
    args = ap.parse_args()

    testset_file = Path(args.testset) if args.testset else TESTSET_FILE
    global EXTRACT_CACHE
    EXTRACT_CACHE = Path(f"/var/tmp/batch_extracted_{testset_file.stem}.json")
    turns = load_testset(testset_file)
    if not turns:
        print("ERROR: no turns loaded from test set")
        return 1
    print(f"Loaded {len(turns)} turns for testing")

    t0 = time.time()
    extract_stats = {}

    # ── Phase 1: Extract with each model sequentially ──
    if args.resume:
        resume_path = Path(args.resume)
        if not resume_path.exists():
            print(f"ERROR: resume file not found: {args.resume}")
            return 1
        all_extracted = json.loads(resume_path.read_text())
        # Rebuild extract_stats from loaded data
        for mk in set(e.get("_extract_model", "unknown") for e in all_extracted):
            model_facts = sum(len(e["facts"]) for e in all_extracted if e.get("_extract_model") == mk)
            extract_stats[mk] = {"total_facts": model_facts, "elapsed_min": 0, "aborted": False, "abort_reason": None}
        print(f"Resumed {len(all_extracted)} extracted entries from {args.resume}")
    else:
        models_to_test = [m for m in args.models if m in MODELS]
        if not models_to_test:
            print("ERROR: no valid models specified")
            return 1

        all_extracted = []
        for mk in models_to_test:
            ts = datetime.now(timezone.utc)
            model_name = MODELS[mk]["name"]
            print(f"\n{'='*60}")
            print(f"[{ts.isoformat()}] Phase 1 — Extracting with {model_name} ({mk})")
            print(f"{'='*60}")

            model_t0 = time.time()
            # Query already-verified turns in DB to skip extraction
            skip_ids = set()
            try:
                rows = psql(f"SELECT DISTINCT turn_id FROM review_facts "
                            f"WHERE extract_model='{MODELS[mk]['name']}'")
                if rows and rows.strip():
                    for row in rows.strip().split("\n"):
                        tid = row.split("|")[0].strip()
                        if tid:
                            skip_ids.add(tid)
                if skip_ids:
                    print(f"  DB has {len(skip_ids)} verified turns — will skip extraction")
            except Exception:
                pass
            extracted = run_extraction(turns, mk, skip_turn_ids=skip_ids)
            if not extracted:
                print(f"  SKIP: no extraction results for {mk}")
                continue

            abort_reason = None
            if "__abort__" in extracted[-1]:
                abort_reason = extracted[-1]["__abort__"]
                extracted = extracted[:-1]
                print(f"  ABORTED: {abort_reason}")

            total_facts = sum(len(e["facts"]) for e in extracted)
            elapsed_m = (time.time() - model_t0) / 60
            extract_stats[mk] = {"total_facts": total_facts, "elapsed_min": round(elapsed_m, 1),
                                 "aborted": bool(abort_reason), "abort_reason": abort_reason}
            print(f"  Done: {total_facts} facts from {len(extracted)} turns in {elapsed_m:.1f}m")
            all_extracted.extend(extracted)

        # Save extraction results for potential resume
        EXTRACT_CACHE.write_text(json.dumps(all_extracted, indent=2))
        print(f"Extraction cache saved to {EXTRACT_CACHE}")

    if not all_extracted:
        print("ERROR: no facts extracted by any model")
        return 1

    # ── Phase 2: Verify ALL facts with Phi-4 (load once) ──
    ts = datetime.now(timezone.utc)
    total_extracted = sum(len(e.get("facts", [])) for e in all_extracted)
    print(f"\n{'='*60}")
    print(f"[{ts.isoformat()}] Phase 2 — Verifying {total_extracted} facts ({len(all_extracted)} entries) with Phi-4")
    print(f"{'='*60}")

    verify_stats = run_verification(all_extracted)

    # ── Phase 3: Cross-validation with DeepSeek ──
    cross_stats = {}
    if not args.skip_cross:
        ts = datetime.now(timezone.utc)
        print(f"\n{'='*60}")
        print(f"[{ts.isoformat()}] Phase 3 — Cross-validating with {CROSS_VERIFY_MODEL}")
        print(f"{'='*60}")
        cross_stats = run_cross_validation(all_extracted)

    # ── Comparison summary ──
    total_elapsed = (time.time() - t0) / 60
    print(f"\n{'='*60}")
    print("COMPARISON SUMMARY")
    print(f"{'='*60}")
    header = f"{'Model':<30} {'Facts':>6} {'Valid%':>8} {'Hallu%':>8} {'Cross%':>8} {'Time':>8}"
    print(header)
    print("-" * len(header))
    tested_models = list(extract_stats.keys())
    # Map model names back to keys for comparison table
    tested_keys = [mk for mk in (args.models if not args.resume else [m.split(":")[0] for m in tested_models])]
    # Use model names found in extract_stats / verify_stats
    tested_names = sorted(set(
        list(verify_stats.keys()) + list(extract_stats.keys())
    ) - {"__abort__"}, key=lambda n: str(n))
    for model_name in tested_names:
        # Find matching model key
        mk = None
        for k, v in MODELS.items():
            if v["name"] == model_name:
                mk = k
                break
        if mk is None:
            continue
        vs = verify_stats.get(model_name, {})
        xs = cross_stats.get("per_model", {}).get(model_name, {})
        facts = vs.get("total_facts", 0)
        valid = vs.get("valid", 0)
        hallu = vs.get("hallucinated", 0)
        valid_pct = round(valid / max(facts, 1) * 100, 1)
        hallu_pct = round(hallu / max(facts, 1) * 100, 1)
        x_valid = xs.get("cross_valid", 0)
        x_total = xs.get("cross_total", 0)
        x_pct = round(x_valid / max(x_total, 1) * 100, 1) if x_total else "-"
        ext_t = extract_stats.get(mk, {}).get("elapsed_min", 0) if mk else 0
        print(f"{model_name:<30} {facts:>6} {valid_pct:>7}% {hallu_pct:>7}% {str(x_pct):>7} {ext_t:>7.1f}m")

    if cross_stats:
        print(f"\nCross-validator ({CROSS_VERIFY_MODEL}) agree/disagree with Phi-4: "
              f"{cross_stats.get('agree', 0)}/{cross_stats.get('disagree', 0)}")
    print(f"Total elapsed: {total_elapsed:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
