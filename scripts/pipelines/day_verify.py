#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.sh — Phase 3 (verify chain)
"""Day Verify Pipeline — verify enrichment metadata quality.

Reads enrich_meta from DB and runs verification phases:
  Phase 1: Entity disk/symbol verify (files exist? symbols found?)
  Phase 2: LLM-based faithfulness (entity + tldr grounding via verify model)
  Phase 2b: Pod A reranker relevance check on uncertain entities

Called by day_cycle.sh after extract + enrich completes.
Stores results as fact_type='verify_result', separate from enrich_meta.
Uses role alias "day_verify" (different model family from
extract/enrich — catches blind spots).
Reranker (Pod A :8080) provides topical relevance check."""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "eval")
os.makedirs(EVAL_DIR, exist_ok=True)
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(os.path.join(SCRIPTS_DIR, "pipelines"))

from lib.db import psql, psql_ok, esc_sql, psql_json
from lib.llm_client import call_llm, call_llm_with_retry, reranker_score, reranker_nli_verdict
from lib.enrich.utils import verify_entities
from lib.infra.preflight import preflight_checks
from lib.watchdog.messenger import heartbeat

BATCH_LIMIT = 10
PARALLEL = 2




def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


_ENTITY_VERIFY_PROMPT = """You are a factual consistency checker. Given a SOURCE text and a list of CLAIMS, determine if each claim is explicitly supported by the source.

SOURCE: {source}

CLAIMS:
{claims}

For each claim, answer YES if the source explicitly supports it, NO if the source contradicts it, or AMBIGUOUS if the source neither supports nor contradicts it.

Return a JSON object like: {{"claim_0": "YES", "claim_1": "NO", ...}}
Answer ONLY with the JSON object, no other text."""


def _llm_verify_entities(entities: Dict, source_text: str) -> Dict:
    """Verify entity grounding via LLM (verify role model)."""
    if not entities:
        return {}

    result: Dict[str, list] = {}
    for key in ("files", "technologies", "functions", "mentioned_users"):
        items = entities.get(key, [])
        if not isinstance(items, list):
            items = []
        checked = []
        unverified = []
        for item in items:
            s = str(item).strip()
            if not s:
                continue
            # Fast path: substring match
            if s.lower() in source_text.lower():
                checked.append({
                    "entity": s, "score": 1.0, "grounding": "GROUNDED",
                    "method": "substr", "grounded": True,
                })
            else:
                unverified.append(s)

        # Batch verify remaining via LLM
        if unverified:
            claims_str = "\n".join(f"claim_{i}: {c}" for i, c in enumerate(unverified))
            prompt = _ENTITY_VERIFY_PROMPT.format(source=source_text[:3000], claims=claims_str)
            try:
                resp = call_llm_with_retry(
                    [{"role": "user", "content": prompt}],
                    model="day_verify", max_tokens=512, temperature=0.0, timeout=60,
                )
                parsed = json.loads(resp)
                for i, c in enumerate(unverified):
                    verdict = parsed.get(f"claim_{i}", "AMBIGUOUS")
                    grounded = verdict == "YES"
                    checked.append({
                        "entity": c, "score": 1.0 if grounded else 0.0,
                        "grounding": "GROUNDED" if grounded else ("UNGROUNDED" if verdict == "NO" else "AMBIGUOUS"),
                        "method": "llm_verify", "grounded": grounded,
                        "_llm_verdict": verdict,
                    })
            except Exception as e:
                # LLM parse failure — fallback to AMBIGUOUS
                for c in unverified:
                    checked.append({
                        "entity": c, "score": 0.5, "grounding": "AMBIGUOUS",
                        "method": "llm_fallback", "grounded": True,
                    })

        result[key] = checked
    return result


def _llm_verify_tldr(tldr: str, source_text: str) -> Dict:
    """Verify tldr factual consistency via LLM (verify model on :8082)."""
    if not tldr or not source_text:
        return {"text": tldr, "grounded": True, "grounding": "SKIP", "method": "skip"}

    prompt = f"""SOURCE: {source_text[:3000]}

CLAIM: {tldr}

Is the CLAIM factually supported by the SOURCE? Answer YES, NO, or AMBIGUOUS.
Answer with one word only."""
    try:
        resp = call_llm_with_retry(
            [{"role": "user", "content": prompt}],
            model="day_verify", max_tokens=16, temperature=0.0, timeout=30,
        ).strip().upper()
        grounded = resp == "YES"
        verdict = "GROUNDED" if grounded else ("UNGROUNDED" if resp == "NO" else "AMBIGUOUS")
        return {"text": tldr, "score": 1.0 if grounded else 0.0,
                "grounding": verdict, "grounded": grounded,
                "method": "llm_verify", "_llm_verdict": resp}
    except Exception as e:
        return {"text": tldr, "score": 0.5, "grounding": "AMBIGUOUS",
                "grounded": True, "method": "llm_fallback"}


