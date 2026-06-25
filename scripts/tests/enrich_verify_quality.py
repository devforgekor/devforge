#!/usr/bin/env python3
# Status: experimental
# Path: none — manual enrich quality scoring test (moved from pipelines/)
"""Enrich Verify — score enrichment metadata quality manually.

Manual test. Reads turn + enrich_meta from DB, sends to judge model (Pod B :8083),
per-field scores 0-100, stores as fact_type='enrich_verify'.

Usage:
  python3 scripts/tests/enrich_verify_quality.py --limit 10                  # batch
  python3 scripts/tests/enrich_verify_quality.py --turn-ids id1,id2          # specific
  python3 scripts/tests/enrich_verify_quality.py --limit 10 --model 7b       # label
  python3 scripts/tests/enrich_verify_quality.py --limit 10 --dry-run        # simulate
"""

import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql, psql_ok, esc_sql, psql_json
from lib.common import strip_think
from lib.llm_client import call_llm
from lib.infra.preflight import preflight_checks
from lib.token_budget import TokenBudget
from lib.llm.rate_estimator import TimingsBasedRateEstimator

# ── Constants ──────────────────────────────────────────────────────────────
TIMEOUT_VERIFY = 900
MAX_TOKENS_VERIFY = 512
TEMP_VERIFY = 0.1
BATCH_LIMIT = 10

# Self-calibrating rate estimator for dynamic timeout
_RATE_EST = TimingsBasedRateEstimator(label="enrich_verify", initial_prompt=10.0, initial_gen=3.0)

# ── System prompt ──────────────────────────────────────────────────────────
SYSTEM_ENRICH_VERIFY = """\
You are an MCP metadata quality verifier. Given the original conversation turn
and the generated MCP metadata, score each field on a 0-100 scale.

Score guidelines:
  tldr_accuracy:   0=wrong/fabricated  50=partial  100=perfect summary
  intent_correctness: 0=wrong intent  50=plausible  100=exact match
  category_correctness: 0=wrong category  50=plausible  100=exact match
  entity_precision:  0=none in turn  50=some correct  100=all in turn
  entity_recall:     0=missing key entities  50=partial  100=all captured
  tag_relevance:     0=irrelevant  50=somewhat  100=perfectly relevant

Output STRICT JSON — no markdown, no explanation outside JSON:
{
  "tldr_accuracy": <0-100>,
  "intent_correctness": <0-100>,
  "category_correctness": <0-100>,
  "entity_precision": <0-100>,
  "entity_recall": <0-100>,
  "tag_relevance": <0-100>,
  "overall": <0-100>,
  "issues": ["brief description of each issue", ...],
  "strengths": ["brief description of each strength", ...]
}

If no issues, issues: [].
If no strengths, strengths: [].
Be critical — default is to find issues, not praise."""


# ── Build prompt ──────────────────────────────────────────────────────────
def _build_verify_prompt(turn: Dict[str, str], enrich_data: Dict[str, Any]) -> str:
    """Build the user prompt for enrichment verification using TokenBudget priority allocation.

    Priority: user_turn(10) > text(7) > thinking(4).
    Sections exceeding budget are dropped entirely — no partial truncation.
    """
    budget = TokenBudget("enrich_verify")
    parts = ["=== TURN ==="]

    user_turn = turn.get('user_turn', '') or ''
    if budget.add_section("user_turn", user_turn, priority=10):
        parts.append(f"user_turn: {user_turn}")
    text = turn.get('text', '') or ''
    if budget.add_section("text", text, priority=7):
        parts.append(f"text: {text}")
    thinking = turn.get('thinking', '') or ''
    if budget.add_section("thinking", thinking, priority=4):
        parts.append(f"thinking: {thinking}")

    parts.append("")
    parts.append("=== GENERATED ENRICHMENT METADATA ===")
    parts.append(f"tldr: {enrich_data.get('tldr', '')}")
    parts.append(f"intent: {enrich_data.get('intent', '')}")
    parts.append(f"category: {enrich_data.get('category', '')}")
    parts.append(f"entities: {json.dumps(enrich_data.get('entities', {}), ensure_ascii=False)}")
    parts.append(f"tags: {json.dumps(enrich_data.get('tags', []), ensure_ascii=False)}")

    prompt = "\n".join(parts)
    if budget.used > 0:
        print(f"    budget: {budget.used}/{budget.limit} tok", flush=True)
    return prompt


# ── JSON parser ────────────────────────────────────────────────────────────


def _parse_verify_json(raw: str) -> Optional[Dict[str, Any]]:
    """Parse verify JSON from LLM output."""
    cleaned = strip_think(raw)
    # Try direct parse first
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Try extracting from code fences
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', cleaned, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


