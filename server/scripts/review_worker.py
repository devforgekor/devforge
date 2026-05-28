#!/usr/bin/env python3
"""review_worker.py — extract → verify → debate review → store.

Phase 1 (extract):   DeepSeek-V2-Lite (Pod A :8080) extracts facts from turns.
Phase 2 (verify):    Python deterministic check — evidence substring in turn text.
Phase 3 (debate):    Qwen-14B (Pod B :8081) reviews verified facts via dart_reviewer.
Phase 4 (store):     review_facts DB + activity_log (queue_status='reviewed').

24h rolling window, checkpoint-based incremental processing.
DeepSeek runs resident on :8080. Qwen-14B runs on :8081 (Pod B in review mode).

Usage:
  python3 review_worker.py                 # normal incremental run
  python3 review_worker.py --reset         # re-review all turns
"""

import http.client as hc
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from lib.db import psql, psql_ok, esc_sql
from lib.llm.rate_estimator import TimingsBasedRateEstimator

CHECKPOINT_FILE = Path("/opt/projects/server/review_checkpoint.json")

MODELS = {
    "deepseek": {
        "name": "deepseek-v2-lite",
        "port": 8080,
    },
    "qwen-14b": {
        "name": "qwen-14b",
        "port": 8081,
    },
}

# Single-model extraction + deterministic Python verification.
# DeepSeek-V2-Lite (:8080) extracts facts from turns.
# Python verify_evidence() confirms evidence is verbatim substring — no LLM, no hallucination.
EXTRACT_MODEL = "deepseek"

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

FACT_REVIEWER_SYSTEM = """You are a FACT REVIEWER in a 2-party review. Evaluate facts extracted from AI coding session transcripts.

Your role — combined Refuter AND Judge:
1. REFUTE: Flag facts that are trivial ("ok", status updates), ambiguous without context, or likely hallucinations that slipped past verification.
2. JUDGE: Score overall fact quality (0-100). 100 = all valuable and well-evidenced. <70 = explain gaps.

Evidence was Python-verified as verbatim substring of the original turn — trust it unless you find contradictions with other facts in the batch.

Output STRICT JSON (no markdown, no explanation):
{"verdict": "approved|needs_revision|rejected", "consensus_score": 0-100, "issues": ["..."], "summary": "1-sentence assessment"}"""


def verify_evidence(evidence: str, turn_text: str) -> bool:
    """Deterministic check: evidence must be a verbatim substring of turn text.

    No LLM involvement — zero hallucination path. If evidence is not literally
    present in the turn, it is hallucinated regardless of semantic correctness.
    """
    if not evidence or not turn_text:
        return False
    return evidence in turn_text


def load_checkpoint() -> Dict[str, str]:
    if CHECKPOINT_FILE.exists():
        return json.loads(CHECKPOINT_FILE.read_text())
    return {}


def save_checkpoint(cp: Dict[str, str]):
    CHECKPOINT_FILE.write_text(json.dumps(cp, indent=2, ensure_ascii=False))


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
                           ("cache_hit", "INTEGER"), ("phase", "TEXT"),
                           ("arbitrator_model", "TEXT"), ("source", "TEXT"),
                           ("corrected_evidence", "TEXT")]:
        psql(f"ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS {col} {col_type}")
    psql("CREATE INDEX IF NOT EXISTS idx_review_facts_turn ON review_facts(turn_id)")
    psql("CREATE INDEX IF NOT EXISTS idx_review_facts_created ON review_facts(created_at DESC)")