# ── Phase 2b: Pod A Reranker Faithfulness ──────────────────────────────


def _reranker_verify(faithfulness: Dict, source_text: str) -> Dict:
    """Phase 2b: Pod A reranker relevance check on uncertain entities.

    For entities/tldr that the LLM couldn't confirm via substring match,
    run Pod A reranker (:8080) as a topical relevance second opinion.

    Reranker measures TOPICAL RELATEDNESS, NOT logical entailment:
      - GROUNDED (>=0.75): on-topic → upgrade to AMBIGUOUS
        (topically relevant but reranker can't confirm factual accuracy)
      - UNGROUNDED (<0.40): off-topic → confirm ungrounded

    Mutates faithfulness dict in-place and returns it.
    """
    for key in ("files", "technologies", "functions", "mentioned_users"):
        items = faithfulness.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("grounded", True) or item.get("score", 1.0) >= 0.8:
                continue
            entity = item.get("entity", "")
            if not entity:
                continue
            score = reranker_score(entity, source_text)
            grounding = reranker_nli_verdict(score)
            if grounding == "GROUNDED":
                # On-topic but can't confirm factual accuracy
                item["grounded"] = True
                item["grounding"] = "AMBIGUOUS"
                item["score"] = round(score * 100, 1)
                item["method"] = "reranker_override"
                item["_reranker"] = grounding
            elif grounding == "UNGROUNDED":
                item["grounded"] = False
                item["grounding"] = "UNGROUNDED"
                item["score"] = round(score * 100, 1)
                item["method"] = "reranker"
                item["_reranker"] = grounding
            else:
                item["_reranker"] = grounding
    # Also check tldr
    tldr = faithfulness.get("tldr", {})
    if isinstance(tldr, dict) and not tldr.get("grounded", True):
        entity = tldr.get("text", "")
        if entity:
            score = reranker_score(entity, source_text)
            grounding = reranker_nli_verdict(score)
            if grounding == "GROUNDED":
                tldr["grounded"] = True
                tldr["grounding"] = "AMBIGUOUS"
                tldr["score"] = round(score * 100, 1)
                tldr["method"] = "reranker_override"
                tldr["_reranker"] = grounding
    return faithfulness


# ── DB helpers ────────────────────────────────────────────────────────────

def _get_turns_for_verify(limit: int = BATCH_LIMIT,
                            turn_id: Optional[str] = None) -> List[Dict]:
    """Turns that completed enrichment but still need verification.

    If turn_id is given, re-verify that specific turn (skips NOT EXISTS filter).
    """
    if turn_id:
        sql = (
            "SELECT DISTINCT ON (t.id) "
            "  t.id, t.user_turn, t.thinking, t.text, "
            "  rf.evidence::text AS enrich_meta, "
            "  t.created_at::text "
            "FROM turns t "
            "JOIN review_facts rf ON rf.turn_id = t.id "
            f"  AND rf.fact_type = 'enrich_meta' "
            f"WHERE t.id = '{esc_sql(turn_id)}'::uuid "
            "ORDER BY t.id, rf.fact_index DESC"
        )
    else:
        sql = (
            "SELECT DISTINCT ON (t.id) "
            "  t.id, t.user_turn, t.thinking, t.text, "
            "  rf.evidence::text AS enrich_meta, "
            "  t.created_at::text "
            "FROM turns t "
            "JOIN review_facts rf ON rf.turn_id = t.id "
            "  AND rf.fact_type = 'enrich_meta' "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM review_facts rf2 "
            "  WHERE rf2.turn_id = t.id "
            "  AND rf2.fact_type = 'verify_result'"
            ") "
            "ORDER BY t.id, rf.fact_index DESC"
        )
    rows = psql_json(sql) or []
    return rows[:limit]


def _compute_reranker_verdict(faithfulness: Dict) -> Optional[str]:
    """Aggregate per-entity _reranker values into a single verdict.

    Returns UNGROUNDED if any entity was confirmed ungrounded by reranker,
    otherwise None (no decisive verdict from reranker alone).
    """
    for key in ("files", "technologies", "functions", "mentioned_users"):
        items = faithfulness.get(key, [])
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("_reranker") == "UNGROUNDED":
                    return "UNGROUNDED"
    tldr = faithfulness.get("tldr", {})
    if isinstance(tldr, dict) and tldr.get("_reranker") == "UNGROUNDED":
        return "UNGROUNDED"
    return None


