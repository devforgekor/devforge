#!/usr/bin/env python3
"""test_review_models.py — compare model combinations for review pipeline.

Picks sample turns from DB, runs each model pair (1차→2차) for fact extraction
plus verification, and scores the results so the best combination can be chosen.

Usage:
  python3 test_review_models.py               # test all 6 combos
  python3 test_review_models.py --limit 10     # only 10 turns
  python3 test_review_models.py --combo A      # single combo only
"""

import argparse
import http.client as hc
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List

SYMLINK = Path("/opt/ai_data/models/gguf/current.gguf")
MODEL_DIR = Path("/opt/ai_data/models/gguf")
LITELLM_URL = "http://localhost:4000/v1/chat/completions"
LITELLM_KEY = "devforge-litellm-key"

MODELS = {
    "qwen14": {
        "name": "qwen2.5-coder-14b",
        "file": "Qwen2.5-Coder-14B-Instruct-Q8_0.gguf",
        "family": "Alibaba",
    },
    "phi4": {
        "name": "phi-4",
        "file": "phi-4-Q8_0.gguf",
        "family": "Microsoft",
    },
    "gemma27": {
        "name": "gemma-2-27b",
        "file": "gemma-2-27b-it-Q4_K_M.gguf",
        "family": "Google",
    },
}

COMBOS = {
    "A": ("qwen14", "phi4"),
    "B": ("qwen14", "gemma27"),
    "C": ("phi4", "qwen14"),
    "D": ("phi4", "gemma27"),
    "E": ("gemma27", "qwen14"),
    "F": ("gemma27", "phi4"),
}

EXTRACT_SYSTEM = """You are a fact extraction system. From the conversation turn below, extract ONLY self-contained, testable facts.

Rules:
- Skip context-dependent replies (e.g. "yes", "ok", "apply it", "진행해")
- Skip questions that reference unknown prior topics
- KEEP decisions, data, observations that make sense without prior context
- KEEP technical facts, code choices, architecture decisions

Return a JSON object:
{"facts": [{"evidence": "verbatim quote from turn", "speaker": "agent name", "fact_type": "statement|decision|data_given"}]}

If no self-contained facts, return {"facts": []}."""

VERIFY_SYSTEM = """You are a critical fact checker. Review the extracted facts against the original turn content.

For each fact, check:
1. Is the evidence VERBATIM from the turn? Mark "hallucinated" if not.
2. Is the fact_type correct? Mark "mismatch" if type is wrong.
3. Is the fact truly self-contained? Mark "context_dependent" if it requires prior knowledge.

Return JSON:
{"reviews": [{"fact_index": 0, "verdict": "valid|hallucinated|mismatch|context_dependent", "reason": "short explanation"}]}"""

from lib.db import psql as _psql


