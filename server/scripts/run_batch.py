#!/usr/bin/env python3
"""run_batch.py — batch extraction → single Phi-4 verify → single DeepSeek cross-verify.

Minimizes model swaps: Phi-4 loads once, DeepSeek API session reuses connection.
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

TESTSET_FILE = Path("/opt/projects/server/model_testset.json")
MIN_FACTS_PER_TURN = 0.3
MAX_HALLU_RATE = 0.5
BATCH_DIR = Path("/opt/projects/server/batch_results")
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


def load_testset() -> List[Dict]:
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
                    "id": parts[0], "agent": parts[1],
                    "user_turn": parts[2][:300], "text": parts[3][:300],
                    "created_at": parts[4],
                })
    return turns


def run_extraction(turns: List[Dict], model_key: str) -> List[Dict]:
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
        results.append({"turn_index": i, "turn_id": turn["id"], "turn_data": turn, "facts": facts})
        if (i + 1) % 20 == 0 or i == len(turns) - 1:
            total_facts = sum(len(r["facts"]) for r in results)
            print(f"    {i+1}/{len(turns)} turns, {total_facts} facts so far")
            if (i + 1) == 40 and total_facts / 40 < MIN_FACTS_PER_TURN:
                reason = f"facts/turn={total_facts/40:.1f} below threshold {MIN_FACTS_PER_TURN}"
                print(f"    ABORT extraction: {reason}")
                results.append({"__abort__": reason})
                return results
    return results


def run_verification_batch(all_extracted: Dict[str, List[Dict]]) -> Dict:
    """Verify all models' facts with Phi-4 loaded once."""
    verify_model = MODELS[VERIFY_MODEL]
    if not swap_model(VERIFY_MODEL):
        print("FATAL: cannot load verify model")
        return {}
    time.sleep(3)

    verify_model_name = verify_model["name"]
    stats = {}

    for model_key, extracted in all_extracted.items():
        model_name = MODELS[model_key]["name"]
        if not extracted or (len(extracted) > 0 and "__abort__" in extracted[-1]):
            abort = extracted[-1].get("__abort__", "extraction failed") if extracted else "no data"
            stats[model_key] = {"total_facts": 0, "valid": 0, "hallucinated": 0,
                                "mismatch": 0, "context_dependent": 0, "__abort__": abort}
            continue

        print(f"\n  Verifying {model_name} with {verify_model_name}...")
        total_valid = total_hallu = total_mismatch = total_context = total_facts = 0
        verified_entries = 0

        for entry in extracted:
            if not entry["facts"]:
                continue
            prompt = build_verify_prompt(entry["turn_data"], entry["facts"])
            result = call_llm(verify_model_name, get_verify_system(VERIFY_MODEL), prompt, max_tokens=512)
            reviews = result.get("reviews", []) if result else []

            entry["_phi4_verdicts"] = {}
            for review in reviews:
                v = review.get("verdict", "pending")
                entry["_phi4_verdicts"][str(review.get("fact_index", 0))] = v
                if v == "valid": total_valid += 1
                elif v == "hallucinated": total_hallu += 1
                elif v == "mismatch": total_mismatch += 1
                elif v == "context_dependent": total_context += 1
            total_facts += len(entry["facts"])
            verified_entries += 1

            if verified_entries == 20 and total_facts > 0:
                hallu_rate = total_hallu / total_facts
                if hallu_rate > MAX_HALLU_RATE:
                    reason = f"hallucination_rate={hallu_rate:.1%} exceeds {MAX_HALLU_RATE:.0%}"
                    print(f"    ABORT verification: {reason}")
                    stats[model_key] = {"total_facts": total_facts, "valid": total_valid,
                                        "hallucinated": total_hallu, "mismatch": total_mismatch,
                                        "context_dependent": total_context, "__abort__": reason}
                    break
            else:
                # Save facts to DB
                for review in reviews:
                    idx = review.get("fact_index", 0)
                    fact = entry["facts"][idx] if idx < len(entry["facts"]) else {}
                    evidence = esc_sql(fact.get("evidence", "")[:500])
                    fact_type = esc_sql(fact.get("fact_type", ""))
                    verdict = esc_sql(review.get("verdict", "pending"))
                    reason_text = esc_sql(review.get("reason", "")[:500])
                    psql(f"""
                    INSERT INTO review_facts (turn_id, fact_index, evidence, fact_type, verdict, reason, extract_model, verify_model)
                    VALUES ('{entry["turn_id"]}', {idx}, '{evidence}', '{fact_type}', '{verdict}', '{reason_text}',
                            '{esc_sql(model_name)}', '{esc_sql(verify_model_name)}')
                    ON CONFLICT (turn_id, fact_index, extract_model) DO UPDATE SET
                        verdict = EXCLUDED.verdict, reason = EXCLUDED.reason, verify_model = EXCLUDED.verify_model
                    """)

        if not stats.get(model_key):
            valid_rate = round(total_valid / max(total_facts, 1) * 100, 1)
            print(f"    {model_name}: {total_valid}/{total_facts} valid ({valid_rate}%), {total_hallu} hallucinated")
            stats[model_key] = {"total_facts": total_facts, "valid": total_valid,
                                "hallucinated": total_hallu, "mismatch": total_mismatch,
                                "context_dependent": total_context, "accuracy_pct": valid_rate}

    return stats


