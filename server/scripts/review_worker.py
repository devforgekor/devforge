#!/usr/bin/env python3
"""review_worker.py — 3-LLM debate pipeline for DevForge.

SLOC-exempt: 695 lines — single cohesive 3-LLM debate pipeline (extract A+B
→ compare → arbitrate → store). Shared checkpoint, RateEstimator instances,
LLM client, and DB inserts tie all stages together.

Phase 1 (extract A+B): Qwen3-4B (Podman A :8080) + Llama-3B (Podman B :8082)
                       independently extract facts from turns — parallel HTTP.
Phase 2 (compare):     Match facts by evidence overlap. Identical → confirmed.
                       Only one model found → conflict.
Phase 3 (arbitrate):   Phi-4-mini (Podman B :8081) adjudicates conflicts.

24h rolling window, checkpoint-based incremental processing.
All 3 containers are always running in normal mode — no model swapping needed.

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
from lib.llm.rate_estimator import TimingsBasedRateEstimator

CHECKPOINT_FILE = Path("/opt/projects/server/review_checkpoint.json")

MODELS = {
    "qwen4b": {
        "name": "qwen3-4b",
        "port": 8080,
    },
    "phi4mini": {
        "name": "phi-4-mini",
        "port": 8081,
    },
    "llama3b": {
        "name": "llama-3.2-3b",
        "port": 8082,
    },
}

# 3-LLM debate pipeline:
# Phase 1: Qwen3-4B (:8080) + Llama-3B (:8082) extract independently (parallel)
# Phase 2: Compare facts — match by evidence overlap
# Phase 3: Phi-4-mini (:8081) arbitrates conflicts
EXTRACT_MODEL_A = "qwen4b"
EXTRACT_MODEL_B = "llama3b"
ARBITRATOR_MODEL = "phi4mini"

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

ARBITRATOR_SYSTEM = """You are an impartial fact arbitrator. Two models independently extracted facts from the same conversation turn. They disagree on some facts. Your job: decide which facts are correct.

