#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.sh — Phase 3 (verify checkpoint-based)
"""Day Verify Pipeline — 14B verify + category on Pod B only.

Called at :30 by systemd timer. Reads extraction facts and MCP metadata
from DB, runs chunked LLM verification (reuses night.py Phase 2 logic),
stores verify_result in review_facts + pipeline_verify_*.json in eval/.

Usage:
  python3 scripts/pipelines/day_verify.py [--limit 50] [--dry-run]
"""

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "eval")
os.makedirs(EVAL_DIR, exist_ok=True)
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(os.path.join(SCRIPTS_DIR, "pipelines"))

from lib.db import psql, psql_ok, esc_sql, psql_json
from lib.llm_client import call_llm
from lib.infra.preflight import preflight_checks
from lib.token_budget import TokenBudget
from lib.common import log

BATCH_LIMIT = 50
MAX_BUDGET = 1500       # 25 minutes
BUFFER_MIN = 180        # 3 minutes
CHUNK_SIZE = 6          # match night.py Phase 2 chunk size
STARVATION_LIMIT = 3
VERIFY_TIMEOUT = 480    # reviewer model timeout
VERIFY_MAX_TOKENS = 1024
VERIFY_TEMP = 0.1
VERIFY_PARALLEL = 3     # match llama-server --parallel on :8083

# ── System Prompt (copied from night.py Phase 2) ──────────────────────

VERIFIER_SYSTEM_PROMPT = """You are a code review verifier. Examine all findings and decide for each:
- approved: correct, can proceed
- rejected: incorrect or not actionable
- needs_review: requires deeper analysis

Classify each finding into a category:
- bug: actual logic error or incorrect behavior
- security: vulnerability or unsafe pattern
- performance: efficiency or resource issue
- quality: maintainability, style, or documentation
- data_loss: missing or dropped information
- hallucination: extracted content NOT supported by the original conversation turn (made up, exaggerated, or contradictory)

IMPORTANT — Faithfulness check: Each finding has an "evidence" field extracted from the
conversation. Verify that the evidence actually appears in or is directly supported by the
turn. If the evidence is fabricated, exaggerated, or contradicts the turn context, mark
it as "hallucination" category with result "fail".

Output STRICT JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "confidence": 0-100,
  "summary": "1-sentence overall assessment",
  "reasoning": "2-3 sentence analysis",
  "verification_items": [
    {"check": "...", "result": "pass|fail|partial", "detail": "...", "category": "bug|security|performance|quality|data_loss|hallucination"}
  ]
}"""


# ── Findings Builder (DB → night.py Phase 2 compatible) ──────────────

def _get_turns_for_verify(limit: int = BATCH_LIMIT) -> List[Dict]:
    """Turns that completed extraction and MCP enrichment but still need verification."""
    sql = (
        "SELECT t.id, t.user_turn, t.thinking, t.text, "
        "       t.created_at::text "
        "FROM turns t "
        "WHERE EXISTS ("
        "  SELECT 1 FROM review_facts rf "
        "  WHERE rf.turn_id = t.id AND rf.fact_type IN ('text','user','thinking')"
        ")"
        "AND EXISTS ("
        "  SELECT 1 FROM review_facts rf "
        "  WHERE rf.turn_id = t.id AND rf.fact_type = 'mcp_meta'"
        ")"
        "AND NOT EXISTS ("
        "  SELECT 1 FROM review_facts rf "
        "  WHERE rf.turn_id = t.id AND rf.fact_type = 'verify_result'"
        ")"
        "ORDER BY t.created_at ASC "
        f"LIMIT {limit}"
    )
    return psql_json(sql) or []


def _get_turn_extractions(turn_id: str) -> List[Dict]:
    """Extraction facts are the raw input for verification — every finding must be checked."""
    sql = (
        "SELECT fact_type, evidence::text, fact_action, created_at::text "
        "FROM review_facts "
        f"WHERE turn_id = '{esc_sql(turn_id)}'::uuid "
        "AND fact_type IN ('text','user','thinking') "
        "ORDER BY fact_index ASC"
    )
    return psql_json(sql) or []


def _get_turn_mcp(turn_id: str) -> Optional[Dict]:
    """MCP metadata provides entity/tag context so verify can cross-check extraction claims."""
    sql = (
        "SELECT evidence::text FROM review_facts "
        f"WHERE turn_id = '{esc_sql(turn_id)}'::uuid "
        "AND fact_type = 'mcp_meta' "
        "ORDER BY fact_index DESC LIMIT 1"
    )
    rows = psql_json(sql) or []
    if not rows:
        return None
    try:
        return json.loads(rows[0]["evidence"])
    except (json.JSONDecodeError, KeyError):
        return None