def fetch_unprocessed_turns(limit: int = 20, checkpoint: dict = None) -> List[Dict]:
    """Get recent turns not yet reviewed by DeepSeek extractor, within 24h window."""
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
      AND NOT EXISTS (
          SELECT 1 FROM review_facts r
          WHERE r.turn_id = t.id
            AND r.extract_model = 'deepseek-v2-lite'
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


def save_review_facts(turn_id: str, facts: List[Dict], decisions: List[Dict],
                      extract_model: str = "deepseek-v2-lite",
                      phase: str = "", timings: Optional[Dict] = None):
    t = timings or {}
    saved = 0
    for dec in decisions:
        idx = dec.get("fact_index", 0)
        fact = facts[idx] if idx < len(facts) else {}
        evidence = esc_sql(fact.get("evidence", "")[:500])
        fact_type = esc_sql(fact.get("fact_type", ""))
        verdict = esc_sql(dec.get("verdict", "pending"))
        reason = esc_sql(dec.get("reason", "")[:500])
        corrected = esc_sql(dec.get("corrected_evidence", "")[:500] or "")
        source = esc_sql(dec.get("source", "deepseek"))
        prompt_n = t.get("prompt_n", 0) or 0
        pred_n = t.get("predicted_n", 0) or 0
        gen_rate = round(t.get("predicted_per_second", 0) or 0, 1)
        elapsed = round((t.get("prompt_ms", 0) or 0) + (t.get("predicted_ms", 0) or 0), 0)
        cache_n = t.get("cache_n", 0) or 0

        ok = psql_ok(f"""
        INSERT INTO review_facts (turn_id, fact_index, evidence, fact_type, verdict, reason,
            extract_model, verify_model, source, corrected_evidence,
            phase, prompt_tokens, gen_tokens, gen_rate, elapsed_ms, cache_hit)
        VALUES ('{turn_id}', {idx}, '{evidence}', '{fact_type}', '{verdict}', '{reason}',
            '{extract_model}', 'python', '{source}',
            '{corrected}', '{phase}', {prompt_n}, {pred_n}, {gen_rate}, {elapsed}, {cache_n})
        ON CONFLICT (turn_id, fact_index, extract_model) DO UPDATE SET
            verdict = EXCLUDED.verdict,
            reason = EXCLUDED.reason,
            corrected_evidence = EXCLUDED.corrected_evidence,
            phase = EXCLUDED.phase,
            prompt_tokens = EXCLUDED.prompt_tokens,
            gen_tokens = EXCLUDED.gen_tokens,
            gen_rate = EXCLUDED.gen_rate,
            elapsed_ms = EXCLUDED.elapsed_ms,
            cache_hit = EXCLUDED.cache_hit
        """)
        if ok:
            saved += 1
    if saved < len(decisions):
        print(f"  WARNING: DB save {saved}/{len(decisions)} for turn {turn_id[:8]}...")


def _insert_activity_review(turn_id: str, facts: List[Dict], decisions: List[Dict],
                            extract_model: str) -> int:
    """Enqueue verified facts via shared queue_writer for downstream review."""
    from lib.queue_writer import enqueue_review
    count = 0
    for dec in decisions:
        idx = dec.get("fact_index", 0)
        if idx >= len(facts):
            continue
        fact = facts[idx]
        if not fact.get("evidence"):
            continue
        ok = enqueue_review(
            entry_type="review",
            source="review_worker",
            title=f"review: {fact.get('fact_type', 'general')} — {fact.get('evidence', '')[:80]}",
            summary=f"[{dec.get('verdict', 'pending')}] {dec.get('reason', '')[:200]}",
            body={
                "fact_index": idx,
                "fact_type": fact.get("fact_type", "general"),
                "evidence": fact.get("evidence", "")[:200],
                "verdict": dec.get("verdict", "pending"),
                "reason": dec.get("reason", "")[:200],
                "extract_model": extract_model,
                "review_verdict": dec.get("review_verdict", "pending"),
                "review_consensus": dec.get("review_consensus", 0),
                "review_summary": dec.get("review_summary", ""),
            },
            model=extract_model,
            turn_ids=[turn_id],
        )
        if ok:
            count += 1
    return count


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


RateEstimator = TimingsBasedRateEstimator  # old name → new location


from lib.llm.client import call_llm as _call_llm_endpoint


def call_llm(port: int, model_name: str, system_prompt: str, user_prompt: str,
             max_tokens: int = 512, timeout: int = 180, retries: int = 2
             ) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Call LLM via lib.llm.client — adapter for review_worker's port-based signature."""
    endpoint = f"http://127.0.0.1:{port}/v1/chat/completions"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt + "\n/no_think"},
    ]

    @retry(stop=stop_after_attempt(retries + 1),
           wait=wait_exponential(multiplier=1, min=1, max=8),
           retry=retry_if_exception_type((TimeoutError, OSError, ConnectionError, Exception)),
           after=lambda rs: print(f"    LLM retry: {rs.outcome.exception()}")
           if rs.failed else None)
    def _do_call():
        status, body = _call_llm_endpoint(
            endpoint, messages, model=model_name, timeout=timeout, max_tokens=max_tokens,
        )
        if status != 200:
            raise ConnectionError(f"HTTP {status}: {body.get('error', 'unknown')}")
        content = body["choices"][0]["message"]["content"]
        result = _parse_llm_json(content)
        return result, body.get("usage", {})

    try:
        return _do_call()
    except Exception:
        return (None, None)