# ── Select turns with enrich data ──────────────────────────────────────
def _get_turns_with_enrich(limit: int = BATCH_LIMIT,
                        turn_ids: Optional[List[str]] = None) -> List[Dict]:
    """Return turns that have enrich_meta and no enrich_verify yet."""
    if turn_ids:
        ids_literal = ", ".join(f"'{esc_sql(t)}'::uuid" for t in turn_ids)
        cond = f"t.id IN ({ids_literal})"
    else:
        cond = (
            "NOT EXISTS ("
            "  SELECT 1 FROM review_facts rv "
            "  WHERE rv.turn_id = t.id AND rv.fact_type = 'enrich_verify'"
            ")"
        )
    sql = (
        "SELECT t.id, t.user_turn, t.thinking, t.text, "
        "       rf.evidence::text AS enrich_json, "
        "       t.created_at::text "
        "FROM turns t "
        "JOIN review_facts rf ON rf.turn_id = t.id AND rf.fact_type = 'enrich_meta' "
        f"WHERE {cond} "
        "ORDER BY t.created_at DESC "
        f"LIMIT {limit}"
    )
    rows = psql_json(sql)
    if not rows:
        return []
    result = []
    for r in rows:
        enrich_raw = r.get("enrich_json", "")
        enrich_data = None
        if enrich_raw:
            try:
                enrich_data = json.loads(enrich_raw)
            except json.JSONDecodeError:
                pass
        result.append({
            "id": r["id"],
            "user_turn": r.get("user_turn", ""),
            "thinking": r.get("thinking") or "",
            "text": r.get("text", ""),
            "enrich": enrich_data,
            "created_at": r.get("created_at", ""),
        })
    return result


# ── DB writer ─────────────────────────────────────────────────────────────
def _insert_verify_fact(turn_id: str, fact_index: int,
                        verify_json_str: str, model_label: str,
                        prompt_tokens: Optional[int] = None,
                        gen_tokens: Optional[int] = None,
                        elapsed_ms: Optional[float] = None,
                        source_file: Optional[str] = None) -> bool:
    """Insert an enrich_verify fact row."""
    cols = ["turn_id", "fact_index", "fact_type", "evidence",
            "extract_model", "verdict", "source", "fact_action"]
    vals = [
        f"'{esc_sql(turn_id)}'::uuid",
        str(fact_index),
        "'enrich_verify'",
        f"'{esc_sql(verify_json_str[:5000])}'",
        f"'{esc_sql(model_label)}'",
        "'pending'",
        f"'enrich_verify_{esc_sql(model_label)}'",
        "'verify'",
    ]
    set_clauses = []

    if prompt_tokens is not None:
        cols.extend(["prompt_tokens", "gen_tokens"])
        vals.extend([str(prompt_tokens), str(gen_tokens)])
        set_clauses.append(f"prompt_tokens = {prompt_tokens}")
    if gen_tokens is not None:
        set_clauses.append(f"gen_tokens = {gen_tokens}")
    if elapsed_ms is not None:
        cols.append("elapsed_ms")
        vals.append(f"{elapsed_ms:.1f}")
        set_clauses.append(f"elapsed_ms = {elapsed_ms:.1f}")
    if source_file:
        cols.append("source_file")
        vals.append(f"'{esc_sql(source_file)}'")
        set_clauses.append(f"source_file = '{esc_sql(source_file)}'")

    sql = (
        f"INSERT INTO review_facts ({', '.join(cols)}) "
        f"VALUES ({', '.join(vals)}) "
        f"ON CONFLICT (turn_id, fact_index, extract_model) "
        f"DO UPDATE SET evidence = EXCLUDED.evidence"
        + (f", {', '.join(set_clauses)}" if set_clauses else "")
    )
    return psql_ok(sql)