def _build_findings_from_turn(turn: Dict) -> List[Dict]:
    """Build night.py-compatible findings list from DB extraction + MCP data.

    Each extraction fact becomes a 'finding' with id, description, evidence.
    MCP fields (tldr, entities, tags) become additional findings for quality check.
    """
    tid = turn["id"]
    findings = []

    # Load extractions
    extractions = _get_turn_extractions(tid)
    for idx, ex in enumerate(extractions):
        evidence = ex.get("evidence", "")[:500]
        if not evidence or evidence == "null":
            continue
        findings.append({
            "id": f"EX-{tid[:8]}-{idx}",
            "severity": "medium",
            "category": "quality",
            "description": f"Extracted {ex.get('fact_type','?')} content",
            "evidence": evidence,
            "source": ex.get("fact_action", "extract"),
            "_turn_id": tid,
        })

    # Load MCP fields for additional context
    mcp = _get_turn_mcp(tid)
    if mcp:
        tldr = mcp.get("tldr", "") or ""
        if tldr:
            findings.append({
                "id": f"MCP-{tid[:8]}-tldr",
                "severity": "medium",
                "category": "quality",
                "description": "MCP tldr summary",
                "evidence": tldr[:300],
                "_turn_id": tid,
            })
        entities = mcp.get("entities", {}) or {}
        tags = mcp.get("tags", []) or []
        if entities:
            findings.append({
                "id": f"MCP-{tid[:8]}-ent",
                "severity": "info",
                "category": "quality",
                "description": f"MCP entities: "
                               f"{len(entities.get('files',[]))} files, "
                               f"{len(entities.get('functions',[]))} funcs",
                "evidence": json.dumps(entities, ensure_ascii=False)[:300],
                "_turn_id": tid,
            })
        if tags:
            findings.append({
                "id": f"MCP-{tid[:8]}-tags",
                "severity": "info",
                "category": "quality",
                "description": f"MCP tags: {', '.join(tags[:5])}",
                "evidence": json.dumps(tags, ensure_ascii=False)[:300],
                "_turn_id": tid,
            })

    # Add turn context for the reviewer (user + response text for faithfulness check)
    user_turn = (turn.get("user_turn") or "")[:200]
    if user_turn:
        findings.insert(0, {
            "id": f"CTX-{tid[:8]}-user",
            "severity": "info",
            "category": "quality",
            "description": f"User turn context",
            "evidence": user_turn[:200],
            "_turn_id": tid,
        })
    turn_text = (turn.get("text") or "")[:500]
    if turn_text:
        findings.insert(0, {
            "id": f"CTX-{tid[:8]}-text",
            "severity": "info",
            "category": "quality",
            "description": "Assistant response context (for faithfulness comparison)",
            "evidence": turn_text[:500],
            "_turn_id": tid,
        })

    return findings


def _findings_to_context(findings: List[Dict]) -> str:
    """TokenBudget-constrained findings context (matches night.py style)."""
    budget = TokenBudget("day_verify")
    parts = []

    def _add(text: str, priority: int = 5) -> bool:
        ok = budget.add_section(text.split("\n")[0][:60], text, priority)
        if ok:
            parts.append(text)
        return ok

    _add(f"## Findings ({len(findings)} total)\n", priority=10)

    for sev_name, pri in (("critical", 9), ("high", 7), ("medium", 5), ("low", 3), ("info", 2)):
        subset = [it for it in findings if it.get("severity", "medium").lower() == sev_name]
        if not subset:
            continue
        txt = f"\n[{sev_name.upper()}] ({len(subset)}):"
        for it in subset:
            txt += f"\n  {it.get('id', '?')}: {json.dumps(it, ensure_ascii=False)[:200]}"
        if not _add(txt, priority=pri):
            _add(f"\n[{sev_name.upper()}] ({len(subset)} total — omitted, budget)", priority=pri - 1)

    return "\n".join(parts)


# ── DB Writer ─────────────────────────────────────────────────────────

def _insert_verify_result(turn_id: str, fact_index: int,
                          verify_json_str: str, model_label: str,
                          category_summary: str = "",
                          prompt_tokens: Optional[int] = None,
                          gen_tokens: Optional[int] = None,
                          elapsed_ms: Optional[float] = None,
                          source_file: Optional[str] = None) -> bool:
    """Persist verification outcome so completed turns are excluded from future batches."""
    cols = ["turn_id", "fact_index", "fact_type", "evidence",
            "extract_model", "verdict", "source", "fact_action"]
    vals = [
        f"'{esc_sql(turn_id)}'::uuid",
        str(fact_index),
        "'verify_result'",
        f"'{esc_sql(verify_json_str[:5000])}'",
        f"'{esc_sql(model_label)}'",
        "'pending'",
        f"'day_verify_{esc_sql(model_label)}'",
        "'verify'",
    ]
    set_clauses = []

    if prompt_tokens is not None:
        cols.extend(["prompt_tokens", "gen_tokens"])
        vals.extend([str(prompt_tokens), str(gen_tokens)])
        set_clauses.append(f"prompt_tokens = {prompt_tokens}")
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


