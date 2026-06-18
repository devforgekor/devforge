#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.py — Phase 3 (verify chain)
"""Day Verify Pipeline — verify enrichment metadata quality.

Reads enrich_meta from DB and runs three verification phases:
  Phase 1: Entity disk/symbol verify (files exist? symbols found?)
  Phase 2: Reranker faithfulness (entity + tldr grounding in source text)
  Phase 2b: 7B NLI self-verify (second opinion on uncertain entities)

Called by day_cycle.py after extract → enrich completes.
Stores results as fact_type='verify_result', separate from enrich_meta.

Usage:
  python3 scripts/pipelines/day_verify.py              # batch verify
  python3 scripts/pipelines/day_verify.py --limit 10   # batch cap
  python3 scripts/pipelines/day_verify.py --dry-run    # simulate, no writes
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "eval")
os.makedirs(EVAL_DIR, exist_ok=True)
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(os.path.join(SCRIPTS_DIR, "pipelines"))

from lib.db import psql, psql_ok, esc_sql, psql_json
from lib.llm_client import call_llm, reranker_score, reranker_nli_verdict
from lib.enrich.utils import verify_entities
from lib.infra.preflight import preflight_checks

BATCH_LIMIT = 20


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ── Reranker faithfulness: entity grounding in source text ──────────────

def _check_substring(entity: str, source: str) -> bool:
    """Fast substring check: normalized entity in normalized source."""
    if not entity or not source:
        return False
    return entity.lower().strip() in source.lower()


def _check_faithfulness(enrich_data: Dict, user_turn: str,
                         thinking: str, text: str) -> Dict:
    """Verify entity + tldr faithfulness via reranker + 7B NLI.

    For each entity type (files, technologies, functions, mentioned_users),
    checks substring presence first (fast path), then reranker score.
    Returns faithfulness dict with _source attached for NLI phase.
    """
    entities = enrich_data.get("entities", {}) or {}
    source_text = " ".join(f"{user_turn}\n{thinking}\n{text}".split())[:4000]

    faithfulness: Dict[str, list] = {}
    for key in ("files", "technologies", "functions", "mentioned_users"):
        items = entities.get(key, [])
        if not isinstance(items, list):
            items = []
        checked = []
        for item in items:
            s = str(item).strip()
            if not s:
                continue
            entry: Dict = {"entity": s, "_source": source_text}
            # Fast path: substring match
            if _check_substring(s, source_text):
                entry.update({
                    "score": 100.0, "grounding": "GROUNDED",
                    "method": "substr", "grounded": True,
                })
                checked.append(entry)
                continue
            # Slow path: reranker
            cos = reranker_score(s, source_text)
            score = round(cos * 100, 1)
            nli_v = reranker_nli_verdict(cos)
            entry.update({
                "score": score, "grounding": nli_v,
                "method": "reranker", "grounded": nli_v != "UNGROUNDED",
            })
            checked.append(entry)
        faithfulness[key] = checked

    # tldr faithfulness
    tldr = (enrich_data.get("tldr", "") or "").strip()
    if tldr:
        cos = reranker_score(tldr, source_text)
        score = round(cos * 100, 1)
        nli_v = reranker_nli_verdict(cos)
        faithfulness["tldr"] = {
            "text": tldr, "_source": source_text,
            "score": score, "grounding": nli_v,
            "grounded": nli_v != "UNGROUNDED",
        }

    return faithfulness


# ── Phase 2b: 7B NLI Self-Verify ──────────────────────────────────

_NLI_VERIFY_PROMPT = """You are verifying whether an EVIDENCE sentence is factually supported by a SOURCE sentence.

Follow these steps:
1. Identify the key factual claim in the evidence.
2. Check whether that claim is directly stated or clearly implied by the source.
3. Output exactly one label.

LABELS:
- ENTAILMENT: The evidence is directly supported by the source.
- CONTRADICTION: The evidence contradicts the source — they cannot both be true.
- NEUTRAL: The evidence is not directly supported but does not contradict either.

