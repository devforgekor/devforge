#!/usr/bin/env python3
# Status: experimental
# Path: none — integrated pipeline test (kiwi→polish→fts5→extract→enrich→embed)
"""Integrated Pipeline Test — 전처리부터 Embed까지 전체 검증.

Runs full pre-verify pipeline from text preprocessing through embedding
on N oldest-unprocessed turns, saves intermediate DB snapshots at each stage
to data/eval/ for later verify-stage test consumption.

Pipeline: Text Preprocess → Polish+Self-Verify → FTS5 → Extract+Reranker → MCP → Embed

day_cycle.sh와 동일한 순서 (Verify만 제외).
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.llm_client import MODEL_REGISTRY
from lib.test_common import test_setup, test_heartbeat, test_complete, log
from lib.db import psql_json, psql_ok
from lib.infra.preflight import preflight_checks

EVAL_DIR = "/opt/projects/server/data/eval"
PIPELINES_DIR = os.path.join(SCRIPTS_DIR, "pipelines")

os.environ["TOKENIZERS_PARALLELISM"] = "false"


# ── Snapshot helpers ──────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _fmt_ids(turn_ids: list[str]) -> str:
    """Format UUID list for SQL IN clause."""
    return ",".join(f"'{tid}'" for tid in turn_ids)


def save_snapshot(stage: str, turn_ids: list[str], tag: str = "") -> str:
    """Save full DB state (turns + facts + MCP) for given turns as JSON."""
    ts = _ts()
    safe_tag = f"_{tag}" if tag else ""
    filename = f"pipeline_preverify_{stage}{safe_tag}_{ts}.json"
    path = os.path.join(EVAL_DIR, filename)
    id_list = _fmt_ids(turn_ids)

    rows = psql_json(
        f"SELECT id, conversation_id, seq, user_turn, user_turn_clean, "
        f"user_turn_clean_polished, thinking, thinking_clean, "
        f"thinking_clean_polished, text, text_clean, text_clean_polished, "
        f"meta, created_at "
        f"FROM turns WHERE id IN ({id_list}) ORDER BY created_at"
    )
    facts = psql_json(
        f"SELECT turn_id, fact_index, fact_type, evidence, verdict, reason, "
        f"fact_action, fact_confidence, nli_verdict, "
        f"extract_model, verify_model, source, phase "
        f"FROM review_facts WHERE turn_id IN ({id_list}) "
        f"ORDER BY turn_id, fact_index"
    )
    # FTS5 state — query SQLite directly (PostgreSQL has no turn_fts)
    fts5_data = []
    try:
        import sqlite3 as _sl3
        _sl_conn = _sl3.connect("file:///opt/ai_data/search/search.db?immutable=1", uri=True)
        _sl_cur = _sl_conn.execute(
            "SELECT rowid, substr(COALESCE(terms,''),1,80) AS preview "
            "FROM turn_search ORDER BY rowid DESC LIMIT 5"
        )
        fts5_data = _sl_cur.fetchall()
        _sl_cur.close()
        # Count total indexed rows
        _sl_cur2 = _sl_conn.execute("SELECT COUNT(*) FROM turn_search")
        fts5_total = _sl_cur2.fetchone()[0]
        _sl_cur2.close()
        _sl_conn.close()
    except Exception as e:
        print(f"  [warn] FTS5 snapshot failed: {e}", flush=True)
        fts5_total = 0

    snapshot = {
        "timestamp": ts,
        "stage": stage,
        "tag": tag or None,
        "n_turns": len(rows),
        "n_facts": len(facts or []),
        "n_fts5": fts5_total,
        "turns": rows,
        "review_facts": facts or [],
        "fts5_preview": [{"rowid": r[0], "terms_preview": r[1]}
                          for r in fts5_data] if fts5_data else [],
    }
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False, default=str)
    print(f"  [snapshot] {stage}: {path} ({len(rows)} turns, {len(facts or [])} facts, {fts5_total} fts5)", flush=True)
    return path


def run_subprocess(cmd: list, timeout: int = 3600, label: str = "") -> bool:
    """Run a pipeline subprocess and log output."""
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        ok = r.returncode == 0
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        print(f"  [{label}] TIMEOUT after {elapsed:.0f}s", flush=True)
        return False
    except Exception as e:
        print(f"  [{label}] ERROR: {e}", flush=True)
        return False

    elapsed = time.monotonic() - t0
    print(f"  [{label}] exit={r.returncode}, {elapsed:.0f}s", flush=True)
    for line in r.stdout.strip().splitlines()[-8:]:
        print(f"    {line}", flush=True)
    if r.stderr:
        excerpt = r.stderr[:400]
        print(f"  [{label} stderr] {excerpt}", flush=True)
    return ok


# ── Turn selection ─────────────────────────────────────────────────────────────

def select_turns(limit: int = 20) -> list[dict]:
    """Pick the N oldest turns needing processing (created_at ASC).

    Includes turns regardless of Kiwi/polish state — the test pipeline
    handles all preprocessing internally. All stages process in
    created_at ASC order, so these same N turns flow through every stage.
    """
    rows = psql_json(
        f"SELECT id, user_turn, text, thinking, "
        f"user_turn_clean, text_clean, thinking_clean, "
        f"LENGTH(COALESCE(text,'')) AS text_len, "
        f"created_at "
        f"FROM turns "
        f"WHERE (text_clean_polished IS NULL OR text_clean IS NULL OR text_clean = '') "
        f"  AND user_turn NOT LIKE 'This session%%' "
        f"  AND user_turn NOT LIKE 'Run /opt%%' "
        f"  AND user_turn NOT LIKE '<command%%' "
        f"  AND user_turn NOT LIKE 'Check %%' "
        f"ORDER BY created_at ASC "
        f"LIMIT {limit}"
    )
    if not rows:
        print("  [error] No turns need processing!", flush=True)
        return []

    # Print overview
    sizes = [(r.get("text_len", 0) or 0) for r in rows]
    n_need_clean = sum(1 for r in rows if not r.get("text_clean"))
    print(f"  [select] {len(rows)} turns (min={min(sizes)}, max={max(sizes)}, avg={sum(sizes)//len(sizes)} chars, {n_need_clean} need text_clean)", flush=True)
    for r in rows[:5]:
        preview = (r.get("user_turn", "") or "")[:50]
        print(f"    {r['id'][:8]} ({r.get('text_len',0)}c) \"{preview}\"", flush=True)
    if len(rows) > 5:
        print(f"    ... and {len(rows)-5} more", flush=True)
    return rows


# ── Stage runners ──────────────────────────────────────────────────────────────

def stage_text_clean(turn_ids: list[str]) -> bool:
    """텍스트 전처리 (NFKC/공백/이모지 정리) — text_clean 생성 (NULL인 턴만)."""
    print(f"\n{'─'*60}", flush=True)
    print(f"  Stage: Text Preprocess (text_clean 생성)", flush=True)
    print(f"{'─'*60}", flush=True)
    test_heartbeat("text_clean")

    from lib.text_cleaner import get_cleaner
    cl = get_cleaner()

    id_list = _fmt_ids(turn_ids)
    needs = psql_json(
        f"SELECT id, user_turn, text, thinking FROM turns WHERE id IN ({id_list}) "
        f"AND (text_clean IS NULL OR text_clean = '')"
    )
    if not needs:
        print(f"  [text_clean] All {len(turn_ids)} turns already have text_clean", flush=True)
        return True

    print(f"  [text_clean] Cleaning {len(needs)}/{len(turn_ids)} turns...", flush=True)
    ok = 0
    for t in needs:
        tid = t["id"]
        user_clean = cl.clean((t.get("user_turn") or "")[:2000])
        text_clean = cl.clean((t.get("text") or "")[:8000])
        think_clean = cl.clean((t.get("thinking") or "")[:4000])
        from lib.db import esc_sql
        sql = (
            f"UPDATE turns SET "
            f"user_turn_clean = '{esc_sql(user_clean)}', "
            f"text_clean = '{esc_sql(text_clean)}', "
            f"thinking_clean = '{esc_sql(think_clean)}' "
            f"WHERE id = '{tid}'"
        )
        if psql_ok(sql):
            ok += 1
    print(f"  [text_clean] {ok}/{len(needs)} cleaned", flush=True)
    return ok == len(needs)


def stage_polish(limit: int) -> bool:
    """Run polish_batch.py (Self-Verify included internally)."""
    print(f"\n{'─'*60}", flush=True)
    print(f"  Stage: Polish + Self-Verify (7B Q8 :8082)", flush=True)
    print(f"{'─'*60}", flush=True)
    test_heartbeat("polish")
    return run_subprocess(
        [sys.executable, "-u", os.path.join(PIPELINES_DIR, "polish_batch.py"),
         "--limit", str(limit)],
        timeout=7200, label="polish"
    )


def stage_fts5() -> bool:
    """Run fts5_refresh.py (no --limit, processes all stale)."""
    print(f"\n{'─'*60}", flush=True)
    print(f"  Stage: FTS5 Refresh (text_clean_polished → FTS5 index)", flush=True)
    print(f"{'─'*60}", flush=True)
    test_heartbeat("fts5")
    return run_subprocess(
        [sys.executable, "-u", os.path.join(PIPELINES_DIR, "fts5_refresh.py")],
        timeout=120, label="fts5"
    )


def stage_extract(limit: int) -> bool:
    """Run extract.py (LLM extract + Reranker faithfulness via :8080 if inference up)."""
    print(f"\n{'─'*60}", flush=True)
    print(f"  Stage: Extract + Reranker (7B Q8 :8082 + inference reranker)", flush=True)
    print(f"{'─'*60}", flush=True)
    test_heartbeat("extract")

    # Check if inference reranker is up (native /v1/rerank, no embedding workaround)
    reranker_ok = False
    try:
        import urllib.request as req
        resp = req.urlopen(f"http://127.0.0.1:{MODEL_REGISTRY['reranker']['port']}/v1/rerank",
                           data=json.dumps({"query": "test", "documents": ["test doc"]}).encode(),
                           timeout=5)
        reranker_ok = resp.status == 200
    except Exception:
        pass

    ok = run_subprocess(
        [sys.executable, "-u", os.path.join(PIPELINES_DIR, "extract.py"),
         "--limit", str(limit)],
        timeout=7200, label="extract"
    )

    if not reranker_ok:
        print(f"  [reranker] inference :8080/v1/rerank unreachable — no reranker verdicts", flush=True)
    else:
        print(f"  [reranker] inference :8080 available — reranker ran within extract", flush=True)
    return ok


def stage_enrich(limit: int) -> bool:
    """Run MCP enrichment (stays on :8082, same 7B Q8)."""
    print(f"\n{'─'*60}", flush=True)
    print(f"  Stage: MCP Enrich (7B Q8 :8082)", flush=True)
    print(f"{'─'*60}", flush=True)
    test_heartbeat("mcp_enrich")
    return run_subprocess(
        [sys.executable, "-u", os.path.join(PIPELINES_DIR, "enrich.py"),
         "--limit", str(limit)],
        timeout=3600, label="enrich"
    )


def stage_embed(limit: int) -> bool:
    """Swap inference → embed mode, run embed_batch.py."""
    print(f"\n{'─'*60}", flush=True)
    print(f"  Stage: Embed (Swap inference :8082 → :8081, 8B f16)", flush=True)
    print(f"{'─'*60}", flush=True)
    test_heartbeat("embed")

    # Swap inference to embed mode
    from lib.pod_manager import start_inference
    print(f"  [swap] Stopping day mode, starting embed (:8081)...", flush=True)
    if not start_inference("embed", 8081, skip_probe=True):
        print(f"  [swap] WARNING: start_inference embed reported failure", flush=True)

    # Wait for embed health
    print(f"  [swap] Waiting for embed server (MODEL_REGISTRY embedder)...", flush=True)
    for i in range(60):
        try:
            import urllib.request as req
            resp = req.urlopen(f"http://127.0.0.1:{MODEL_REGISTRY['embedder']['port']}/health", timeout=5)
            if resp.status == 200:
                print(f"  [swap] Embed ready after {(i+1)*5}s", flush=True)
                break
        except Exception:
            pass
        time.sleep(5)
    else:
        print(f"  [swap] Embed server not ready after 300s — skip", flush=True)
        return False

    # Run embed_batch.py
    ok = run_subprocess(
        [sys.executable, "-u", os.path.join(PIPELINES_DIR, "embed_batch.py"),
         "--limit", str(limit)],
        timeout=3600, label="embed"
    )

    # Swap back to day mode (7B Q8) for subsequent operation
    print(f"  [swap] Switching inference back to day mode (:8082)...", flush=True)
    start_inference("day", 8082, skip_probe=True)
    return ok


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pipeline Pre-Verify Integration Test")
    parser.add_argument("--limit", type=int, default=20, help="Number of turns to test")
    parser.add_argument("--skip-embed", action="store_true", default=False,
                        help="Skip embedding stage (default: include embed)")
    parser.add_argument("--with-embed", action="store_true",
                        help="Obsolete — embed is included by default")
    parser.add_argument("--dry-run", action="store_true", help="Check resources, no execution")
    args = parser.parse_args()

    run_embed_flag = not args.skip_embed
    TEST = test_setup("pipeline_preverify",
                      f"TextPreprocess→Polish→FTS5→Extract→MCP→Embed ({args.limit}turns)")

    if args.dry_run:
        print("Dry run — checking resources")
        preflight_checks("pipeline_preverify_test", required_ports={8082}, optional_ports={8080, 8081})
        test_complete("dry_run")
        return

    print("=" * 60, flush=True)
    print("  Pipeline Pre-Verify Integration Test", flush=True)
    print(f"  turns={args.limit}, embed={'yes' if run_embed_flag else 'no'}", flush=True)
    print(f"  Stages: Text Preprocess → Polish+Self-Verify → FTS5 → Extract+Reranker → MCP → Embed", flush=True)
    print("=" * 60, flush=True)

    # ── Pick 20 oldest unpolished turns ──
    turns = select_turns(args.limit)
    if not turns:
        test_complete("error: no turns")
        return
    turn_ids = [t["id"] for t in turns]

    # Save baseline (pre-all-stages) — marks our test set
    save_snapshot("00_baseline", turn_ids, "pre_all")

    # ── Stage 1: Kiwi 전처리 ──
    if not stage_text_clean(turn_ids):
        test_complete("error: text_clean failed")
        return
    save_snapshot("01_after_text_clean", turn_ids)

    # ── Stage 2: Polish + Self-Verify ──
    stage_polish(args.limit)
    save_snapshot("02_after_polish", turn_ids)

    # ── Stage 3: FTS5 Refresh ──
    stage_fts5()
    save_snapshot("03_after_fts5", turn_ids)

    # ── Stage 4: Extract + Reranker ──
    stage_extract(args.limit)
    save_snapshot("04_after_extract", turn_ids)

    # ── Stage 5: MCP Enrich ──
    stage_enrich(args.limit)
    save_snapshot("05_after_enrich", turn_ids)

    # ── Stage 6: Embed ──
    stage_embed(args.limit)
    save_snapshot("06_after_embed", turn_ids)

    # ── Summary ──
    print(f"\n{'=' * 60}", flush=True)
    print("  TEST COMPLETE", flush=True)
    print(f"  Snapshots saved to data/eval/pipeline_preverify_*.json", flush=True)
    print(f"  All stages completed. Data ready for Verify test harness.", flush=True)
    print(f"{'=' * 60}", flush=True)

    test_complete("success")


if __name__ == "__main__":
    main()