def _parse_llm_json(content: str) -> Optional[Dict]:
    """Thin wrapper — delegates to shared Recovery Ladder in lib.llm.json_parser."""
    from lib.llm.json_parser import parse_llm_json
    return parse_llm_json(content)


def build_extract_prompt(turn: Dict) -> str:
    """Build prompt with full turn text for evidence substring verification."""
    return (
        f"[{turn['agent']}] user: {turn['user_turn']}\n"
        f"[{turn['agent']}] text: {turn['text']}"
    )


def main():
    import argparse

    ap = argparse.ArgumentParser(description="Fact extraction + Python evidence verification")
    ap.add_argument("--reset", action="store_true", help="Re-review all turns in 24h window")
    ap.add_argument("--limit", type=int, default=20, help="Max turns per run (default: 20)")
    args = ap.parse_args()

    ts = datetime.now(timezone.utc)
    print(f"[{ts.isoformat()}] review_worker starting (DeepSeek extract + Python verify)")
    _notify_slack(f"review_worker extract+verify start — {ts.strftime('%H:%M:%S')} UTC")

    ensure_review_table()
    from lib.worklog import log_commits_to_worklog
    log_commits_to_worklog()

    checkpoint = load_checkpoint()
    if args.reset:
        CHECKPOINT_FILE.unlink(missing_ok=True)
        checkpoint = {}
        print("  Reset: cleared checkpoint")

    model = MODELS[EXTRACT_MODEL]
    turns = fetch_unprocessed_turns(args.limit, checkpoint)
    if not turns:
        print("No unprocessed turns in 24h window")
        return 0
    print(f"Fetched {len(turns)} unprocessed turns")

    if not check_endpoint(model["port"], EXTRACT_MODEL + " (DeepSeek extract)"):
        print(f"FATAL: {EXTRACT_MODEL} endpoint :{model['port']} not responding")
        _notify_slack(f"FATAL: {EXTRACT_MODEL} :{model['port']} down")
        return 1

    review_model = MODELS["qwen-14b"]
    if not check_endpoint(review_model["port"], "Qwen-14B (debate review)"):
        print(f"FATAL: Qwen-14B on :{review_model['port']} not responding — review cannot proceed")
        _notify_slack(f"FATAL: Qwen-14B :{review_model['port']} down")
        return 1

    est = RateEstimator(EXTRACT_MODEL)

    total_facts = total_valid = total_hall = 0

    for i, turn in enumerate(turns):
        prompt = build_extract_prompt(turn)
        ptok = int((len(EXTRACT_SYSTEM.split()) + len(prompt.split())) * 1.3)
        timeout = est.calc_timeout(ptok, 512) if est.calls > 0 else 180

        # Phase 1: DeepSeek-V2-Lite extracts facts
        result, timings = call_llm(model["port"], model["name"],
                                   EXTRACT_SYSTEM, prompt,
                                   max_tokens=512, timeout=timeout)
        facts = result.get("facts", []) if result else []
        if timings:
            est.update(timings)

        rate = timings.get("predicted_per_second", 0) if timings else 0
        print(f"  Turn {i+1}/{len(turns)}: {len(facts)} facts @{rate:.1f}t/s")

        if not facts:
            continue

        # Phase 2: Python deterministic evidence verification
        decisions = []
        turn_text = turn["user_turn"] + "\n" + turn["text"]
        for j, fact in enumerate(facts):
            evidence = fact.get("evidence", "")
            if verify_evidence(evidence, turn_text):
                decisions.append({
                    "fact_index": j,
                    "verdict": "valid",
                    "reason": "evidence found verbatim in turn",
                    "source": "deepseek",
                })
                total_valid += 1
            else:
                decisions.append({
                    "fact_index": j,
                    "verdict": "hallucinated",
                    "reason": "evidence not found in turn text",
                    "source": "deepseek",
                })
                total_hall += 1

        total_facts += len(facts)

        # Phase 3: Qwen-14B debate review — evaluate verified facts as a batch
        verified_facts = [
            {"index": d["fact_index"], "type": facts[d["fact_index"]].get("fact_type", ""),
             "evidence": facts[d["fact_index"]].get("evidence", ""),
             "speaker": facts[d["fact_index"]].get("speaker", "")}
            for d in decisions if d["verdict"] == "valid"
        ]

        review_verdict = {}
        review_failed = False
        if verified_facts:
            review_prompt = (
                f"Turn by [{turn['agent']}]:\n{turn['user_turn']}\n\n"
                f"Python-verified facts (evidence confirmed verbatim in turn):\n"
                f"{json.dumps(verified_facts, ensure_ascii=False, indent=2)}"
            )
            review_result, _ = call_llm(
                review_model["port"], review_model["name"],
                FACT_REVIEWER_SYSTEM, review_prompt,
                max_tokens=512, timeout=300,
            )
            if review_result:
                review_verdict = review_result
                print(f"    review: {review_result.get('verdict','?')} consensus={review_result.get('consensus_score','?')}")
            else:
                print(f"    review: FAILED (Qwen-14B unavailable) — turn skipped, will retry next cycle")
                review_failed = True

        if review_failed:
            # 14B unavailable: do NOT save to review_facts — that would cause
            # NOT EXISTS filter to skip this turn forever. Retry next cycle.
            print(f"    skipped — will retry next cycle")
            continue

        # Merge review verdict into each decision
        for dec in decisions:
            dec["review_verdict"] = review_verdict.get("verdict", "pending")
            dec["review_consensus"] = review_verdict.get("consensus_score", 0)
            dec["review_issues"] = review_verdict.get("issues", [])
            dec["review_summary"] = review_verdict.get("summary", "")

        # Phase 4: Store
        phase_label = "extract_verify_review"
        save_review_facts(turn["id"], facts, decisions,
                          extract_model=model["name"],
                          phase=phase_label, timings=timings)
        _insert_activity_review(turn["id"], facts, decisions,
                                extract_model=model["name"])

    vr = round(total_valid / max(total_facts, 1) * 100, 1)
    print(f"\nDone: {total_facts}f, {total_valid}v ({vr}%), {total_hall}h")

    _print_stats(est)
    _notify_slack(f"review_worker done — {total_facts}f/{total_valid}v ({vr}%), "
                   f"hall={total_hall}")

    max_ts = max((t.get("created_at", "") for t in turns), default="")
    if max_ts:
        save_checkpoint({"last_ts": max_ts})
    return 0


def _notify_slack(text: str) -> None:
    try:
        import urllib.request as _ur
        payload = json.dumps({"channel": "U0APJGD8CBW", "text": text}).encode()
        req = _ur.Request("https://slack.com/api/chat.postMessage", data=payload,
                          headers={"Authorization": "Bearer xoxb-10781519811159-11168454462293-A9nR8gdlZiPkrSwdE656CAHK",
                                   "Content-Type": "application/json"})
        _ur.urlopen(req, timeout=5)
    except Exception:
        pass


def _print_stats(est: RateEstimator):
    print("\n── performance stats ──")
    s = est.stats()
    cp = round(est.cache_hits / max(est.calls, 1) * 100, 1)
    print(f"  {est.label}:")
    print(f"    rate: prompt={s['prompt_rate']:.1f} t/s, gen={s['gen_rate']:.1f} t/s"
          f" (median={s['median_gen_rate']:.1f})")
    print(f"    tokens: prompt={s['total_prompt']}, gen={s['total_gen']},"
          f" elapsed={s['total_elapsed_s']:.0f}s")
    print(f"    calls: {est.calls}, cache_hits: {est.cache_hits} ({cp}%)")


if __name__ == "__main__":
    sys.exit(main())
