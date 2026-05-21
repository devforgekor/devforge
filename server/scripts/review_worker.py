#!/usr/bin/env python3
"""review_worker.py — 2-phase LLM review pipeline for DevForge.

Phase 1 (extract): Qwen3-4B (Podman A, port 8080) extracts facts from turns.
Phase 2 (verify):  Phi-4-mini (Podman B, port 8081) cross-validates facts.

24h rolling window, checkpoint-based incremental processing.
Both containers are always running — no model swapping needed.

Usage:
  python3 review_worker.py                 # normal incremental run
  python3 review_worker.py --reset         # re-review all turns
"""

import http.client as hc
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from lib.db import psql, psql_ok, esc_sql
from session_guard import log_commits_to_worklog

# ── endpoints ──────────────────────────────────────────────────────
EXTRACT_URL = "http://127.0.0.1:8080/v1/chat/completions"  # Podman A — Qwen3-4B, always on
VERIFY_URL = "http://127.0.0.1:8081/v1/chat/completions"   # Podman B — mode-switchable
CHECKPOINT_FILE = Path("/opt/projects/server/review_checkpoint.json")

# ── models ─────────────────────────────────────────────────────────
MODELS = {
    "qwen4b": {
        "name": "qwen3-4b",
        "port": 8080,
    },
    "phi4mini": {
        "name": "phi-4-mini",
        "port": 8081,
    },
    "phi4": {
        "name": "phi-4",
        "port": 8081,
    },
    "llama3b": {
        "name": "llama-3.2-3b",
        "port": 8081,
    },
}

# Pipeline: extract with Podman A (8080), verify with Podman B (8081)
EXTRACT_MODEL = "qwen4b"
VERIFY_MODEL = "phi4mini"

# ── prompts ────────────────────────────────────────────────────────
EXTRACT_SYSTEM = """You are a fact extraction system. From the conversation turn below, extract ONLY self-contained, testable facts.

CRITICAL — evidence MUST be an exact copy-paste substring from the turn. No paraphrasing, no summarizing, no completing partial sentences. If you cannot find the exact text, do NOT extract.

Rules:
- Evidence = verbatim substring found in the turn text (copy-paste exactly, same language)
- Skip: "yes", "ok", "apply it", "진행해", single words, sentence fragments, questions
- Skip: anything that requires prior conversation context to understand
- KEEP: decisions, code choices, architecture changes, data values, observed results
- NEVER fabricate, infer, or complete — if the turn doesn't explicitly state it, skip it
- NEVER extract credentials, API keys, passwords, tokens, or connection strings

fact_type choices:
- "decision": a choice was made (e.g. "we will use X for Y")
- "data_given": concrete data, numbers, values, paths, error messages
- "observation": something observed, measured, or tested (e.g. "X returned Y at Z time")

Return JSON:
{"facts": [{"evidence": "exact copy-paste from turn", "speaker": "agent name", "fact_type": "decision|data_given|observation"}]}

If no self-contained, complete facts with exact evidence, return {"facts": []}."""

VERIFY_SYSTEM = """You are a critical fact checker. Review the extracted facts against the original turn content.

For each fact, check:
1. Is the evidence VERBATIM from the turn? Mark "hallucinated" if not.
2. Is the fact_type correct? Mark "mismatch" if type is wrong.
3. Is the fact truly self-contained? Mark "context_dependent" if it requires prior knowledge.

Return JSON:
{"reviews": [{"fact_index": 0, "verdict": "valid|hallucinated|mismatch|context_dependent", "reason": "short explanation"}]}"""


def get_extract_system(model_key: str) -> str:
    """Return model-specific extract prompt or global default."""
    return MODELS.get(model_key, {}).get("extract_system", EXTRACT_SYSTEM)