def _insert_verify_result(turn_id: str, fact_index: int,
                           verify_json_str: str,
                           nli_verdict: Optional[str] = None) -> bool:
    cols = ["turn_id", "fact_index", "fact_type", "evidence",
            "extract_model", "verdict", "source", "fact_action"]
    vals = [f"'{esc_sql(turn_id)}'::uuid", str(fact_index),
            "'verify_result'",
            f"'{esc_sql(verify_json_str[:5000])}'",
            "'enrich-self'", "'pending'",
            "'day_verify'", "'verify'"]
    if nli_verdict:
        cols.append("nli_verdict")
        vals.append(f"'{esc_sql(nli_verdict)}'")
    sql = (
        f"INSERT INTO review_facts ({', '.join(cols)}) "
        f"VALUES ({', '.join(vals)})"
    )
    return psql_ok(sql)


def _build_category_summary(verify_data: Dict) -> str:
    parts = []

    # Entity verify
    ev = verify_data.get("entity_verify", {})
    files = ev.get("files", [])
    syms = ev.get("symbols", [])
    missing_files = sum(1 for f in files if not f.get("exists"))
    missing_syms = sum(1 for s in syms if not s.get("found"))
    parts.append(f"entity: {len(files)} files ({missing_files} missing), "
                 f"{len(syms)} syms ({missing_syms} missing)")

    # Faithfulness
    fh = verify_data.get("faithfulness", {})
    n_ent = sum(len(v) for v in fh.values() if isinstance(v, list))
    n_fail = sum(
        1 for v in fh.values()
        if isinstance(v, list) and any(not e.get("grounded", True) for e in v)
    )
    parts.append(f"faithfulness: {n_ent} entities ({n_fail} ungrounded)")
    tldr = fh.get("tldr", {})
    if isinstance(tldr, dict):
        tldr_ok = tldr.get("grounded", True)
        parts.append(f"tldr={'OK' if tldr_ok else 'LOW'}")

    return " | ".join(parts)


# ── Pipeline ──────────────────────────────────────────────────────────────

def _process_turn(turn: Dict) -> Tuple[str, Optional[Dict], Optional[str]]:
    """Process a single turn in thread pool. Returns (turn_id, verify_result, error).

    verify_result embeds _log_* keys for sequential logging after parallel phase.
    """
    try:
        turn_id = turn["id"]
        enrich_meta_str = turn.get("enrich_meta", "")
        enrich_data = json.loads(enrich_meta_str) if enrich_meta_str else {}
        if not enrich_data:
            return (turn_id, None, None)

        verify_result: Dict[str, Any] = {}

        # Phase 1: Entity disk/symbol verify
        entities = enrich_data.get("entities", {})
        if entities:
            ev = verify_entities(enrich_data)
            verify_result["entity_verify"] = ev

        # Phase 2: LLM-based faithfulness via role "day_verify"
        user_turn = turn.get("user_turn", "") or ""
        thinking = turn.get("thinking", "") or ""
        text = turn.get("text", "") or ""
        source_text = " ".join(f"{user_turn}\n{thinking}\n{text}".split())[:4000]
        faithfulness = _llm_verify_entities(entities, source_text)

        tldr = (enrich_data.get("tldr", "") or "").strip()
        if tldr:
            faithfulness["tldr"] = _llm_verify_tldr(tldr, source_text)
        verify_result["faithfulness"] = faithfulness

        # Phase 2b: Pod A reranker second opinion on uncertain entities
        pre_reranker_ungrounded = sum(
            1 for v in faithfulness.values()
            if isinstance(v, list) and any(not e.get("grounded", True) for e in v)
        )
        if pre_reranker_ungrounded > 0:
            _reranker_verify(faithfulness, source_text)
            reranker_overrides = pre_reranker_ungrounded - sum(
                1 for v in faithfulness.values()
                if isinstance(v, list) and any(not e.get("grounded", True) for e in v)
            )
        else:
            reranker_overrides = 0

        # Embed log metadata (popped in sequential store loop)
        if entities:
            ev = verify_result.get("entity_verify", {})
            n_files = len(ev.get("files", []))
            n_syms = len(ev.get("symbols", []))
            n_missing_files = sum(1 for f in ev.get("files", []) if not f["exists"])
            n_missing_syms = sum(1 for s in ev.get("symbols", []) if not s["found"])
            verify_result["_log_entity"] = (n_files, n_missing_files, n_syms, n_missing_syms)

        if faithfulness:
            n_ent = sum(len(v) for v in faithfulness.values() if isinstance(v, list))
            n_fail = sum(
                1 for v in faithfulness.values()
                if isinstance(v, list) and any(not e.get("grounded", True) for e in v)
            )
            tldr_ok = faithfulness.get("tldr", {}).get("grounded", True) if isinstance(faithfulness.get("tldr"), dict) else True
            verify_result["_log_faith"] = (n_ent, n_fail, tldr_ok, reranker_overrides)

        return (turn_id, verify_result, None)

    except Exception as e:
        return (turn["id"], None, f"{type(e).__name__}: {e}")