def run_cross_validation_batch(all_extracted: Dict[str, List[Dict]]) -> Dict:
    """Cross-validate all models' facts with DeepSeek API."""
    if not DEEPSEEK_API_KEY:
        print("  SKIP cross-validation: no DEEPSEEK_API_KEY")
        return {}

    cross_stats = {}
    for model_key, extracted in all_extracted.items():
        model_name = MODELS[model_key]["name"]
        if not extracted:
            continue

        entries_with_facts = [e for e in extracted if e.get("facts")]
        if not entries_with_facts:
            continue

        print(f"  Cross-validating {model_name} ({len(entries_with_facts)} entries)...")
        cross_valid = cross_hallu = cross_mismatch = cross_context = total_facts = agree = disagree = 0

        for entry in entries_with_facts:
            prompt = build_verify_prompt(entry["turn_data"], entry["facts"])
            result = call_deepseek(CROSS_VERIFY_SYSTEM, prompt, max_tokens=512)
            reviews = result.get("reviews", []) if result else []

            for review in reviews:
                v = review.get("verdict", "pending")
                if v == "valid": cross_valid += 1
                elif v == "hallucinated": cross_hallu += 1
                elif v == "mismatch": cross_mismatch += 1
                elif v == "context_dependent": cross_context += 1

                # Compare with Phi-4
                idx = str(review.get("fact_index", 0))
                phi4_v = entry.get("_phi4_verdicts", {}).get(idx, "")
                if phi4_v:
                    if review.get("verdict") == phi4_v:
                        agree += 1
                    else:
                        disagree += 1

            total_facts += len(entry["facts"])

        cross_valid_rate = round(cross_valid / max(total_facts, 1) * 100, 1)
        print(f"    Cross: {cross_valid}/{total_facts} valid ({cross_valid_rate}%), agree/disagree: {agree}/{disagree}")
        cross_stats[model_key] = {
            "cross_valid": cross_valid, "cross_hallu": cross_hallu,
            "cross_mismatch": cross_mismatch, "cross_context": cross_context,
            "cross_total": total_facts, "cross_valid_rate": cross_valid_rate,
            "agree": agree, "disagree": disagree,
        }

    return cross_stats


def call_deepseek(system_prompt: str, user_prompt: str, max_tokens: int = 512, retries: int = 2) -> Optional[Dict]:
    payload = json.dumps({
        "model": CROSS_VERIFY_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens, "temperature": 0.0,
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


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Batch model testing: extract all → verify once → cross-validate once")
    ap.add_argument("--models", nargs="+", default=["granite_8b", "qwen_coder_7b", "deepseek_r1"],
                    help="Models to test in batch")
    args = ap.parse_args()

    BATCH_DIR.mkdir(exist_ok=True)

    turns = load_testset()
    if not turns:
        print("ERROR: no turns loaded")
        return 1
    print(f"Loaded {len(turns)} turns for testing\n{'='*60}")

    # ── Phase 1: Extract with all models ──
    all_extracted = {}
    for model_key in args.models:
        if model_key not in MODELS:
            print(f"ERROR: unknown model key '{model_key}'")
            continue
        ts = datetime.now(timezone.utc)
        print(f"\n[{ts.isoformat()}] Phase 1 — Extracting with {MODELS[model_key]['name']} ({model_key})")
        extracted = run_extraction(turns, model_key)
        abort = next((e.get("__abort__") for e in extracted if "__abort__" in e), None)
        if abort:
            print(f"  ABORT {model_key}: {abort}")
        all_extracted[model_key] = extracted

        # Save intermediate results
        batch_file = BATCH_DIR / f"{model_key}_extracted.json"
        batch_file.write_text(json.dumps(
            [{"turn_id": e["turn_id"], "facts": e["facts"]} for e in extracted if "__abort__" not in e],
            indent=2, ensure_ascii=False))

    # ── Phase 2: Phi-4 verify all ──
    ts = datetime.now(timezone.utc)
    print(f"\n{'='*60}")
    print(f"[{ts.isoformat()}] Phase 2 — Phi-4 verification (single load)")
    print(f"{'='*60}")
    verify_stats = run_verification_batch(all_extracted)

    # ── Phase 3: DeepSeek cross-verify all ──
    ts = datetime.now(timezone.utc)
    print(f"\n{'='*60}")
    print(f"[{ts.isoformat()}] Phase 3 — DeepSeek cross-validation")
    print(f"{'='*60}")
    cross_stats = run_cross_validation_batch(all_extracted)

    # ── Summary ──
    print(f"\n{'='*60}")
    print("BATCH SUMMARY")
    print(f"{'='*60}")
    print(f"{'Model':<25} {'Facts':>6} {'Phi4%':>7} {'Cross%':>7} {'Agree':>6} {'Disagree':>8}")
    print("-" * 60)
    for model_key in args.models:
        if model_key not in all_extracted:
            continue
        vs = verify_stats.get(model_key, {})
        cs = cross_stats.get(model_key, {})
        facts = vs.get("total_facts", 0)
        phi4_rate = vs.get("accuracy_pct", 0)
        cross_rate = cs.get("cross_valid_rate", 0)
        agree = cs.get("agree", 0)
        disagree = cs.get("disagree", 0)
        abort = vs.get("__abort__", "")
        flag = f" ABORT:{abort}" if abort else ""
        print(f"{MODELS[model_key]['name']:<25} {facts:>6} {phi4_rate:>6}% {cross_rate:>6}% {agree:>6} {disagree:>8}{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