def get_verify_system(model_key: str) -> str:
    """Return model-specific verify prompt or global default."""
    return MODELS.get(model_key, {}).get("verify_system", VERIFY_SYSTEM)


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
        UNIQUE (turn_id, fact_index, extract_model)
    )""")
    for col, col_type in [("prompt_tokens", "INTEGER"), ("gen_tokens", "INTEGER"),
                           ("gen_rate", "REAL"), ("elapsed_ms", "REAL"),
                           ("cache_hit", "INTEGER"), ("phase", "TEXT")]:
        psql(f"ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS {col} {col_type}")
    psql("CREATE INDEX IF NOT EXISTS idx_review_facts_turn ON review_facts(turn_id)")
    psql("CREATE INDEX IF NOT EXISTS idx_review_facts_created ON review_facts(created_at DESC)")


def fetch_unprocessed_turns(limit: int = 20, checkpoint: dict = None,
                            extract_model: str = "") -> List[Dict]:
    """Get recent turns that haven't been reviewed by current extract_model yet, within 24h window."""
    last_ts = (checkpoint or {}).get("last_ts", "")
    ts_filter = f"AND t.created_at > '{last_ts}'::timestamptz" if last_ts else ""
    model_filter = ""
    if extract_model:
        model_filter = f"AND r.extract_model = '{esc_sql(extract_model)}'"
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
      AND NOT EXISTS (
          SELECT 1 FROM review_facts r
          WHERE r.turn_id = t.id {model_filter}
      )
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


def save_review_facts(turn_id: str, facts: List[Dict], reviews: List[Dict],
                      extract_model: str, verify_model: str,
                      phase: str = "", timings: Optional[Dict] = None):
    """Save extracted and verified facts to DB with optional timing data."""
    t = timings or {}
    saved = 0
    for review in reviews:
        idx = review.get("fact_index", 0)
        fact = facts[idx] if idx < len(facts) else {}
        evidence = esc_sql(fact.get("evidence", "")[:500])
        fact_type = esc_sql(fact.get("fact_type", ""))
        verdict = esc_sql(review.get("verdict", "pending"))
        reason = esc_sql(review.get("reason", "")[:500])
        prompt_n = t.get("prompt_n", 0) or 0
        pred_n = t.get("predicted_n", 0) or 0
        gen_rate = round(t.get("predicted_per_second", 0) or 0, 1)
        elapsed = round((t.get("prompt_ms", 0) or 0) + (t.get("predicted_ms", 0) or 0), 0)
        cache_n = t.get("cache_n", 0) or 0

        ok = psql_ok(f"""
        INSERT INTO review_facts (turn_id, fact_index, evidence, fact_type, verdict, reason,
            extract_model, verify_model, phase, prompt_tokens, gen_tokens, gen_rate, elapsed_ms, cache_hit)
        VALUES ('{turn_id}', {idx}, '{evidence}', '{fact_type}', '{verdict}', '{reason}',
            '{extract_model}', '{verify_model}', '{phase}', {prompt_n}, {pred_n}, {gen_rate}, {elapsed}, {cache_n})
        ON CONFLICT (turn_id, fact_index, extract_model) DO UPDATE SET
            verdict = EXCLUDED.verdict,
            reason = EXCLUDED.reason,
            extract_model = EXCLUDED.extract_model,
            verify_model = EXCLUDED.verify_model,
            phase = EXCLUDED.phase,
            prompt_tokens = EXCLUDED.prompt_tokens,
            gen_tokens = EXCLUDED.gen_tokens,
            gen_rate = EXCLUDED.gen_rate,
            elapsed_ms = EXCLUDED.elapsed_ms,
            cache_hit = EXCLUDED.cache_hit
        """)
        if ok:
            saved += 1
    if saved < len(reviews):
        print(f"  WARNING: DB save {saved}/{len(reviews)} for turn {turn_id[:8]}...")


# ── health check ──────────────────────────────────────────────────
def check_endpoint(port: int, label: str) -> bool:
    """Quick health check — confirm LLM server on port is responding."""
    try:
        conn = hc.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status == 200
    except Exception:
        return False


# ── LLM calls ──────────────────────────────────────────────────────

