#!/usr/bin/env python3
"""review_worker.py — 2-phase LLM review pipeline for DevForge.

Phase 1 (extract): Qwen2.5-Coder-14B extracts self-contained facts from turns.
Phase 2 (verify):  Phi-4 cross-validates facts against original turn content.

24h rolling window, checkpoint-based incremental processing.
Models swap via /opt/ai_data/models/gguf/current.gguf symlink.

Usage:
  python3 review_worker.py                 # normal incremental run
  python3 review_worker.py --reset         # re-review all turns
"""

import http.client as hc
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List

from lib.db import psql, psql_ok, esc_sql
from session_guard import log_commits_to_worklog

# ── paths ──────────────────────────────────────────────────────────
SYMLINK = Path("/opt/ai_data/models/gguf/current.gguf")
MODEL_DIR = Path("/opt/ai_data/models/gguf")
LITELLM_URL = "http://localhost:4000/v1/chat/completions"
LITELLM_KEY = "devforge-litellm-key"
CHECKPOINT_FILE = Path("/opt/projects/server/review_checkpoint.json")

# ── models ─────────────────────────────────────────────────────────
MODELS = {
    "qwen14": {
        "name": "qwen2.5-coder-14b",
        "file": "Qwen2.5-Coder-14B-Instruct-Q8_0.gguf",
    },
    "phi4": {
        "name": "phi-4",
        "file": "phi-4-Q8_0.gguf",
    },
    "deepseek_r1": {
        "name": "deepseek-r1-distill-qwen-14b",
        "file": "DeepSeek-R1-Distill-Qwen-14B-Q8_0.gguf",
    },
    "yi_coder_9b": {
        "name": "yi-coder-9b-chat",
        "file": "Yi-Coder-9B-Chat-Q8_0.gguf",
    },
    "mistral_nemo_12b": {
        "name": "mistral-nemo-12b-instruct",
        "file": "Mistral-Nemo-Instruct-2407-Q8_0.gguf",
    },
}

# Pipeline: extract with model1, verify with model2
EXTRACT_MODEL = "qwen14"
VERIFY_MODEL = "phi4"

# ── prompts ────────────────────────────────────────────────────────
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

# ── checkpoint ─────────────────────────────────────────────────────
def load_checkpoint() -> Dict[str, str]:
    if CHECKPOINT_FILE.exists():
        return json.loads(CHECKPOINT_FILE.read_text())
    return {}


def save_checkpoint(cp: Dict[str, str]):
    CHECKPOINT_FILE.write_text(json.dumps(cp, indent=2, ensure_ascii=False))


# ── DB helpers ─────────────────────────────────────────────────────
def ensure_review_table():
    psql("""
    CREATE TABLE IF NOT EXISTS review_facts (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        turn_id UUID NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
        fact_index INTEGER NOT NULL,
        evidence TEXT,
        fact_type TEXT,
        verdict TEXT NOT NULL DEFAULT 'pending',
        reason TEXT,
        extract_model TEXT,
        verify_model TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (turn_id, fact_index)
    )""")
    psql("CREATE INDEX IF NOT EXISTS idx_review_facts_turn ON review_facts(turn_id)")
    psql("CREATE INDEX IF NOT EXISTS idx_review_facts_created ON review_facts(created_at DESC)")