SOURCE: {source}

EVIDENCE: {evidence}

LABEL:"""


def _llm_nli_check(entity: str, source: str) -> str:
    """Run 7B Q8 NLI self-verify on a single entity-source pair.
    Returns ENTAILMENT, CONTRADICTION, or NEUTRAL.
    Falls back to NEUTRAL on any error.
    """
    if not entity or not source:
        return "NEUTRAL"
    prompt = _NLI_VERIFY_PROMPT.format(
        source=source[:2000], evidence=entity[:500]
    )
    try:
        meta = call_llm(
            [{"role": "user", "content": prompt}],
            model="day_enrich",
            max_tokens=64, temperature=0.0, timeout=30,
            return_meta=True,
        )
        raw = meta["content"].strip().upper()
        for tok in raw.replace("\n", " ").split():
            tok = tok.strip(".,!?;:\"'()[]")
            if tok in ("ENTAILMENT", "CONTRADICTION", "NEUTRAL"):
                return tok
        return "NEUTRAL"
    except Exception:
        return "NEUTRAL"


def _llm_nli_verify(faithfulness: Dict) -> Dict:
    """Phase 2b: 7B NLI self-verify on reranker-uncertain entities.

    For entities that the reranker scored as UNGROUNDED/AMBIGUOUS,
    run 7B Q8 NLI as second opinion. If NLI says ENTAILMENT,
    override to GROUNDED. If CONTRADICTION, keep UNGROUNDED.

    Mutates faithfulness dict in-place and returns it.
    """
    for key in ("files", "technologies", "functions", "mentioned_users"):
        items = faithfulness.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            # Only re-check entities that reranker couldn't confidently ground
            if item.get("grounded", True) or item.get("score", 100) >= 80:
                continue
            entity = item.get("entity", "")
            source = item.get("_source", "")
            if not source:
                continue
            nli = _llm_nli_check(entity, source)

            # ENTAILMENT from 7B overrides reranker UNGROUNDED
            if nli == "ENTAILMENT":
                item["grounded"] = True
                item["grounding"] = "GROUNDED"
                item["method"] = "7b_nli_override"
                item["_nli"] = nli
            elif nli == "CONTRADICTION":
                item["grounded"] = False
                item["grounding"] = "CONTRADICTION"
                item["method"] = "7b_nli"
                item["_nli"] = nli
            else:
                item["_nli"] = nli

    # Also check tldr
    tldr = faithfulness.get("tldr", {})
    if isinstance(tldr, dict) and not tldr.get("grounded", True):
        entity = tldr.get("text", "")
        source = tldr.get("_source", "")
        if source:
            nli = _llm_nli_check(entity, source)
            if nli == "ENTAILMENT":
                tldr["grounded"] = True
                tldr["grounding"] = "GROUNDED"
                tldr["method"] = "7b_nli_override"
                tldr["_nli"] = nli

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


def _insert_verify_result(turn_id: str, fact_index: int,
                           verify_json_str: str) -> bool:
    sql = (
        "INSERT INTO review_facts "
        "  (turn_id, fact_index, fact_type, evidence, "
        "   extract_model, verdict, source, fact_action) "
        f"VALUES ('{esc_sql(turn_id)}'::uuid, {fact_index}, "
        f"  'verify_result', "
        f"  '{esc_sql(verify_json_str[:5000])}', "
        f"  'enrich-self', 'pending', "
        f"  'day_verify', 'verify')"
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

def day_verify_pipeline(limit: int = BATCH_LIMIT,
                         dry_run: bool = False,
                         turn_id: Optional[str] = None) -> Dict[str, Any]:
    """Verify enrichment metadata: entity disk check + reranker + 7B NLI."""
    t_start = time.monotonic()
    processed = 0
    failed = 0

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

    log(f"Processing {len(turns)} turn(s)")

    for turn in turns:
        turn_id = turn["id"]
        turn_short = turn_id[:8]
        log(f"\n  [{turn_short}]")

        try:
            enrich_meta_str = turn.get("enrich_meta", "")
            enrich_data = json.loads(enrich_meta_str) if enrich_meta_str else {}

            if not enrich_data:
                log(f"    No enrich data — skip")
                continue

            verify_result: Dict[str, Any] = {}

            # Phase 1: Entity disk/symbol verify
            entities = enrich_data.get("entities", {})
            if entities:
                ev = verify_entities(enrich_data)
                verify_result["entity_verify"] = ev
                n_files = len(ev.get("files", []))
                n_syms = len(ev.get("symbols", []))
                n_missing_files = sum(1 for f in ev.get("files", []) if not f["exists"])
                n_missing_syms = sum(1 for s in ev.get("symbols", []) if not s["found"])
                log(f"    entity: {n_files} files ({n_missing_files} missing), "
                    f"{n_syms} symbols ({n_missing_syms} missing)")

            # Phase 2: Reranker faithfulness
            user_turn = turn.get("user_turn", "") or ""
            thinking = turn.get("thinking", "") or ""
            text = turn.get("text", "") or ""
            faithfulness = _check_faithfulness(enrich_data, user_turn, thinking, text)
            verify_result["faithfulness"] = faithfulness

            # Phase 2b: 7B NLI self-verify on uncertain entities
            pre_nli_ungrounded = sum(
                1 for v in faithfulness.values()
                if isinstance(v, list) and any(not e.get("grounded", True) for e in v)
            )
            if pre_nli_ungrounded > 0:
                _llm_nli_verify(faithfulness)
                nli_overrides = pre_nli_ungrounded - sum(
                    1 for v in faithfulness.values()
                    if isinstance(v, list) and any(not e.get("grounded", True) for e in v)
                )
            else:
                nli_overrides = 0

            # Strip _source before storage (bulky, runtime-only)
            for key in ("files", "technologies", "functions", "mentioned_users"):
                items = faithfulness.get(key, [])
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            item.pop("_source", None)
            tldr = faithfulness.get("tldr", {})
            if isinstance(tldr, dict):
                tldr.pop("_source", None)

            if faithfulness:
                n_ent = sum(len(v) for v in faithfulness.values() if isinstance(v, list))
                n_fail = sum(
                    1 for v in faithfulness.values()
                    if isinstance(v, list) and any(not e.get("grounded", True) for e in v)
                )
                tldr_ok = faithfulness.get("tldr", {}).get("grounded", True) if isinstance(faithfulness.get("tldr"), dict) else True
                nli_log = f", {nli_overrides} NLI override" if nli_overrides else ""
                log(f"    faithfulness: {n_ent} entities ({n_fail} ungrounded), "
                    f"tldr={'OK' if tldr_ok else 'LOW'}{nli_log}")

            if dry_run:
                log(f"    [DRY] Would store verify_result")
                processed += 1
                continue

            # Store to DB
            fi_str = psql(
                f"SELECT COALESCE(MAX(fact_index), -1) + 1 "
                f"FROM review_facts WHERE turn_id = '{esc_sql(turn_id)}'::uuid"
            )
            fi = int(fi_str) if fi_str and fi_str != "-infinity" else 0

            verify_json = json.dumps(verify_result, ensure_ascii=False)
            _insert_verify_result(turn_id, fi, verify_json)
            log(f"    Stored verify_result (fact_index={fi})")

            cat_summary = _build_category_summary(verify_result)
            if cat_summary:
                log(f"    {cat_summary}")

            processed += 1

        except Exception as e:
            log(f"    ERROR: {type(e).__name__}: {e}")
            failed += 1

    elapsed = round(time.monotonic() - t_start, 1)
    log(f"\n{'=' * 60}")
    log(f"Done: {processed} verified, {failed} failed ({elapsed}s)")
    log(f"{'=' * 60}")

    return {"ok": failed == 0, "processed": processed, "failed": failed,
            "elapsed_s": elapsed}


def main() -> None:
    preflight_checks("day_verify.py", required_ports={8080})
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