def swap_model(model_key: str) -> bool:
    """Change current.gguf symlink and restart llama container if needed."""
    model_file = MODELS[model_key]["file"]
    target = MODEL_DIR / model_file
    if not target.exists():
        print(f"  ERROR: model file not found: {target}")
        return False

    # Check if target model already loaded (avoid unnecessary reload)
    symlink_matches = False
    try:
        symlink_matches = (SYMLINK.resolve() == target)
    except Exception:
        pass

    if symlink_matches:
        # Symlink already correct — just wait for health
        try:
            conn = hc.HTTPConnection("127.0.0.1", 4000, timeout=5)
            conn.request("GET", "/health",
                headers={"Authorization": f"Bearer {LITELLM_KEY}"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()
            if resp.status == 200 and body.get("healthy_count", 0) > 0:
                if _verify_model_serving(model_key):
                    print(f"  Model {model_key} already loaded and healthy")
                    return True
        except Exception:
            pass
        print(f"  Model {model_key} already linked, waiting for health...")
    else:
        SYMLINK.unlink(missing_ok=True)
        SYMLINK.symlink_to(model_file)  # relative symlink for container resolution
        print(f"  Swapped to {model_key} ({model_file})")

        # Restart llama container
        subprocess.run(
            ["systemctl", "--user", "restart", "--no-block", "container-devforge-llm"],
            capture_output=True, text=True, timeout=15,
        )
        print(f"  Restarting llama for {model_key}...")
        # Give the old container time to fully stop (SIGTERM → 10s → SIGKILL + buffer)
        time.sleep(25)

    # Poll for health + verify model actually responds
    for attempt in range(120):
        time.sleep(3)
        try:
            conn = hc.HTTPConnection("127.0.0.1", 4000, timeout=5)
            conn.request("GET", "/health",
                headers={"Authorization": f"Bearer {LITELLM_KEY}"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()
            if resp.status == 200 and body.get("healthy_count", 0) > 0:
                if _verify_model_serving(model_key):
                    elapsed = 25 + (attempt + 1) * 3 if not symlink_matches else (attempt + 1) * 3
                    print(f"  llama healthy ({model_key}) after {elapsed}s")
                    return True
        except Exception:
            pass
        if (attempt + 1) % 20 == 0:
            elapsed = 25 + (attempt + 1) * 3 if not symlink_matches else (attempt + 1) * 3
            print(f"    still waiting... ({elapsed}s elapsed)")
    print(f"  ERROR: llama failed to become healthy for {model_key}")
    return False


def _verify_model_serving(model_key: str) -> bool:
    """Send a trivial prompt to confirm the model is actually serving, not just healthy."""
    model_name = MODELS[model_key]["name"]
    payload = json.dumps({"model": model_name, "messages": [{"role": "user", "content": "OK"}], "max_tokens": 5})
    try:
        conn = hc.HTTPConnection("127.0.0.1", 4000, timeout=60)
        conn.request("POST", "/v1/chat/completions", payload,
                     {"Authorization": f"Bearer {LITELLM_KEY}", "Content-Type": "application/json"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status == 200
    except Exception:
        return False


def _call_litellm(model_name: str, system_prompt: str, user_prompt: str, max_tokens: int = 512) -> Optional[Dict]:
    """Call a model via LiteLLM proxy using raw HTTP (avoids requests connection pooling issues)."""
    import re as _re
    payload = json.dumps({
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    })
    for attempt in range(3):
        try:
            conn = hc.HTTPConnection("127.0.0.1", 4000, timeout=300)
            conn.request("POST", "/v1/chat/completions", payload,
                         {"Authorization": f"Bearer {LITELLM_KEY}", "Content-Type": "application/json"})
            resp = conn.getresponse()
            body = resp.read().decode()
            conn.close()
            if resp.status == 200:
                content = json.loads(body)["choices"][0]["message"]["content"]
                match = _re.search(r'\{.*\}', content, _re.DOTALL)
                if match:
                    return json.loads(match.group(0))
                return None
            else:
                print(f"    LLM error: {resp.status} (attempt {attempt+1}/3)")
                time.sleep(2 ** attempt)
        except Exception as e:
            print(f"    LLM exception: {e} (attempt {attempt+1}/3)")
            time.sleep(2 ** attempt)
    return None


def fetch_sample_turns(limit: int = 10) -> list:
    """Get recent turns with substantive content from each agent."""
    rows = _psql(
        f"SELECT t.agent, "
        f"  replace(replace(COALESCE(t.user_turn, ''), E'\n', ' '), '|', '/'), "
        f"  replace(replace(COALESCE(t.text, ''), E'\n', ' '), '|', '/') "
        f"FROM turns t "
        f"WHERE t.user_turn NOT LIKE '%<task-notification>%' "
        f"  AND t.user_turn NOT LIKE '%<bash-stdout>%' "
        f"  AND length(t.user_turn) > 30 "
        f"ORDER BY t.created_at DESC "
        f"LIMIT {limit}"
    )
    turns = []
    for line in rows.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) >= 3:
            turns.append({
                "agent": parts[0],
                "user_turn": parts[1][:300],
                "text": parts[2][:300],
            })
    return turns


def build_extract_prompt(turn: dict) -> str:
    return f"[{turn['agent']}] user: {turn['user_turn']}\n[{turn['agent']}] text: {turn['text']}"


def build_verify_prompt(turn: dict, facts: list) -> str:
    lines = [f"## Original Turn\n[{turn['agent']}] user: {turn['user_turn']}\n[{turn['agent']}] text: {turn['text']}"]
    lines.append("\n## Extracted Facts")
    for i, f in enumerate(facts):
        lines.append(f"{i}. [{f.get('fact_type','')}] {f.get('evidence','')} (speaker: {f.get('speaker','')})")
    return "\n".join(lines)


def run_test(turns: List[Dict], combos: Optional[List[str]] = None) -> Dict:
    """Run extraction + verification for each combo and score results."""
    results = {}

    combo_keys = list(combos) if combos else list(COMBOS.keys())

    for combo_key in combo_keys:
        if combo_key not in COMBOS:
            continue
        model1_key, model2_key = COMBOS[combo_key]
        model1 = MODELS[model1_key]
        model2 = MODELS[model2_key]

        print(f"\n{'='*60}")
        print(f"Combo {combo_key}: {model1['family']}(1차) → {model2['family']}(2차)")
        print(f"  {model1['name']} → {model2['name']}")
        print(f"{'='*60}")

        # Phase 1: Extract with model1
        if not swap_model(model1_key):
            print(f"  SKIP: cannot load {model1_key}")
            continue
        time.sleep(5)  # KV cache warmup

        combo_facts = []
        print(f"  Phase 1 — {model1_key} extracting facts from {len(turns)} turns...")
        for i, turn in enumerate(turns):
            prompt = build_extract_prompt(turn)
            result = _call_litellm(model1["name"], EXTRACT_SYSTEM, prompt, max_tokens=512)
            facts = result.get("facts", []) if result else []
            combo_facts.append({
                "turn_index": i,
                "turn_agent": turn["agent"],
                "facts": facts,
            })
            if result:
                print(f"    Turn {i+1}/{len(turns)}: {len(facts)} facts")
            else:
                print(f"    Turn {i+1}/{len(turns)}: FAILED")

        # Phase 2: Verify with model2
        if not swap_model(model2_key):
            print(f"  SKIP: cannot load {model2_key}")
            continue
        time.sleep(5)

        combo_reviews = []
        print(f"  Phase 2 — {model2_key} verifying...")
        total_valid = 0
        total_hallucinated = 0
        for entry in combo_facts:
            if not entry["facts"]:
                continue
            prompt = build_verify_prompt(turns[entry["turn_index"]], entry["facts"])
            result = _call_litellm(model2["name"], VERIFY_SYSTEM, prompt, max_tokens=512)
            reviews = result.get("reviews", []) if result else []
            entry_review = {
                "turn_index": entry["turn_index"],
                "reviews": reviews,
                "fact_count": len(entry["facts"]),
                "valid": sum(1 for r in reviews if r.get("verdict") == "valid"),
                "hallucinated": sum(1 for r in reviews if r.get("verdict") == "hallucinated"),
            }
            combo_reviews.append(entry_review)
            total_valid += entry_review["valid"]
            total_hallucinated += entry_review["hallucinated"]
            print(f"    Turn {entry['turn_index']+1}: {entry_review['fact_count']} facts → "
                  f"{entry_review['valid']} valid, {entry_review['hallucinated']} hallucinated")

        total_facts = sum(e["fact_count"] for e in combo_reviews)
        score = {
            "combo": combo_key,
            "model1": f"{model1['name']} ({model1['family']})",
            "model2": f"{model2['name']} ({model2['family']})",
            "total_facts": total_facts,
            "valid": total_valid,
            "hallucinated": total_hallucinated,
            "valid_rate": round(total_valid / max(total_facts, 1) * 100, 1),
            "extraction_rate": round(sum(1 for e in combo_facts if e["facts"]) / max(len(turns), 1) * 100, 1),
        }
        results[combo_key] = score

    return results


def print_summary(results: dict):
    print(f"\n{'='*70}")
    print("FINAL COMPARISON")
    print(f"{'='*70}")
    print(f"{'Combo':<6} {'1차 (extract)':<30} {'2차 (verify)':<30} {'Facts':<6} {'Valid%':<8} {'Halluc%':<8} {'Extract%':<8}")
    print("-" * 70)
    for combo_key in ["A", "B", "C", "D", "E", "F"]:
        if combo_key not in results:
            continue
        s = results[combo_key]
        halluc_rate = round(s["hallucinated"] / max(s["total_facts"], 1) * 100, 1)
        print(f"{s['combo']:<6} {s['model1']:<30} {s['model2']:<30} {s['total_facts']:<6} {s['valid_rate']:<8} {halluc_rate:<8} {s['extraction_rate']:<8}")
    print("=" * 70)

    # Best combo
    if results:
        best = max(results.values(), key=lambda s: s["valid_rate"])
        print(f"\nBEST: Combo {best['combo']} — {best['model1']} → {best['model2']}")
        print(f"  Valid rate: {best['valid_rate']}%, Hallucination: {best['hallucinated']}/{best['total_facts']}")


def main():
    ap = argparse.ArgumentParser(description="Test model combinations for review pipeline")
    ap.add_argument("--limit", type=int, default=10, help="Number of sample turns (default: 10)")
    ap.add_argument("--combo", choices=list(COMBOS.keys()), help="Single combo to test")
    args = ap.parse_args()

    ts = datetime.now(timezone.utc)
    print(f"[{ts.isoformat()}] test_review_models starting (limit={args.limit})")

    turns = fetch_sample_turns(args.limit)
    if not turns:
        print("No turns found in DB")
        return 1
    print(f"Fetched {len(turns)} sample turns")

    combos = [args.combo] if args.combo else None
    results = run_test(turns, combos)

    # Save results
    out_file = Path("/opt/projects/server/scripts/test_review_results.json")
    out_file.write_text(json.dumps({
        "timestamp": ts.isoformat(),
        "turns_used": len(turns),
        "results": {k: v for k, v in results.items()},
    }, indent=2, ensure_ascii=False))
    print(f"\nResults saved to {out_file}")

    print_summary(results)

    # Restore Qwen14B for normal operations (default extract model)
    SYMLINK.unlink(missing_ok=True)
    SYMLINK.symlink_to("Qwen2.5-Coder-14B-Instruct-Q8_0.gguf")  # relative for container
    print(f"\nRestored current.gguf → qwen2.5-coder-14b")
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", "--no-block", "container-devforge-llm"],
            capture_output=True, text=True, timeout=30,
        )
        print("  Restart triggered (async)")
    except subprocess.TimeoutExpired:
        print("  WARNING: Restart timed out (may still be running async)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