def fetch_unprocessed_turns(limit: int = 20, checkpoint: dict = None) -> List[Dict]:
    """Get recent turns that haven't been reviewed yet, within 24h window."""
    last_ts = (checkpoint or {}).get("last_ts", "")
    ts_filter = f"AND t.created_at > '{last_ts}'::timestamptz" if last_ts else ""
    rows = psql(f"""
    SELECT t.id, t.agent,
           regexp_replace(t.user_turn, E'[\\n\\r\\\\|]+', ' ', 'g'),
           regexp_replace(t.text, E'[\\n\\r\\\\|]+', ' ', 'g'),
           t.created_at
    FROM turns t
    WHERE t.created_at > now() - interval '24 hours'
      {ts_filter}
      AND t.user_turn NOT LIKE '%<task-notification>%'
      AND t.user_turn NOT LIKE '%<bash-stdout>%'
      AND length(t.user_turn) > 30
      AND NOT EXISTS (SELECT 1 FROM review_facts r WHERE r.turn_id = t.id)
    ORDER BY t.created_at DESC
    LIMIT {limit}
    """)
    turns = []
    for line in rows.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) >= 5 and parts[0].strip() != "":
            turns.append({
                "id": parts[0],
                "agent": parts[1],
                "user_turn": parts[2][:300],
                "text": parts[3][:300],
                "created_at": parts[4],
            })
    return turns


def esc_sql(s: str) -> str:
    return s.replace("'", "''").replace("\\", "\\\\").replace("\n", " ").replace("\r", " ")


def save_review_facts(turn_id: str, facts: List[Dict], reviews: List[Dict],
                      extract_model: str, verify_model: str):
    """Save extracted and verified facts to DB."""
    saved = 0
    for review in reviews:
        idx = review.get("fact_index", 0)
        fact = facts[idx] if idx < len(facts) else {}
        evidence = esc_sql(fact.get("evidence", "")[:500])
        fact_type = esc_sql(fact.get("fact_type", ""))
        verdict = esc_sql(review.get("verdict", "pending"))
        reason = esc_sql(review.get("reason", "")[:500])

        ok = psql_ok(f"""
        INSERT INTO review_facts (turn_id, fact_index, evidence, fact_type, verdict, reason, extract_model, verify_model)
        VALUES ('{turn_id}', {idx}, '{evidence}', '{fact_type}', '{verdict}', '{reason}', '{extract_model}', '{verify_model}')
        ON CONFLICT (turn_id, fact_index) DO UPDATE SET
            verdict = EXCLUDED.verdict,
            reason = EXCLUDED.reason,
            extract_model = EXCLUDED.extract_model,
            verify_model = EXCLUDED.verify_model
        """)
        if ok:
            saved += 1
    if saved < len(reviews):
        print(f"  WARNING: DB save {saved}/{len(reviews)} for turn {turn_id[:8]}...")


# ── model swap ─────────────────────────────────────────────────────
LLAMA_RUN_ARGS = [
    "run", "-d", "--pod", "ai-pod", "--name", "devforge-llm", "--rm", "--replace", "--no-healthcheck",
    "-v", "/opt/ai_data/models/gguf:/models:Z,ro",
    "-v", "/opt/ai_data/cache:/cache:Z",
    "--env-file", "/home/opc/.config/devforge/secrets.env",
    "localhost/devforge-llama:2026.05.13",
    "-m", "/models/current.gguf", "--host", "0.0.0.0", "--port", "8080",
    "--threads", "4", "--threads-batch", "4", "--ctx-size", "8192",
    "--flash-attn", "on", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
    "--cache-reuse", "256", "--slot-save-path", "/cache/",
    "--cache-ram", "8192", "--batch-size", "4096", "--ubatch-size", "1024",
]


def _podman_restart_llama() -> bool:
    """Restart llama container using podman directly (bypasses systemd)."""
    r = subprocess.run(["podman", "stop", "devforge-llm"],
                       capture_output=True, text=True, timeout=30)
    time.sleep(5)
    r = subprocess.run(["podman"] + LLAMA_RUN_ARGS,
                       capture_output=True, text=True, timeout=30)
    return r.returncode == 0