def day_verify_pipeline(limit: int = BATCH_LIMIT,
                         dry_run: bool = False,
                         turn_id: Optional[str] = None) -> Dict[str, Any]:
    """Verify enrichment metadata: entity disk check + reranker + NLI self-verify."""
    t_start = time.monotonic()
    processed = 0
    failed = 0

    heartbeat("day_verify", "pipeline_start")

    log("=" * 60)
    log("Day Verify — enrichment metadata quality check")
    if dry_run:
        log("  [DRY RUN] No writes to DB")
    if turn_id:
        log(f"  [re-verify] turn_id={turn_id[:12]}")
    log("=" * 60)

    turns = _get_turns_for_verify(limit, turn_id=turn_id)
    if not turns:
        log("[done] No turns needing verification")
        return {"ok": True, "processed": 0, "elapsed_s": 0}

    log(f"Processing {len(turns)} turn(s) (parallel={PARALLEL})")

    # ── Phase 1+2+2b: Concurrent processing ────────────────────────────
    turn_results: Dict[str, Tuple[Optional[Dict], Optional[str]]] = {}
    llm_t0 = time.monotonic()

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        fut_map = {}
        for turn in turns:
            fut = pool.submit(_process_turn, turn)
            fut_map[fut] = turn
        for fut in as_completed(fut_map):
            turn = fut_map[fut]
            turn_id_val, verify_result, error = fut.result()
            turn_results[turn["id"]] = (verify_result, error)

    log(f"  concurrent phase: {time.monotonic() - llm_t0:.1f}s")

    # ── Sequential store + logging ─────────────────────────────────────
    for turn in turns:
        turn_id_val = turn["id"]
        turn_short = turn_id_val[:8]
        log(f"\n  [{turn_short}]")

        verify_result, error = turn_results.get(turn_id_val, (None, "missing batch result"))

        if error:
            log(f"    ERROR: {error}")
            failed += 1
            continue

        if verify_result is None:
            log(f"    No enrich data — skip")
            continue

        # Phase 1 log
        log_entity = verify_result.pop("_log_entity", None)
        if log_entity:
            n_files, n_missing_files, n_syms, n_missing_syms = log_entity
            log(f"    entity: {n_files} files ({n_missing_files} missing), "
                f"{n_syms} symbols ({n_missing_syms} missing)")

        # Phase 2 log
        log_faith = verify_result.pop("_log_faith", None)
        if log_faith:
            n_ent, n_fail, tldr_ok, reranker_overrides = log_faith
            r_log = f", {reranker_overrides} reranker override" if reranker_overrides else ""
            log(f"    faithfulness: {n_ent} entities ({n_fail} ungrounded), "
                f"tldr={'OK' if tldr_ok else 'LOW'}{r_log}")

        if dry_run:
            log(f"    [DRY] Would store verify_result")
            processed += 1
            continue

        # Store to DB
        fi_str = psql(
            f"SELECT COALESCE(MAX(fact_index), -1) + 1 "
            f"FROM review_facts WHERE turn_id = '{esc_sql(turn_id_val)}'::uuid"
        )
        fi = int(fi_str) if fi_str and fi_str != "-infinity" else 0

        verify_json = json.dumps(verify_result, ensure_ascii=False)
        faithfulness = verify_result.get("faithfulness", {})
        reranker_verdict = _compute_reranker_verdict(faithfulness)
        _insert_verify_result(turn_id_val, fi, verify_json, reranker_verdict)
        log(f"    Stored verify_result (fact_index={fi})")

        cat_summary = _build_category_summary(verify_result)
        if cat_summary:
            log(f"    {cat_summary}")

        processed += 1
        heartbeat("day_verify", f"turn {turn_id_val[:8]} verified")

    elapsed = round(time.monotonic() - t_start, 1)
    log(f"\n{'=' * 60}")
    log(f"Done: {processed} verified, {failed} failed ({elapsed}s)")
    log(f"{'=' * 60}")

    return {"ok": failed == 0, "processed": processed, "failed": failed,
            "elapsed_s": elapsed}


def main() -> None:
    preflight_checks("day_verify.py", required_ports={8082})
    import argparse
    parser = argparse.ArgumentParser(
        description="Day Verify — enrichment metadata quality check")
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    parser.add_argument("--turn-id", type=str, default=None,
                        help="Re-verify a specific turn UUID (skips NOT EXISTS filter)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate without DB writes")
    args = parser.parse_args()

    result = day_verify_pipeline(
        limit=args.limit,
        dry_run=args.dry_run,
        turn_id=args.turn_id,
    )
    if args.dry_run:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