def _save_verify_output(data: Dict) -> str:
    """Save verification result to eval/ as pipeline_verify_*.json."""
    file_timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = f"pipeline_verify_{file_timestamp_str}.json"
    fpath = os.path.join(EVAL_DIR, fname)
    with open(fpath, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"  [save] {fpath}")
    return fpath


def _build_category_summary(verification_items: List[Dict], total_findings: int) -> str:
    """Aggregate categories into a one-line summary (matches night.py)."""
    if not verification_items:
        return ""
    cats: Dict[str, Dict[str, int]] = {}
    cats_result: Dict[str, Dict[str, int]] = {}
    verdict_counts: Dict[str, int] = {}
    for item in verification_items:
        cat = item.get("category", "other")
        sev = item.get("severity", item.get("result", "medium"))
        if cat not in cats:
            cats[cat] = {}
        cats[cat][sev] = cats[cat].get(sev, 0) + 1
        v = item.get("result", "unknown")
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
        if cat not in cats_result:
            cats_result[cat] = {}
        cats_result[cat][v] = cats_result[cat].get(v, 0) + 1

    cat_parts = []
    for cat, sevs in sorted(cats.items(), key=lambda x: -sum(x[1].values())):
        sev_parts = [f"{s}={c}" for s, c in sorted(sevs.items())]
        total = sum(sevs.values())
        res_parts = cats_result.get(cat, {})
        res_str = f" [{','.join(f'{r}={c}' for r,c in sorted(res_parts.items()))}]" if res_parts else ""
        cat_parts.append(f"{cat}={total}({','.join(sev_parts)}){res_str}")
    verdict_str = ", ".join(f"{k}={v}" for k, v in sorted(verdict_counts.items()))
    return f"Category Distribution: {'; '.join(cat_parts)} ({total_findings} total) | Verdict: {verdict_str}"


# ── Concurrent Chunk Helper ──────────────────────────────────────────────

def _verify_chunk(chunk: List[Dict], ci: int, total: int) -> Dict[str, Any]:
    """Single chunk LLM call + JSON parse. Thread-safe (no shared state)."""
    ctx = _findings_to_context(chunk)
    log(f"  [chunk {ci}/{total}] {len(chunk)} items")

    resp = call_llm(
        [{"role": "system", "content": VERIFIER_SYSTEM_PROMPT},
         {"role": "user", "content": ctx}],
        model="reviewer",
        max_tokens=VERIFY_MAX_TOKENS, temperature=VERIFY_TEMP,
        timeout=VERIFY_TIMEOUT, json_mode=True, return_meta=True,
    )
    r_content = resp["content"]
    if isinstance(r_content, str):
        m = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', r_content)
        if m:
            r_content = m.group(1)
        try:
            r = json.loads(r_content.strip())
        except json.JSONDecodeError:
            r = {}
    else:
        r = r_content

    return {
        "ok": True,
        "items": r.get("verification_items", []),
        "verdict": r.get("final_verdict"),
        "summary": r.get("summary", ""),
        "reasoning": r.get("reasoning", ""),
        "usage": resp.get("usage", {}),
        "elapsed_ms": resp.get("elapsed_ms", 0),
        "chunk_size": len(chunk),
    }


# ── Pipeline ──────────────────────────────────────────────────────────