def swap_model(model_key: str) -> bool:
    """Change current.gguf symlink and restart llama container if needed."""
    model_file = MODELS[model_key]["file"]
    target = MODEL_DIR / model_file
    if not target.exists():
        print(f"  ERROR: model file not found: {target}")
        return False

    symlink_matches = False
    try:
        symlink_matches = (SYMLINK.resolve() == target)
    except Exception:
        pass

    if symlink_matches:
        if _verify_serving(model_key):
            print(f"  Model {model_key} already loaded and healthy")
            return True
        print(f"  Model {model_key} already linked but dead — restarting...")
        if not _podman_restart_llama():
            print(f"  ERROR: failed to restart llama container for {model_key}")
            return False
        print(f"  Restarting llama for {model_key}...")
        time.sleep(25)
    else:
        SYMLINK.unlink(missing_ok=True)
        SYMLINK.symlink_to(model_file)
        print(f"  Swapped to {model_key} ({model_file})")

        if not _podman_restart_llama():
            print(f"  ERROR: failed to restart llama container for {model_key}")
            return False
        print(f"  Restarting llama for {model_key}...")
        time.sleep(25)

    for attempt in range(120):
        time.sleep(3)
        if _verify_serving(model_key):
            elapsed = 25 + (attempt + 1) * 3
            print(f"  llama healthy ({model_key}) after {elapsed}s")
            return True
        if (attempt + 1) % 20 == 0:
            elapsed = 25 + (attempt + 1) * 3
            print(f"    still waiting... ({elapsed}s elapsed)")
    print(f"  ERROR: llama failed to become healthy for {model_key}")
    return False