# ── Rate Estimator ────────────────────────────────────────────────────
class RateEstimator:
    """EMA + median rate tracker, self-calibrating from timings field."""
    def __init__(self, label: str = "", initial_prompt: float = 15.0,
                 initial_gen: float = 5.0, alpha: float = 0.3):
        self.label = label
        self.prompt_rate = initial_prompt
        self.gen_rate = initial_gen
        self.alpha = alpha
        self._samples: list = []
        self.calls = 0
        self.cache_hits = 0
        self._total_prompt = 0
        self._total_gen = 0
        self._total_elapsed = 0.0

    def update(self, timings: dict):
        pr = timings.get("prompt_per_second", 0)
        gr = timings.get("predicted_per_second", 0)
        if pr > 0:
            self.prompt_rate = self._ema(self.prompt_rate, pr)
        if gr > 0:
            self.gen_rate = self._ema(self.gen_rate, gr)
            self._samples.append(gr)
            if len(self._samples) > 50:
                self._samples.pop(0)
        self.calls += 1
        self._total_prompt += timings.get("prompt_n", 0)
        self._total_gen += timings.get("predicted_n", 0)
        self._total_elapsed += timings.get("predicted_ms", 0) + timings.get("prompt_ms", 0)
        if timings.get("cache_n", 0) > 0:
            self.cache_hits += 1

    def _ema(self, old: float, new: float) -> float:
        return self.alpha * new + (1 - self.alpha) * old

    def calc_timeout(self, prompt_tokens: int, max_tokens: int, buffer: int = 30) -> int:
        return int(prompt_tokens / max(self.prompt_rate, 0.5)
                   + max_tokens / max(self.gen_rate, 0.5) + buffer)

    def stats(self) -> dict:
        result = {
            "prompt_rate": round(self.prompt_rate, 1),
            "gen_rate": round(self.gen_rate, 1),
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "total_prompt": self._total_prompt,
            "total_gen": self._total_gen,
            "total_elapsed_s": round(self._total_elapsed / 1000, 1),
        }
        if self._samples:
            result["median_gen_rate"] = round(statistics.median(self._samples), 1)
        return result


