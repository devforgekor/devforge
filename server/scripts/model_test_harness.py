#!/usr/bin/env python3
"""model_test_harness.py — fixed turn-set A/B testing for extraction models.

Runs the same 100 turns through each candidate extraction model (Phase 1),
then Phi-4 verification (Phase 2). Results stored per extract_model via
UNIQUE (turn_id, fact_index, extract_model).
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


def load_testset() -> List[Dict]:
    """Load fixed test set of turns."""
    if not TESTSET_FILE.exists():
        print(f"ERROR: testset file not found: {TESTSET_FILE}")
        return []

    turn_ids = json.loads(TESTSET_FILE.read_text())
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


def run_extraction(turns: List[Dict], model_key: str) -> List[Dict]:
    """Phase 1: extract facts from all turns with given model."""
    model = MODELS[model_key]
    if not swap_model(model_key):
        print(f"FATAL: cannot load {model_key}")
        return []
    time.sleep(3)

    results = []
    print(f"  Extracting with {model['name']} ({len(turns)} turns)...")
    for i, turn in enumerate(turns):
        prompt = build_extract_prompt(turn)
        result = call_llm(model["name"], get_extract_system(model_key), prompt, max_tokens=512)
        facts = result.get("facts", []) if result else []
        results.append({
            "turn_index": i,
            "turn_id": turn["id"],
            "turn_data": turn,
            "facts": facts,
        })
        if (i + 1) % 20 == 0 or i == len(turns) - 1:
            total_facts = sum(len(r["facts"]) for r in results)
            print(f"    {i+1}/{len(turns)} turns, {total_facts} facts so far")
            # Early termination: too few facts at 40-turn mark
            if (i + 1) == 40 and total_facts / 40 < MIN_FACTS_PER_TURN:
                reason = f"facts/turn={total_facts/40:.1f} below threshold {MIN_FACTS_PER_TURN}"
                print(f"    ABORT extraction: {reason}")
                results.append({"__abort__": reason})
                return results
    return results


def run_verification(extracted: List[Dict], extract_model_name: str) -> Dict:
    """Phase 2: verify extracted facts with Phi-4."""
    if not swap_model(VERIFY_MODEL):
        print("FATAL: cannot load verify model")
        return {}
    time.sleep(3)

    total_valid = 0
    total_hallucinated = 0
    total_mismatch = 0
    total_context = 0
    total_facts = 0

    verify_model_name = MODELS[VERIFY_MODEL]["name"]
    print(f"  Verifying with {verify_model_name}...")

    verified_entries = 0
    for entry in extracted:
        if not entry["facts"]:
            continue

        prompt = build_verify_prompt(entry["turn_data"], entry["facts"])
        result = call_llm(verify_model_name, get_verify_system(VERIFY_MODEL), prompt, max_tokens=512)
        reviews = result.get("reviews", []) if result else []

        # Store Phi-4 verdicts for cross-validation comparison
        entry["_phi4_verdicts"] = {}
        for review in reviews:
            v = review.get("verdict", "pending")
            entry["_phi4_verdicts"][str(review.get("fact_index", 0))] = v
            if v == "valid":
                total_valid += 1
            elif v == "hallucinated":
                total_hallucinated += 1
            elif v == "mismatch":
                total_mismatch += 1
            elif v == "context_dependent":
                total_context += 1
        total_facts += len(entry["facts"])
        verified_entries += 1

        # Early termination: excessive hallucination at 20-verify mark
        if verified_entries == 20 and total_facts > 0:
            hallu_rate = total_hallucinated / total_facts
            if hallu_rate > MAX_HALLU_RATE:
                reason = f"hallucination_rate={hallu_rate:.1%} exceeds {MAX_HALLU_RATE:.0%}"
                print(f"    ABORT verification: {reason}")
                return {
                    "total_facts": total_facts,
                    "valid": total_valid,
                    "hallucinated": total_hallucinated,
                    "mismatch": total_mismatch,
                    "context_dependent": total_context,
                    "__abort__": reason,
                }

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

    return {
        "total_facts": total_facts,
        "valid": total_valid,
        "hallucinated": total_hallucinated,
        "mismatch": total_mismatch,
        "context_dependent": total_context,
    }


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

    cross_valid = 0
    cross_hallu = 0
    cross_mismatch = 0
    cross_context = 0
    total_facts = 0
    # Track agreements/disagreements with Phi-4
    agree = 0
    disagree = 0

    for entry_idx, entry in enumerate(extracted):
        if not entry["facts"]:
            continue
        prompt = build_verify_prompt(entry["turn_data"], entry["facts"])
        result = call_deepseek(CROSS_VERIFY_SYSTEM, prompt, max_tokens=512)
        reviews = result.get("reviews", []) if result else []

        for review in reviews:
            v = review.get("verdict", "pending")
            if v == "valid":
                cross_valid += 1
            elif v == "hallucinated":
                cross_hallu += 1
            elif v == "mismatch":
                cross_mismatch += 1
            elif v == "context_dependent":
                cross_context += 1
        total_facts += len(entry["facts"])

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

    cross_valid_rate = round(cross_valid / max(total_facts, 1) * 100, 1)
    return {
        "cross_valid": cross_valid,
        "cross_hallu": cross_hallu,
        "cross_mismatch": cross_mismatch,
        "cross_context": cross_context,
        "cross_total": total_facts,
        "cross_valid_rate": cross_valid_rate,
        "agree": agree,
        "disagree": disagree,
    }


def run_model(model_key: str, turns: List[Dict]) -> Dict:
    """Run full 2-phase pipeline for one extraction model."""
    model_name = MODELS[model_key]["name"]
    ts = datetime.now(timezone.utc)
    print(f"\n{'='*60}")
    print(f"[{ts.isoformat()}] Testing model: {model_name} ({model_key})")
    print(f"{'='*60}")

    t0 = time.time()
    extracted = run_extraction(turns, model_key)
    if not extracted:
        return {}
    # Check extraction abort
    abort_reason = None
    if extracted and "__abort__" in extracted[-1]:
        abort_reason = extracted[-1]["__abort__"]
        extracted = extracted[:-1]
    if abort_reason:
        print(f"\n  SKIPPED verification: {abort_reason}")
        stats = {"total_facts": 0, "valid": 0, "hallucinated": 0, "mismatch": 0, "context_dependent": 0, "__abort__": abort_reason}
        cross = {}
    else:
        stats = run_verification(extracted, model_name)
        # Phase 3: cross-validation with DeepSeek/Claude
        cross = run_cross_validation(extracted) if not stats.get("__abort__") else {}

    elapsed = time.time() - t0
    valid_rate = round(stats.get("valid", 0) / max(stats.get("total_facts", 1), 1) * 100, 1)
    hallu_rate = round(stats.get("hallucinated", 0) / max(stats.get("total_facts", 1), 1) * 100, 1)

    print(f"\n  Results for {model_name}:")
    if stats.get("__abort__"):
        print(f"    ABORTED: {stats['__abort__']}")
    print(f"    Facts: {stats['total_facts']}")
    print(f"    Phi-4 valid: {stats['valid']} ({valid_rate}%)")
    print(f"    Phi-4 hallucinated: {stats['hallucinated']} ({hallu_rate}%)")
    print(f"    Phi-4 mismatch: {stats.get('mismatch', 0)}")
    print(f"    Phi-4 context_dep: {stats.get('context_dependent', 0)}")
    if cross:
        print(f"    Cross-validator ({CROSS_VERIFY_MODEL}):")
        print(f"      Valid: {cross.get('cross_valid', 0)} ({cross.get('cross_valid_rate', 0)}%)")
        print(f"      Hallucinated: {cross.get('cross_hallu', 0)}")
        print(f"      Agree/Disagree with Phi-4: {cross.get('agree', 0)}/{cross.get('disagree', 0)}")
    print(f"    Elapsed: {elapsed/60:.1f} min")

    stats["model_name"] = model_name
    stats["model_key"] = model_key
    stats["elapsed_min"] = round(elapsed / 60, 1)
    stats["accuracy_pct"] = valid_rate
    if cross:
        stats["cross_valid_rate"] = cross.get("cross_valid_rate", 0)
        stats["cross_agree"] = cross.get("agree", 0)
        stats["cross_disagree"] = cross.get("disagree", 0)
    return stats


def main():
    import argparse
    ap = argparse.ArgumentParser(description="A/B test extraction models on fixed turn set")
    ap.add_argument("--model", type=str, default="qwen14",
                    help="Model key to test (default: qwen14)")
    ap.add_argument("--compare", nargs="+", default=[],
                    help="Compare multiple models (e.g. --compare qwen14 granite_8b)")
    args = ap.parse_args()

    turns = load_testset()
    if not turns:
        print("ERROR: no turns loaded from test set")
        return 1

    print(f"Loaded {len(turns)} turns for testing")

    if args.compare:
        models_to_test = args.compare
    else:
        models_to_test = [args.model]

    results = {}
    for mk in models_to_test:
        if mk not in MODELS:
            print(f"ERROR: unknown model key '{mk}'")
            continue
        stats = run_model(mk, turns)
        if stats:
            results[mk] = stats

    # Comparison summary
    if len(results) > 1:
        print(f"\n{'='*60}")
        print("COMPARISON SUMMARY")
        print(f"{'='*60}")
        print(f"{'Model':<30} {'Facts':>6} {'Valid%':>8} {'Hallu%':>8} {'Time':>8}")
        print("-" * 60)
        for mk, s in results.items():
            hallu_rate = round(s.get("hallucinated", 0) / max(s.get("total_facts", 1), 1) * 100, 1)
            print(f"{s['model_name']:<30} {s['total_facts']:>6} {s['accuracy_pct']:>7}% {hallu_rate:>7}% {s['elapsed_min']:>7.1f}m")

    return 0


if __name__ == "__main__":
    sys.exit(main())