def day_verify_pipeline(limit: int = BATCH_LIMIT,
                        dry_run: bool = False,
                        model_label: str = "14b") -> Dict[str, Any]:
    """Main entry: load unverified turns, verify each finding against MCP context, persist results."""
    t_start = time.monotonic()
    consecutive_defer = 0

    processed_total = 0
    failed_total = 0

    log("=" * 60)
    log(f"DevForge Day Verify — verification on Pod B (:8083)")
    log(f"  Label: {model_label}")
    if dry_run:
        log("  [DRY RUN] No writes to DB")
    log("=" * 60)

    while True:
        elapsed = time.monotonic() - t_start
        remaining = MAX_BUDGET - elapsed

        # Load turns needing verification
        turns = _get_turns_for_verify(limit)
        if not turns:
            log(f"[done] No turns needing verification ({elapsed:.0f}s)")
            break

        if remaining < BUFFER_MIN:
            log(f"[buffer] Remaining {remaining:.0f}s < {BUFFER_MIN}s — deferring {len(turns)} turns")
            consecutive_defer += 1
            if consecutive_defer >= STARVATION_LIMIT:
                log(f"[alert] Verify backlog: {len(turns)} turns, {consecutive_defer}x defer")
            break

        consecutive_defer = 0
        log(f"\n=== Batch: verify {len(turns)} turns (budget={remaining:.0f}s) ===")

        # Build findings from DB
        all_findings = []
        for turn in turns:
            findings = _build_findings_from_turn(turn)
            all_findings.extend(findings)

        if not all_findings:
            log("  [skip] No findings to verify")
            break

        log(f"  [input] {len(all_findings)} findings from {len(turns)} turns")

        # Concurrent chunk verification (continuous batching on :8083)
        chunks = [all_findings[i:i+CHUNK_SIZE] for i in range(0, len(all_findings), CHUNK_SIZE)]
        merged: Dict[str, Any] = {"verification_items": [], "summary": "", "reasoning": ""}
        all_usage: Dict[str, Any] = {}
        total_elapsed = 0
        verdicts: List[str] = []
        processed = 0
        failed = 0

        workers = min(VERIFY_PARALLEL, len(chunks))
        log(f"  [verify] {len(chunks)} chunks → {workers} concurrent")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {}
            for ci, chunk in enumerate(chunks):
                future = executor.submit(_verify_chunk, chunk, ci + 1, len(chunks))
                future_map[future] = ci
            for future in as_completed(future_map):
                ci = future_map[future]
                try:
                    result = future.result()
                except Exception as e:
                    log(f"    ERROR chunk {ci+1}: {type(e).__name__}: {e}")
                    failed += len(chunks[ci])
                    continue
                merged["verification_items"].extend(result["items"])
                if result["verdict"]:
                    verdicts.append(result["verdict"])
                if result["summary"]:
                    merged["summary"] = (merged.get("summary", "") + " | " + result["summary"])[:500]
                if result["reasoning"]:
                    merged["reasoning"] = (merged.get("reasoning", "") + "\n" + result["reasoning"])[:1000]
                total_elapsed += result["elapsed_ms"]
                for k, v in result["usage"].items():
                    all_usage[k] = all_usage.get(k, 0) + (v if isinstance(v, int) else 0)
                processed += result["chunk_size"]

        # Build final verdict
        if verdicts:
            counts = {}
            for v in verdicts:
                counts[v] = counts.get(v, 0) + 1
            merged["final_verdict"] = max(counts, key=counts.get)
        else:
            merged["final_verdict"] = "unknown"
        merged["confidence"] = int(len(verdicts) / max(len(chunks), 1) * 90)

        # Build category summary
        cat_summary = _build_category_summary(merged.get("verification_items", []), len(all_findings))

        # Store to DB (per-turn verify_result)
        turn_set = set(f["_turn_id"] for f in all_findings)
        for tid in turn_set:
            fi_sql = (
                f"SELECT COALESCE(MAX(fact_index), -1) + 1 "
                f"FROM review_facts WHERE turn_id = '{esc_sql(tid)}'::uuid"
            )
            fi_str = psql(fi_sql)
            fi = int(fi_str) if fi_str and fi_str != "-infinity" else 0
            if not dry_run:
                _insert_verify_result(
                    tid, fi,
                    json.dumps(merged, ensure_ascii=False),
                    model_label,
                    category_summary=cat_summary,
                    prompt_tokens=all_usage.get("prompt_tokens"),
                    gen_tokens=all_usage.get("completion_tokens"),
                    elapsed_ms=total_elapsed,
                )

        # Save to eval/ (once per batch)
        result = {
            "phase": "day_verify", "role": f"verify_{model_label}",
            "model": model_label, "port": 8083,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "usage": all_usage, "elapsed_ms": total_elapsed,
            "turns": len(turn_set), "findings": len(all_findings),
            "chunks": len(chunks), "chunks_done": len(verdicts),
            "result": merged,
            "category_summary": cat_summary,
        }
        if not dry_run:
            _save_verify_output(result)

        processed_total += processed
        failed_total += failed

        # Check if budget allows another batch
        log(f"  [batch] processed={processed}, failed={failed}, verdict={merged.get('final_verdict','?')}")
        if cat_summary:
            log(f"  [cat] {cat_summary}")

        if (time.monotonic() - t_start) > (MAX_BUDGET - BUFFER_MIN):
            log(f"[budget] Exceeded max budget")
            break

    total = round(time.monotonic() - t_start, 1)
    log(f"\n{'=' * 60}")
    log(f"Day Verify complete: {processed_total} verified, {failed_total} failed ({total}s)")
    log(f"{'=' * 60}")

    return {"processed": processed_total, "failed": failed_total, "elapsed_s": total}


def main() -> None:
    preflight_checks("day_verify.py", required_ports={8083})
    import argparse
    parser = argparse.ArgumentParser(description="Day Verify — 14B verify + category")
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model", default="14b", help="Model label for output")
    args = parser.parse_args()

    day_verify_pipeline(limit=args.limit, dry_run=args.dry_run, model_label=args.model)
    sys.exit(0)


if __name__ == "__main__":
    main()