def call_llm(port: int, model_name: str, system_prompt: str, user_prompt: str,
             max_tokens: int = 512, timeout: int = 180, retries: int = 2
             ) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Call LLM endpoint via http.client. Returns (parsed_json, timings_dict) or (None, None)."""
    payload = json.dumps({
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt + "\n/no_think"},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }).encode()
    headers = {"Content-Type": "application/json"}

    @retry(stop=stop_after_attempt(retries + 1),
           wait=wait_exponential(multiplier=1, min=1, max=8),
           retry=retry_if_exception_type((TimeoutError, OSError, ConnectionError, Exception)),
           after=lambda rs: print(f"    LLM retry: {rs.outcome.exception()}")
           if rs.failed else None)
    def _do_call():
        conn = hc.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("POST", "/v1/chat/completions", payload, headers)
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        if resp.status != 200:
            raise ConnectionError(f"HTTP {resp.status}")
        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
        result = _parse_llm_json(content)
        return result, data.get("timings", {})

    try:
        return _do_call()
    except Exception:
        return (None, None)


def _parse_llm_json(content: str) -> Optional[Dict]:
    """Extract JSON from LLM output, handling common formatting issues."""
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{[^{}]*"(?:facts|reviews)"[^{}]*\[.*?\][^{}]*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
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
    log_commits_to_worklog()

    checkpoint = load_checkpoint()
    if args.reset:
        CHECKPOINT_FILE.unlink(missing_ok=True)
        checkpoint = {}
        print("  Reset: cleared checkpoint")

    extract_model_name = MODELS[EXTRACT_MODEL]["name"]
    turns = fetch_unprocessed_turns(args.limit, checkpoint, extract_model_name)
    if not turns:
        print("No unprocessed turns in 24h window")
        return 0
    print(f"Fetched {len(turns)} unprocessed turns")

    # ── init estimators ────────────────────────────────────────────
    est_extract = RateEstimator("extract")
    est_verify = RateEstimator("verify", initial_gen=3.0)

    # Phase 1: Extract with Podman A (Qwen3-4B on 8080)
    extract_model = MODELS[EXTRACT_MODEL]
    if not check_endpoint(extract_model["port"], EXTRACT_MODEL):
        print(f"FATAL: extract endpoint :{extract_model['port']} not responding")
        return 1

    combo_facts = []
    print(f"Phase 1 — extracting facts with {extract_model['name']}...")
    for i, turn in enumerate(turns):
        prompt = build_extract_prompt(turn)
        prompt_tokens = int((len(get_extract_system(EXTRACT_MODEL).split()) + len(prompt.split())) * 1.3)
        timeout = est_extract.calc_timeout(prompt_tokens, 512) if est_extract.calls > 0 else 180
        result, timings = call_llm(extract_model["port"], extract_model["name"],
                                   get_extract_system(EXTRACT_MODEL), prompt,
                                   max_tokens=512, timeout=timeout)
        facts = result.get("facts", []) if result else []
        if timings:
            est_extract.update(timings)
            rate_now = timings.get("predicted_per_second", 0)
            cache = timings.get("cache_n", 0)
        else:
            rate_now, cache = 0, 0
        combo_facts.append({
            "turn_index": i, "turn_id": turn["id"], "turn_agent": turn["agent"],
            "turn_data": turn, "facts": facts,
            "extract_timings": timings,
        })
        status = f"{len(facts)} facts @ {rate_now:.1f}t/s" if result else "FAILED"
        if cache:
            status += f" [cache:{cache}]"
        print(f"  Turn {i+1}/{len(turns)}: {status}")

    # Phase 2: Verify with Podman B (Phi-4-mini on 8081)
    facts_extracted = [e for e in combo_facts if e["facts"]]
    if not facts_extracted:
        max_ts = max((t.get("created_at", "") for t in turns), default="")
        if max_ts:
            save_checkpoint({"last_ts": max_ts})
        _print_stats(est_extract, est_verify)
        print(f"Phase 2 — skipped (0 facts from {len(turns)} turns)")
        return 0

    verify_model = MODELS[VERIFY_MODEL]
    if not check_endpoint(verify_model["port"], VERIFY_MODEL):
        print(f"FATAL: verify endpoint :{verify_model['port']} not responding")
        return 1

    total_valid = 0
    total_hallucinated = 0
    total_facts = 0
    print(f"Phase 2 — verifying with {verify_model['name']}...")
    for entry in combo_facts:
        if not entry["facts"]:
            continue
        prompt = build_verify_prompt(entry["turn_data"], entry["facts"])
        prompt_tokens = int((len(get_verify_system(VERIFY_MODEL).split()) + len(prompt.split())) * 1.3)
        timeout = est_verify.calc_timeout(prompt_tokens, 512) if est_verify.calls > 0 else 120
        result, timings = call_llm(verify_model["port"], verify_model["name"],
                                   get_verify_system(VERIFY_MODEL), prompt,
                                   max_tokens=512, timeout=timeout)
        reviews = result.get("reviews", []) if result else []
        if timings:
            est_verify.update(timings)
            rate_now = timings.get("predicted_per_second", 0)
        else:
            rate_now = 0

        valid = sum(1 for r in reviews if r.get("verdict") == "valid")
        hallucinated = sum(1 for r in reviews if r.get("verdict") == "hallucinated")
        total_valid += valid
        total_hallucinated += hallucinated
        total_facts += len(entry["facts"])

        save_review_facts(
            entry["turn_id"], entry["facts"], reviews,
            extract_model["name"], verify_model["name"], phase="verify",
            timings=timings
        )
        print(f"  Turn {entry['turn_index']+1}: {len(entry['facts'])} facts → "
              f"{valid} valid, {hallucinated} hallucinated @ {rate_now:.1f}t/s")

    valid_rate = round(total_valid / max(total_facts, 1) * 100, 1)
    print(f"\nDone: {total_facts} facts, {total_valid} valid ({valid_rate}%), "
          f"{total_hallucinated} hallucinated")

    _print_stats(est_extract, est_verify)

    max_ts = max((t.get("created_at", "") for t in turns), default="")
    if max_ts:
        save_checkpoint({"last_ts": max_ts})

    return 0


def _print_stats(est_extract: RateEstimator, est_verify: RateEstimator):
    """Print per-model performance stats from timings data."""
    print("\n── performance stats ──")
    for est in (est_extract, est_verify):
        if est.calls == 0:
            continue
        s = est.stats()
        cache_pct = round(est.cache_hits / max(est.calls, 1) * 100, 1)
        print(f"  {est.label}:")
        print(f"    rate: prompt_eval={s['prompt_rate']:.1f} t/s, gen={s['gen_rate']:.1f} t/s"
              f" (median={s['median_gen_rate']:.1f})")
        print(f"    tokens: prompt={s['total_prompt']}, gen={s['total_gen']},"
              f" elapsed={s['total_elapsed_s']:.0f}s")
        print(f"    calls: {est.calls}, cache_hits: {est.cache_hits} ({cache_pct}%)")


if __name__ == "__main__":
    sys.exit(main())