# ── Pipeline ──────────────────────────────────────────────────────────────
def enrich_verify_pipeline(turn_ids: Optional[List[str]] = None,
                        limit: int = BATCH_LIMIT,
                        dry_run: bool = False,
                        model_label: str = "7b") -> Dict[str, Any]:
    """Verify enrichment metadata quality using judge model."""
    t_start = time.monotonic()
    print(f"\n{'=' * 60}")
    print(f"Enrich Verify Pipeline — judge model on Pod B (:8083)")
    print(f"  Label: {model_label}")
    if dry_run:
        print("  [DRY RUN] No writes to DB")
    print(f"{'=' * 60}")

    turns = _get_turns_with_enrich(limit, turn_ids)
    if not turns:
        print("[enrich_verify] No turns with enrich data found")
        return {"processed": 0, "failed": 0, "ok": True}

    print(f"[enrich_verify] Processing {len(turns)} turn(s)", flush=True)

    processed = 0
    failed = 0
    all_scores = []

    for ti, turn in enumerate(turns, 1):
        turn_id = turn["id"]
        enrich_data = turn.get("enrich")
        if not enrich_data:
            print(f"  [{ti}/{len(turns)}] {turn_id[:8]} — no enrich data, skip", flush=True)
            failed += 1
            continue

        prompt = _build_verify_prompt(turn, enrich_data)
        prompt_tok = len(prompt) // 3  # rough estimate for calc_timeout
        dynamic_timeout = _RATE_EST.calc_timeout(prompt_tok, MAX_TOKENS_VERIFY)
        print(f"  [{ti}/{len(turns)}] {turn_id[:8]} — timeout={dynamic_timeout}s", flush=True)

        try:
            resp = call_llm(
                [{"role": "system", "content": SYSTEM_ENRICH_VERIFY},
                 {"role": "user", "content": prompt}],
                model="reviewer",
                max_tokens=MAX_TOKENS_VERIFY, temperature=TEMP_VERIFY,
                timeout=dynamic_timeout, json_mode=True, return_meta=True,
            )
            # Update rate estimator from response timings
            timings = resp.get("timings") or resp.get("usage", {})
            if isinstance(timings, dict):
                _RATE_EST.update(timings)
            result = _parse_verify_json(resp["content"])
            if not result:
                print(f"    Parse failed — raw: {resp['content'][:100]}", flush=True)
                failed += 1
                continue

            # Validate scores
            for field in ("tldr_accuracy", "intent_correctness", "category_correctness",
                          "entity_precision", "entity_recall", "tag_relevance"):
                if field not in result or not isinstance(result.get(field), (int, float)):
                    result[field] = 0

            overall = result.get("overall", 0)
            issues = result.get("issues", [])
            strengths = result.get("strengths", [])

            print(f"    scores: tldr={result['tldr_accuracy']} "
                  f"intent={result['intent_correctness']} "
                  f"cat={result['category_correctness']} "
                  f"entP={result['entity_precision']} "
                  f"entR={result['entity_recall']} "
                  f"tag={result['tag_relevance']} "
                  f"overall={overall}", flush=True)
            if issues:
                print(f"    issues ({len(issues)}): {issues[0][:80]}", flush=True)
            if strengths:
                print(f"    strengths ({len(strengths)}): {strengths[0][:80]}", flush=True)

            all_scores.append({
                "turn_id": turn_id,
                "scores": {k: result.get(k, 0) for k in
                           ("tldr_accuracy", "intent_correctness", "category_correctness",
                            "entity_precision", "entity_recall", "tag_relevance",
                            "overall")},
                "issues": issues[:3],
                "strengths": strengths[:2],
            })

            if dry_run:
                processed += 1
                continue

            # Store
            fi_sql = (
                f"SELECT COALESCE(MAX(fact_index), -1) + 1 "
                f"FROM review_facts WHERE turn_id = '{esc_sql(turn_id)}'::uuid"
            )
            fi_str = psql(fi_sql)
            fi = int(fi_str) if fi_str and fi_str != "-infinity" else 0

            usage = resp.get("usage", {})
            _insert_verify_fact(
                turn_id, fi, json.dumps(result, ensure_ascii=False),
                model_label,
                prompt_tokens=usage.get("prompt_tokens"),
                gen_tokens=usage.get("completion_tokens"),
                elapsed_ms=resp.get("elapsed_ms"),
            )
            print(f"    Stored enrich_verify ({model_label})", flush=True)
            processed += 1

        except Exception as e:
            print(f"    ERROR: {type(e).__name__}: {e}", flush=True)
            failed += 1

    elapsed = round(time.monotonic() - t_start, 1)
    print(f"\n{'=' * 60}", flush=True)
    print(f"Done: {processed} verified, {failed} failed ({elapsed}s)", flush=True)
    if dry_run:
        print("  [DRY RUN] No data was written", flush=True)

    # Summary
    if all_scores:
        avg = {k: round(sum(s["scores"][k] for s in all_scores) / len(all_scores), 1)
               for k in all_scores[0]["scores"]}
        print(f"\nAverage scores ({model_label}):", flush=True)
        for k, v in avg.items():
            print(f"  {k}: {v}", flush=True)

    print(f"{'=' * 60}", flush=True)

    return {
        "processed": processed, "failed": failed,
        "elapsed_s": elapsed, "ok": failed == 0,
        "averages": avg if all_scores else {},
        "scores": all_scores,
    }


# ── CLI ───────────────────────────────────────────────────────────────────
def main() -> None:
    preflight_checks("enrich_verify.py", required_ports={8083})
    import argparse
    parser = argparse.ArgumentParser(
        description="Enrich Verify Quality — score enrichment metadata manually")
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    parser.add_argument("--turn-ids", help="Comma-separated turn UUIDs")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model", default="verify",
                        help="Model label for output (7b, 14b, etc)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    turn_ids = [t.strip() for t in args.turn_ids.split(",")
                ] if args.turn_ids else None

    result = enrich_verify_pipeline(
        turn_ids=turn_ids,
        limit=args.limit,
        dry_run=args.dry_run,
        model_label=args.model,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