For each conflicting fact:
1. Check if the evidence is VERBATIM in the original turn text
2. If yes → "valid" (the fact is correct)
3. If paraphrased or inferred → "modified" (fact is true but evidence isn't exact — provide corrected evidence)
4. If not present at all → "hallucinated" (reject)
5. If needs prior context → "context_dependent"

Return JSON:
{"decisions": [{"fact_index": 0, "source": "A|B", "verdict": "valid|hallucinated|modified|context_dependent", "corrected_evidence": "exact text from turn or null", "reason": "short explanation"}]}"""


def get_extract_system(model_key: str) -> str:
    return MODELS.get(model_key, {}).get("extract_system", EXTRACT_SYSTEM)


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


def save_review_facts(turn_id: str, facts: List[Dict], decisions: List[Dict],
                      extract_model_a: str, extract_model_b: str,
                      arbitrator_model: str = "",
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
        source = esc_sql(dec.get("source", ""))
        prompt_n = t.get("prompt_n", 0) or 0
        pred_n = t.get("predicted_n", 0) or 0
        gen_rate = round(t.get("predicted_per_second", 0) or 0, 1)
        elapsed = round((t.get("prompt_ms", 0) or 0) + (t.get("predicted_ms", 0) or 0), 0)
        cache_n = t.get("cache_n", 0) or 0

        ok = psql_ok(f"""
        INSERT INTO review_facts (turn_id, fact_index, evidence, fact_type, verdict, reason,
            extract_model, verify_model, arbitrator_model, source, corrected_evidence,
            phase, prompt_tokens, gen_tokens, gen_rate, elapsed_ms, cache_hit)
        VALUES ('{turn_id}', {idx}, '{evidence}', '{fact_type}', '{verdict}', '{reason}',
            '{extract_model_a}', '{extract_model_b}', '{arbitrator_model}', '{source}',
            '{corrected}', '{phase}', {prompt_n}, {pred_n}, {gen_rate}, {elapsed}, {cache_n})
        ON CONFLICT (turn_id, fact_index, extract_model) DO UPDATE SET
            verdict = EXCLUDED.verdict,
            reason = EXCLUDED.reason,
            verify_model = EXCLUDED.verify_model,
            arbitrator_model = EXCLUDED.arbitrator_model,
            source = EXCLUDED.source,
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


def _insert_activity_review(turn_id: str, facts: List[Dict], reviews: List[Dict],
                            verify_model: str) -> int:
    """Insert verified findings into activity_log for nightly relay consumption.
    Each finding is a separate row with queue_status='unprocessed'.
    Returns number of rows inserted."""
    import json as _json
    count = 0
    for review in reviews:
        idx = review.get("fact_index", 0)
        fact = facts[idx] if idx < len(facts) else {}
        evidence = fact.get("evidence", "")[:200]
        fact_type = fact.get("fact_type", "general")
        verdict = review.get("verdict", "pending")
        reason = review.get("reason", "")[:200]

        title = f"review: {fact_type} — {evidence[:80]}"
        summary = f"[{verdict}] {reason}"
        body = _json.dumps({
            "fact_index": idx, "fact_type": fact_type,
            "evidence": evidence, "verdict": verdict, "reason": reason
        }, ensure_ascii=False)

        body_esc = body.replace("'", "''")
        ok = psql_ok(f"""INSERT INTO activity_log (type, source, title, summary, body,
            model, turn_ids, summary_status, queue_status, exec_status)
        VALUES ('review', 'review_worker', '{esc_sql(title)}',
                '{esc_sql(summary)}', '{body_esc}',
                '{esc_sql(verify_model)}', ARRAY['{turn_id}']::UUID[],
                'raw', 'unprocessed', 'DONE')""")
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
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

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


def build_extract_prompt(turn: Dict) -> str:
    return f"[{turn['agent']}] user: {turn['user_turn']}\n[{turn['agent']}] text: {turn['text']}"


def build_arbitration_prompt(turn: Dict, conflicts: List[Dict]) -> str:
    """Build prompt for Phi-4-mini to arbitrate conflicting facts."""
    lines = [
        f"## Original Turn\n[{turn['agent']}] user: {turn['user_turn']}\n[{turn['agent']}] text: {turn['text']}",
        "\n## Conflicting Facts (A=Qwen3-4B, B=Llama-3B)",
    ]
    for c in conflicts:
        lines.append(f"\nFact {c['index']}:")
        lines.append(f"  A: [{c.get('fact_type_a', '?')}] {c.get('evidence_a', '')}")
        lines.append(f"  B: [{c.get('fact_type_b', '?')}] {c.get('evidence_b', '')}")
    return "\n".join(lines)


def _evidence_overlap(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def compare_facts(facts_a: List[Dict], facts_b: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Compare facts from two models. Returns (confirmed, conflicts).

    confirmed: facts both models agree on (overlap > 0.6)
    conflicts: facts only one model found (overlap < 0.4 to any counterpart)
    """
    confirmed = []
    used_b = set()

    for fa in facts_a:
        best_overlap = 0.0
        best_j = -1
        for j, fb in enumerate(facts_b):
            if j in used_b:
                continue
            overlap = _evidence_overlap(fa.get("evidence", ""), fb.get("evidence", ""))
            if overlap > best_overlap:
                best_overlap = overlap
                best_j = j

        if best_overlap >= 0.6:
            used_b.add(best_j)
            confirmed.append({
                "evidence": fa["evidence"],  # use A's evidence
                "fact_type": fa.get("fact_type", ""),
                "speaker": fa.get("speaker", ""),
                "source": "both",
            })
        else:
            # No match in B — this is a conflict candidate
            confirmed.append({
                "evidence": fa.get("evidence", ""),
                "fact_type": fa.get("fact_type", ""),
                "speaker": fa.get("speaker", ""),
                "source": "A_only",
                "index": len(confirmed),
                "evidence_a": fa.get("evidence", ""),
                "fact_type_a": fa.get("fact_type", ""),
                "evidence_b": facts_b[best_j].get("evidence", "") if best_j >= 0 else "",
                "fact_type_b": facts_b[best_j].get("fact_type", "") if best_j >= 0 else "",
            })

    # Remaining B facts (not matched to any A fact)
    conflicts = [c for c in confirmed if c.get("source") == "A_only"]
    for j, fb in enumerate(facts_b):
        if j not in used_b:
            conflicts.append({
                "evidence": fb.get("evidence", ""),
                "fact_type": fb.get("fact_type", ""),
                "speaker": fb.get("speaker", ""),
                "source": "B_only",
                "index": len(conflicts),
                "evidence_a": "",
                "fact_type_a": "",
                "evidence_b": fb.get("evidence", ""),
                "fact_type_b": fb.get("fact_type", ""),
            })
            confirmed.append({
                "evidence": fb.get("evidence", ""),
                "fact_type": fb.get("fact_type", ""),
                "speaker": fb.get("speaker", ""),
                "source": "B_only",
            })

    # Filter confirmed to only "both" sources
    confirmed_both = [c for c in confirmed if c.get("source") == "both"]
    return confirmed_both, conflicts


def main():
    import argparse
    from concurrent.futures import ThreadPoolExecutor

    ap = argparse.ArgumentParser(description="3-LLM debate review pipeline")
    ap.add_argument("--reset", action="store_true", help="Re-review all turns in 24h window")
    ap.add_argument("--limit", type=int, default=20, help="Max turns per run (default: 20)")
    args = ap.parse_args()

    ts = datetime.now(timezone.utc)
    print(f"[{ts.isoformat()}] review_worker starting (3-LLM debate)")
    _notify_slack(f"review_worker 3-LLM debate start — {ts.strftime('%H:%M:%S')} UTC")

    ensure_review_table()
    from lib.worklog import log_commits_to_worklog
    log_commits_to_worklog()

    checkpoint = load_checkpoint()
    if args.reset:
        CHECKPOINT_FILE.unlink(missing_ok=True)
        checkpoint = {}
        print("  Reset: cleared checkpoint")

    model_a = MODELS[EXTRACT_MODEL_A]
    model_b = MODELS[EXTRACT_MODEL_B]
    model_arb = MODELS[ARBITRATOR_MODEL]
    turns = fetch_unprocessed_turns(args.limit, checkpoint, model_a["name"])
    if not turns:
        print("No unprocessed turns in 24h window")
        return 0
    print(f"Fetched {len(turns)} unprocessed turns")

    for key, mdl in [(EXTRACT_MODEL_A, model_a), (EXTRACT_MODEL_B, model_b),
                      (ARBITRATOR_MODEL, model_arb)]:
        if not check_endpoint(mdl["port"], key):
            print(f"FATAL: {key} endpoint :{mdl['port']} not responding")
            _notify_slack(f"FATAL: {key} :{mdl['port']} down")
            return 1

    est_a = RateEstimator(EXTRACT_MODEL_A)
    est_b = RateEstimator(EXTRACT_MODEL_B)
    est_arb = RateEstimator(ARBITRATOR_MODEL, initial_gen=3.0)

    # Phase 1: Parallel independent extraction (Qwen3-4B + Llama-3B)
    print(f"\nPhase 1 — parallel extract: {model_a['name']} + {model_b['name']}...")
    combo_facts = []

    for i, turn in enumerate(turns):
        prompt = build_extract_prompt(turn)
        sys_a = get_extract_system(EXTRACT_MODEL_A)
        sys_b = get_extract_system(EXTRACT_MODEL_B)

        def _extract(mdl, sys_prompt, est):
            ptok = int((len(sys_prompt.split()) + len(prompt.split())) * 1.3)
            timeout = est.calc_timeout(ptok, 512) if est.calls > 0 else 180
            result, timings = call_llm(mdl["port"], mdl["name"],
                                        sys_prompt, prompt,
                                        max_tokens=512, timeout=timeout)
            return result.get("facts", []) if result else [], timings, result is not None

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_a = pool.submit(_extract, model_a, sys_a, est_a)
            f_b = pool.submit(_extract, model_b, sys_b, est_b)
            facts_a, timings_a, ok_a = f_a.result()
            facts_b, timings_b, ok_b = f_b.result()

        if timings_a: est_a.update(timings_a)
        if timings_b: est_b.update(timings_b)
        ra = timings_a.get("predicted_per_second", 0) if timings_a else 0
        rb = timings_b.get("predicted_per_second", 0) if timings_b else 0

        print(f"  Turn {i+1}/{len(turns)}: A={len(facts_a)}f @{ra:.1f}t/s  "
              f"B={len(facts_b)}f @{rb:.1f}t/s")

        combo_facts.append({
            "turn_index": i, "turn_id": turn["id"], "turn_agent": turn["agent"],
            "turn_data": turn,
            "facts_a": facts_a, "facts_b": facts_b,
            "timings_a": timings_a, "timings_b": timings_b,
        })

    # Phase 2: Compare + Phase 3: Arbitrate
    total_confirmed = total_conflicts = total_valid = total_hall = 0

    for entry in combo_facts:
        if not entry["facts_a"] and not entry["facts_b"]:
            continue

        confirmed, conflicts = compare_facts(entry["facts_a"], entry["facts_b"])
        total_confirmed += len(confirmed)
        total_conflicts += len(conflicts)

        if not conflicts:
            decisions = [{"fact_index": i, "verdict": "valid",
                          "reason": "both models agree", "source": "both"}
                         for i in range(len(confirmed))]
            entry["decisions"] = decisions
            total_valid += len(decisions)
            print(f"  Turn {entry['turn_index']+1}: {len(confirmed)} confirmed (all agree)")
            combined_timings = {}
            for k in ("prompt_n", "predicted_n", "predicted_per_second",
                       "prompt_ms", "predicted_ms", "cache_n"):
                combined_timings[k] = (entry.get("timings_a", {}) or {}).get(k, 0) + \
                                      (entry.get("timings_b", {}) or {}).get(k, 0)
            save_review_facts(entry["turn_id"], confirmed, decisions,
                              model_a["name"], model_b["name"], phase="debate",
                              timings=combined_timings)
            continue

        # Phase 3: Arbitrate conflicts
        prompt = build_arbitration_prompt(entry["turn_data"], conflicts)
        ptok = int((len(ARBITRATOR_SYSTEM.split()) + len(prompt.split())) * 1.3)
        timeout = est_arb.calc_timeout(ptok, 512) if est_arb.calls > 0 else 120
        result, timings = call_llm(model_arb["port"], model_arb["name"],
                                    ARBITRATOR_SYSTEM, prompt,
                                    max_tokens=512, timeout=timeout)
        arb_decisions = result.get("decisions", []) if result else []
        if not isinstance(arb_decisions, list):
            print(f"  WARNING: arb_decisions is {type(arb_decisions).__name__}, expected list — treating as empty")
            arb_decisions = []
        if timings: est_arb.update(timings)

        all_decisions = [{"fact_index": i, "verdict": "valid",
                           "reason": "both agree", "source": "both"}
                          for i in range(len(confirmed))]
        for d in arb_decisions:
            arb_idx = d.get("fact_index")
            if isinstance(arb_idx, int) and arb_idx >= 0:
                d["fact_index"] = len(confirmed) + arb_idx
            else:
                d["fact_index"] = len(all_decisions)
            all_decisions.append(d)

        entry["decisions"] = all_decisions
        v = sum(1 for d in arb_decisions if d.get("verdict") == "valid")
        h = sum(1 for d in arb_decisions if d.get("verdict") == "hallucinated")
        total_valid += len(confirmed) + v
        total_hall += h

        all_facts = confirmed + conflicts
        save_review_facts(entry["turn_id"], all_facts, all_decisions,
                          model_a["name"], model_b["name"],
                          arbitrator_model=model_arb["name"],
                          phase="debate", timings=timings)
        _insert_activity_review(entry["turn_id"], all_facts, all_decisions,
                                 model_arb["name"])
        rate = timings.get("predicted_per_second", 0) if timings else 0
        print(f"  Turn {entry['turn_index']+1}: {len(confirmed)}✓ + "
              f"{len(conflicts)}⚡ → {v}v/{h}h @{rate:.1f}t/s")

    total_facts = total_confirmed + total_conflicts
    vr = round(total_valid / max(total_facts, 1) * 100, 1)
    print(f"\nDone: {total_facts}f, {total_valid}v ({vr}%), {total_hall}h")
    print(f"  confirmed: {total_confirmed}, arbitrated: {total_conflicts}")

    _print_stats_3llm(est_a, est_b, est_arb)
    _notify_slack(f"review_worker done — {total_facts}f/{total_valid}v ({vr}%), "
                   f"confirmed={total_confirmed}, arbitrated={total_conflicts}")

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


def _print_stats_3llm(est_a: RateEstimator, est_b: RateEstimator,
                        est_arb: RateEstimator):
    print("\n── performance stats ──")
    for est in (est_a, est_b, est_arb):
        if est.calls == 0:
            continue
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