def _verify_serving(model_key: str) -> bool:
    model_name = MODELS[model_key]["name"]
    payload = json.dumps({"model": model_name, "messages": [{"role": "user", "content": "OK"}], "max_tokens": 5})
    try:
        conn = hc.HTTPConnection("127.0.0.1", 4000, timeout=60)
        conn.request("POST", "/v1/chat/completions", payload,
                     {"Authorization": f"Bearer {LITELLM_KEY}", "Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status == 200
    except Exception:
        return False


# ── LLM calls ──────────────────────────────────────────────────────
def call_llm(model_name: str, system_prompt: str, user_prompt: str,
             max_tokens: int = 512, retries: int = 2) -> Optional[Dict]:
    payload = json.dumps({
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    })
    for attempt in range(retries + 1):
        try:
            conn = hc.HTTPConnection("127.0.0.1", 4000, timeout=600)
            conn.request("POST", "/v1/chat/completions", payload,
                         {"Authorization": f"Bearer {LITELLM_KEY}", "Content-Type": "application/json"})
            resp = conn.getresponse()
            body = resp.read().decode()
            conn.close()
            if resp.status == 200:
                content = json.loads(body)["choices"][0]["message"]["content"]
                match = re.search(r'\{.*\}', content, re.DOTALL)
                if match:
                    return json.loads(match.group(0))
                return None
            else:
                print(f"    LLM error: {resp.status} (attempt {attempt+1}/{retries+1})")
                time.sleep(2 ** attempt)
        except Exception as e:
            print(f"    LLM exception: {e} (attempt {attempt+1}/{retries+1})")
            time.sleep(2 ** attempt)
    return None


# ── prompts ────────────────────────────────────────────────────────
def build_extract_prompt(turn: Dict) -> str:
    return f"[{turn['agent']}] user: {turn['user_turn']}\n[{turn['agent']}] text: {turn['text']}"


def build_verify_prompt(turn: Dict, facts: List[Dict]) -> str:
    lines = [
        f"## Original Turn\n[{turn['agent']}] user: {turn['user_turn']}\n[{turn['agent']}] text: {turn['text']}",
        "\n## Extracted Facts"
    ]
    for i, f in enumerate(facts):
        lines.append(f"{i}. [{f.get('fact_type', '')}] {f.get('evidence', '')} (speaker: {f.get('speaker', '')})")
    return "\n".join(lines)


# ── main ───────────────────────────────────────────────────────────
def main():
    import argparse
    ap = argparse.ArgumentParser(description="2-phase LLM review pipeline")
    ap.add_argument("--reset", action="store_true", help="Re-review all turns in 24h window")
    ap.add_argument("--limit", type=int, default=20, help="Max turns per run (default: 20)")
    args = ap.parse_args()

    ts = datetime.now(timezone.utc)
    print(f"[{ts.isoformat()}] review_worker starting")

    ensure_review_table()

    # Record any new git commits (v1.1: idempotent, ON CONFLICT DO NOTHING)
    log_commits_to_worklog()

    checkpoint = load_checkpoint()
    if args.reset:
        CHECKPOINT_FILE.unlink(missing_ok=True)
        checkpoint = {}
        print("  Reset: cleared checkpoint")

    # Fetch unprocessed turns (skip already-checkpointed turns)
    turns = fetch_unprocessed_turns(args.limit, checkpoint)
    if not turns:
        print("No unprocessed turns in 24h window")
        return 0
    print(f"Fetched {len(turns)} unprocessed turns")

    # Phase 1: Extract with Qwen14B
    extract_model = MODELS[EXTRACT_MODEL]
    if not swap_model(EXTRACT_MODEL):
        print("FATAL: cannot load extract model")
        return 1
    time.sleep(3)

    combo_facts = []
    print(f"Phase 1 — extracting facts with {extract_model['name']}...")
    for i, turn in enumerate(turns):
        prompt = build_extract_prompt(turn)
        result = call_llm(extract_model["name"], EXTRACT_SYSTEM, prompt, max_tokens=512)
        facts = result.get("facts", []) if result else []
        combo_facts.append({
            "turn_index": i,
            "turn_id": turn["id"],
            "turn_agent": turn["agent"],
            "turn_data": turn,
            "facts": facts,
        })
        status = f"{len(facts)} facts" if result else "FAILED"
        print(f"  Turn {i+1}/{len(turns)}: {status}")

    # Phase 2: Verify with Phi-4 (skip if no facts extracted)
    facts_extracted = [e for e in combo_facts if e["facts"]]
    if not facts_extracted:
        max_ts = max((t.get("created_at", "") for t in turns), default="")
        if max_ts:
            save_checkpoint({"last_ts": max_ts})
        print(f"Phase 2 — skipped (0 facts from {len(turns)} turns)")
        return 0

    verify_model = MODELS[VERIFY_MODEL]
    if not swap_model(VERIFY_MODEL):
        print("FATAL: cannot load verify model")
        return 1
    time.sleep(3)

    total_valid = 0
    total_hallucinated = 0
    total_facts = 0
    print(f"Phase 2 — verifying with {verify_model['name']}...")
    for entry in combo_facts:
        if not entry["facts"]:
            continue
        prompt = build_verify_prompt(entry["turn_data"], entry["facts"])
        result = call_llm(verify_model["name"], VERIFY_SYSTEM, prompt, max_tokens=512)
        reviews = result.get("reviews", []) if result else []

        valid = sum(1 for r in reviews if r.get("verdict") == "valid")
        hallucinated = sum(1 for r in reviews if r.get("verdict") == "hallucinated")
        total_valid += valid
        total_hallucinated += hallucinated
        total_facts += len(entry["facts"])

        save_review_facts(
            entry["turn_id"], entry["facts"], reviews,
            extract_model["name"], verify_model["name"]
        )
        print(f"  Turn {entry['turn_index']+1}: {len(entry['facts'])} facts → "
              f"{valid} valid, {hallucinated} hallucinated")

    valid_rate = round(total_valid / max(total_facts, 1) * 100, 1)
    print(f"\nDone: {total_facts} facts, {total_valid} valid ({valid_rate}%), "
          f"{total_hallucinated} hallucinated")

    # Leave Phi-4 loaded (already serving from Phase 2)
    max_ts = max((t.get("created_at", "") for t in turns), default="")
    if max_ts:
        save_checkpoint({"last_ts": max_ts})
    print("Done — Phi-4 left loaded, no restore needed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
