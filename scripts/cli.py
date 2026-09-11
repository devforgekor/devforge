#!/usr/bin/env python3
# Status: production
# Path: manual — CLI entry
"""DevForge CLI — AI 대화 검색 및 저장 도구."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

from lib.cli_experiment import (
    cmd_experiment_active,
    cmd_experiment_adopt,
    cmd_experiment_compare,
    cmd_experiment_list,
)
from lib.cli_fact import cmd_fact_confirm, cmd_fact_list, cmd_fact_reject
from lib.cli_helpers import (
    _get_active_config,
    _get_alerts,
    _get_containers,
    _get_experiments,
    _get_glossary,
    _get_models,
    _get_references,
    _get_resources,
    _get_rule_status,
    _get_services,
    _get_tasks,
    _get_timers,
)
from lib.cli_watch import (
    cmd_watch_alerts,
    cmd_watch_incident_show,
    cmd_watch_incidents,
    cmd_watch_log,
    cmd_watch_pulse_create,
    cmd_watch_pulse_resolve,
    cmd_watch_pulse_show,
    cmd_watch_pulses_list,
    cmd_watch_status,
)
from lib.cli_worklog import cmd_worklog_add, cmd_worklog_recent, cmd_worklog_search
from lib.db import esc_sql
from lib.db import psql as _sql
from lib.dev_pipeline import claim_issue, create_pr, poll_issues
from lib.llm_client import MODEL_REGISTRY
from lib.reflex_rules import (
    rule_create,
    rule_decay_all,
    rule_detect_patterns,
    rule_get,
    rule_match,
    rule_promote_all,
    rule_report,
    rule_search,
    rule_update,
)


def _format_results(rows):
    for r in rows:
        print(f"--- [{r['source']}] {r['title'] or '(no title)'} ---")
        print(f"  conversation: {r['conversation_id']}")
        print(f"  model: {r['model']}  seq: {r['seq']}  created: {r['created_at']}")
        ut = (r["user_turn"] or "")[:120]
        tx = (r["text"] or "")[:120]
        print(f"  user: {ut}")
        print(f"  text: {tx}")
        print()


async def cmd_search(args):
    from api.search import search_memories

    results = await search_memories(query=args.query, source=args.source, limit=args.limit)
    print(f"Found {len(results)} results for '{args.query}'\n")
    _format_results(results)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))


async def cmd_save(args):
    from api.search import save_memory

    detail = {}
    if args.detail:
        try:
            detail = json.loads(args.detail)
        except json.JSONDecodeError:
            detail = {"user_turn": args.detail, "text": args.summary or ""}

    result = await save_memory(
        source=args.source,
        user_turn=detail.get("user_turn", args.detail or ""),
        text=detail.get("text", args.summary or ""),
        title=args.summary,
        model=args.model,
        thinking=detail.get("thinking"),
        meta=detail.get("meta", {}),
    )
    print(json.dumps(result, indent=2))


async def cmd_recent(args):
    from api.search import search_memories

    results = await search_memories(query="", source=args.source, limit=args.limit)
    print(f"Recent {len(results)} entries:\n")
    _format_results(results)


def cmd_activity_recent(args):
    """Show recent activity_log entries."""
    limit = getattr(args, "limit", 10)
    today = getattr(args, "today", False)
    where = "WHERE created_at::date = CURRENT_DATE" if today else ""
    sql = f"SELECT id, created_at, type, source, title, summary_status, queue_status FROM activity_log {where} ORDER BY created_at DESC LIMIT {limit}"
    rows = _sql(sql).split("\n") if _sql(sql) else []
    print(
        f"{'ID':<6} {'Created':<20} {'Type':<10} {'Source':<14} {'Title':<50} {'Sum.Status':<12} {'Q.Status'}"
    )
    print("-" * 130)
    for row in rows:
        if not row.strip():
            continue
        parts = row.split("|", 6)
        if len(parts) >= 7:
            print(
                f"{parts[0]:<6} {parts[1]:<20} {parts[2]:<10} {parts[3]:<14} {parts[4][:48]:<50} {parts[5]:<12} {parts[6]}"
            )


def cmd_activity_stats(args):
    """Show activity_log counts by type and status."""
    sql = """SELECT type, summary_status, count(*) FROM activity_log
             WHERE created_at > NOW() - INTERVAL '7 days'
             GROUP BY type, summary_status ORDER BY type, summary_status"""
    rows = _sql(sql).split("\n") if _sql(sql) else []
    print(f"{'Type':<12} {'Status':<14} {'Count'}")
    print("-" * 40)
    for row in rows:
        if not row.strip():
            continue
        parts = row.split("|")
        if len(parts) >= 3:
            print(f"{parts[0]:<12} {parts[1]:<14} {parts[2]}")


def cmd_activity_add(args):
    """Insert a manual entry into activity_log."""
    title = esc_sql(args.title)
    summary = esc_sql(getattr(args, "summary", ""))
    tags = getattr(args, "tags", "")
    tags_sql = (
        "ARRAY[" + ",".join(f"'{esc_sql(t.strip())}'" for t in tags.split(",") if t.strip()) + "]"
        if tags
        else "'{}'"
    )
    result = _sql(f"""INSERT INTO activity_log (type, source, title, summary, tags, summary_status)
        VALUES ('manual', 'cli', '{title}', '{summary}', {tags_sql}, 'raw') RETURNING id""")
    if result and result.strip():
        print(f"  Added: {title} (id={result.strip()})")
    else:
        print("  Failed to add entry")


def cmd_obs_search(args):
    """Search observations using unified API."""
    import json as _json

    from lib.observation import obs_search as _search

    tags_obj = None
    if getattr(args, "tags", None):
        try:
            tags_obj = json.loads(args.tags)
        except json.JSONDecodeError:
            pass
    limit = max(1, min(getattr(args, "limit", 10), 100))

    rows = _search(
        category=getattr(args, "category", None) or None,
        source=getattr(args, "source", None) or None,
        tags=tags_obj,
        query=getattr(args, "query", None) or None,
        limit=limit,
    )
    if not rows:
        print("  (no observations)")
        return

    for r in rows:
        utc_timestamp = (r.get("created_at") or "")[:19]
        cat = r.get("category", "") or ""
        obs = (r.get("observation") or "")[:120]
        ctx = r.get("context") or "{}"
        try:
            ctx_obj = _json.loads(ctx) if isinstance(ctx, str) else ctx
        except (json.JSONDecodeError, TypeError):
            ctx_obj = {}
        tool = ctx_obj.get("tool") or ctx_obj.get("file_path") or ""
        extra = f" [{tool}]" if tool else ""
        tags_str = ""
        tag_obj = r.get("tags") or {}
        if isinstance(tag_obj, dict) and tag_obj:
            flat = []
            for k, v in tag_obj.items():
                if isinstance(v, list):
                    flat.extend(f"{k}={item}" for item in v[:2])
            if flat:
                tags_str = f" ({', '.join(flat)})"
        print(f"  [{utc_timestamp}] {cat:14s} {obs}{extra}{tags_str}")

    if getattr(args, "json", False):
        print(_json.dumps(rows, ensure_ascii=False, indent=2, default=str))


def cmd_obs_add(args):
    """Write an observation via CLI."""
    from lib.observation import observe as _observe

    tags_obj = None
    if getattr(args, "tags", None):
        try:
            tags_obj = json.loads(args.tags)
        except json.JSONDecodeError:
            pass
    oid = _observe(
        args.observation,
        category=getattr(args, "category", "general"),
        source=getattr(args, "source", "cli:obs"),
        tags=tags_obj,
    )
    if oid:
        print(f"  Observation recorded: {oid}")
    else:
        print("  Failed to record observation")


def cmd_obs_stats(args):
    """Show observation category stats."""
    from lib.observation import obs_stats as _stats

    days = getattr(args, "days", 7)
    stats = _stats(days=days)
    if not stats:
        print(f"  (no observations in {days}d)")
        return
    total = sum(int(s["count"]) for s in stats)
    print(f"  Observations ({days}d): {total} total")
    for s in stats:
        pct = int(s["count"]) / total * 100 if total else 0
        bar = "#" * int(pct / 5) + "·" * (20 - int(pct / 5))
        print(f"  {s['category']:14s} {s['count']:>5d} {bar} {pct:.0f}%")
    print()
    print("  Sources: use `cli.py obs search --source X` to filter by source")
    print('  Tags: use `cli.py obs search --tags \'{"domain": ["X"]}\'` to filter by tag')


# ── Reflex Rules Commands ──────────────────────────────────────


def _fmt_rule(r: dict) -> str:
    """Format a rule dict into a human-readable string."""
    rid = (r.get("id") or r.get("rule_id") or "?")[:8]
    desc = (r.get("description") or "(no description)")[:60]
    conf = r.get("confidence", 0.0)
    st = r.get("status", "?")
    act = r.get("action_type", "?")
    obs_cnt = r.get("observation_count", 0)
    return f"  [{rid}] {desc:60s} {st:10s} conf={conf:.2f} cnt={obs_cnt:3d} action={act}"


def _cmd_reflex(args, parser):
    """Dispatch reflex subcommands."""
    if args.ref_command == "list":
        rules = rule_search(status=args.status, action_type=args.action, limit=args.limit)
        if not rules:
            print("  (no rules)")
            return
        if args.json:
            print(json.dumps(rules, ensure_ascii=False, indent=2, default=str))
            return
        print(f"  Rules: {len(rules)}")
        print(f"  {'ID':8s}  {'Description':60s}  {'Status':10s}  {'Conf':5s}  {'Cnt':3s}  Action")
        print(f"  {'-' * 8}  {'-' * 60}  {'-' * 10}  {'-' * 5}  {'-' * 3}  {'-' * 10}")
        for r in rules:
            rid = (r.get("id") or "?")[:8]
            desc = (r.get("description") or "")[:60]
            st = r.get("status", "?")
            conf = r.get("confidence", 0.0)
            oc = r.get("observation_count", 0)
            act = r.get("action_type", "?")
            print(f"  [{rid}] {desc:60s} {st:10s} {conf:.2f}  {oc:3d}  {act}")

    elif args.ref_command == "show":
        r = rule_get(args.id)
        if not r:
            print(f"  Rule not found: {args.id}")
            return
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))

    elif args.ref_command == "add":
        params = None
        if args.action_params:
            try:
                params = json.loads(args.action_params)
            except json.JSONDecodeError:
                print(f"  Invalid action_params JSON: {args.action_params}")
                return
        rid = rule_create(
            trigger_category=args.category,
            trigger_source=args.source,
            trigger_pattern=args.pattern,
            trigger_min_count=args.min_count,
            trigger_window_hours=args.window_hours,
            action_type=args.action,
            action_params=params,
            description=args.description,
        )
        if rid:
            print(f"  Rule created: {rid}")
        else:
            print("  Failed to create rule")

    elif args.ref_command == "approve":
        ok = rule_update(args.id, status="approved")
        print(f"  Rule {args.id[:8]}{' approved' if ok else ' approve FAILED'}")

    elif args.ref_command == "reject":
        ok = rule_update(args.id, status="archived")
        print(f"  Rule {args.id[:8]}{' archived' if ok else ' archive FAILED'}")

    elif args.ref_command == "match":
        matches = rule_match(args.observation, category=args.category)
        if not matches:
            print(f"  No matching rules for: {args.observation[:80]}")
            return
        print(f"  Matches for: {args.observation[:80]}")
        for m in matches:
            print(f"  {_fmt_rule(m)}")

    elif args.ref_command == "detect":
        patterns = rule_detect_patterns(
            since_hours=args.hours, min_occurrences=args.min_occurrences
        )
        if not patterns:
            print(
                f"  No patterns detected (last {args.hours}h, min {args.min_occurrences} occurrences)"
            )
            return
        print(f"  Detected {len(patterns)} patterns (last {args.hours}h):")
        for p in patterns:
            cnt = p.get("occurrence_count", 0)
            src = p.get("pattern_source", "?")
            cat = p.get("pattern_category", "?")
            obs = (p.get("pattern_observation") or "")[:80]
            print(f"  [{cnt:3d}x] {cat}/{src}: {obs}")

    elif args.ref_command == "promote":
        promoted = rule_promote_all()
        if not promoted:
            print("  No rules to promote")
        else:
            print(f"  Promoted {len(promoted)} rules:")
            for p in promoted:
                print(
                    f"    Rule {p.get('rule_id', '?')[:8]}: {p.get('old_status')} → {p.get('new_status')} (conf={p.get('confidence', 0):.2f})"
                )

    elif args.ref_command == "decay":
        decayed = rule_decay_all()
        if not decayed:
            print("  No rules to decay")
        else:
            print(f"  Decayed {len(decayed)} rules:")
            for d in decayed:
                print(
                    f"    Rule {d.get('rule_id', '?')[:8]}: {d.get('old_status')} → {d.get('new_status')} (idle {d.get('days_since_match', '?')}d)"
                )

    elif args.ref_command == "report":
        rows = rule_report()
        if not rows:
            print("  No rule data available")
            return
        for r in rows:
            if r["section"] == "summary":
                print(f"  {r['line']}")
            elif r["section"] == "candidates":
                if r["line"]:
                    print(f"  Candidate rules:\n    {r['line']}")
            elif r["section"] == "applied":
                if r["line"]:
                    print(f"  Recently applied:\n    {r['line']}")
    else:
        parser.print_help()


def cmd_search_bm25(args):
    """FTS5 BM25 search via local_index."""
    from lib.search.local_index import FTS5Index

    idx = FTS5Index()
    t0 = time.monotonic()
    results = idx.bm25_search(args.query, limit=args.limit)
    elapsed = round(time.monotonic() - t0, 3)

    if args.json:
        print(
            json.dumps(
                {"results": results, "meta": {"count": len(results), "elapsed_s": elapsed}},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    print(f"BM25 search: {len(results)} results for '{args.query}' ({elapsed}s)\n")
    for r in results:
        print(f"  [{r['rank']:.2f}] {r['agent']} {r['created_at'][:19]}")
        txt = (r.get("text_clean") or "")[:160]
        if txt:
            print(f"       {txt}")
        print()


def cmd_search_hybrid(args):
    """Hybrid BM25 + Dense search via RRF fusion."""
    from lib.search.hybrid import bm25_only, hybrid_search
    from lib.text_cleaner import get_cleaner

    # Preprocess query through Kiwi for BM25
    cl = get_cleaner()
    query_terms = " ".join(cl.extract_terms(args.query))

    # --- Phase 1: BM25 ---
    t0 = time.monotonic()
    bm25_results = bm25_only(query_terms, limit=50)
    bm25_time = round(time.monotonic() - t0, 3)
    bm25_list = bm25_results.get("results", [])

    # Short-circuit: if BM25 top-1 is dominant, skip hybrid
    short_circuited = False
    if not args.no_short_circuit and len(bm25_list) >= 2:
        top1_score = bm25_list[0]["rank"]
        top2_score = bm25_list[1]["rank"]
        # FTS5 BM25: lower = better (more negative = higher relevance)
        if top1_score <= -8.0 and (top2_score - top1_score) > 0.15:
            short_circuited = True

    if short_circuited:
        meta = {
            "mode": "bm25_short_circuit",
            "bm25_time": bm25_time,
            "bm25_count": len(bm25_list),
            "short_circuit": True,
            "top1_score": top1_score,
            "gap": round(top2_score - top1_score, 3),
        }
        results = [
            {
                "turn_id": r["turn_id"],
                "conversation_id": r["conversation_id"],
                "created_at": r["created_at"],
                "agent": r["agent"],
                "seq": r["seq"],
                "text_clean": (r.get("text_clean") or "")[:200],
                "bm25_rank": i,
                "dense_rank": None,
                "rrf_score": 0,
            }
            for i, r in enumerate(bm25_list[: args.limit])
        ]
    else:
        result = hybrid_search(args.query, limit=args.limit)
        results = result["results"]
        meta = result["meta"]
        meta["mode"] = "hybrid_rrf"

        meta["bm25_time"] = bm25_time
        meta["short_circuit"] = False

    if args.json:
        print(json.dumps({"results": results, "meta": meta}, ensure_ascii=False, indent=2))
        return

    mode_label = "BM25 SHORT-CIRCUIT" if short_circuited else "HYBRID RRF"
    print(f"{mode_label}: {len(results)} results for '{args.query}'")
    if short_circuited:
        print(f"  (top1 score={top1_score:.2f}, gap={meta.get('gap', 0):.2f} — dense skipped)")
    else:
        if meta.get("embed_error"):
            print(f"  [warn] Dense search: {meta['embed_error']}")
        print(
            f"  BM25={meta.get('bm25_count', 0)} dense={meta.get('dense_count', 0)} "
            f"bm25_time={meta.get('bm25_time', 0)}s dense_time={meta.get('dense_time', 0)}s"
        )

    print()
    for r in results:
        label = ""
        if r.get("bm25_rank") is not None and r.get("dense_rank") is not None:
            label = f" B{r['bm25_rank']} D{r['dense_rank']}"
        elif r.get("dense_rank") is None:
            label = f" B{r.get('bm25_rank', '?')}"
        print(
            f"  [{r.get('rrf_score', r.get('bm25_score', 0)):.2f}{label}] {r.get('agent', '?')} {r.get('created_at', '')[:19]}"
        )
        txt = (r.get("text_clean") or "")[:160]
        if txt:
            print(f"       {txt}")
        print()


def cmd_research_search(args):
    from lib.research import research
    try:
        r = research(args.query, mode=args.mode, candidate_k=args.candidate_k,
                     top_k=args.top_k, rerank=not args.no_rerank)
    except Exception as e:
        print(f"research error: {e}", file=sys.stderr)
        return
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    for i, item in enumerate(r["results"], 1):
        print(f"{i}. {item.get('title', '')}\n   {item.get('url', '')}\n   {item.get('snippet', '')[:200]}")
    print(f"\n[{r['meta']}]")


def cmd_research_docs(args):
    from lib.research import docs
    try:
        r = docs(args.library, args.question, top_k=args.top_k)
    except ValueError as e:
        print(f"docs error: {e}", file=sys.stderr)
        return
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    if not r["results"]:
        print(f"No docs found for '{args.library}'.")
        return
    print(r["results"][0].get("snippet", ""))


def cmd_research_fetch(args):
    from lib.research import fetch_page
    try:
        r = fetch_page(args.url, max_chars=args.max_chars)
    except ValueError as e:
        print(f"fetch error: {e}", file=sys.stderr)
        return
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    if r.get("error"):
        print(f"fetch error: {r['error']}", file=sys.stderr)
        return
    print(r.get("text", ""))


MODE_FILE_INFERENCE = "/opt/ai_data/scripts/current-mode-inference.env"
SYSTEM_MODE_FILE = "/opt/ai_data/scripts/current-system-mode.env"


def _switch_mode(mode: str) -> bool:
    """Write mode file and restart inference container. mode: day|review|verify."""
    valid_modes = {"day", "review", "verify"}
    if mode not in valid_modes:
        print(f"Unknown mode: {mode}")
        return False

    # Write mode file
    with open(MODE_FILE_INFERENCE, "w") as f:
        f.write(f"MODE={mode}\n")
    print(f"Switched inference to {mode}")

    # Restart inference container
    print("Restarting devforge-inference...")
    from lib.pod_manager.container import _podman_start_inference, _podman_stop_inference

    _podman_stop_inference()
    _podman_start_inference()

    # Wait for model to load
    print("Waiting for models to load...")
    for _ in range(120):
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{MODEL_REGISTRY['verifier']['port']}/health"
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read())
                    if data.get("status") == "ok":
                        print(
                            f"  Ready: {data.get('slots_idle', '?')} idle / {data.get('slots_processing', '?')} processing"
                        )
                        return True
        except Exception:
            pass
        time.sleep(2)
    print("Warning: model health check timed out, may still be loading")
    return True


def cmd_discussion(args):
    """Launch multi-agent LLM debate (DRAG or Tool-MAD)."""
    method = args.method  # "drag" or "toolmad"
    question = getattr(args, "question", None)
    skip_drag = getattr(args, "skip_drag", False)
    dry_run = getattr(args, "dry_run", False)

    # Ensure container is in review mode for DRAG + verification
    if not _container_in_review_mode():
        print("Switching container to review mode...")
        if not _switch_mode("review"):
            print("ERROR: failed to switch to review mode")
            return

    # Prompt for question if not provided
    if not question:
        try:
            question = input("Enter your question: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return
    if not question:
        print("ERROR: question required")
        return

    print(f"\nStarting debate [{method.upper()}]...")
    print(f"  Question: {question}")
    if skip_drag:
        print("  DRAG: skipped")
    if dry_run:
        print("  Dry-run: enabled")

    # Import and run
    from lib.debate.local_debate import LocalDebateReview

    session = LocalDebateReview(
        question=question,
        method=method,
        skip_drag=skip_drag,
        dry_run=dry_run,
    )
    result = session.run_session()
    if result is None:
        print("\nDebate FAILED — check logs above for details.")
    else:
        print(f"\nReport: /opt/ai_data/debate_sessions/{session.session_id}/final_report.md")


def _container_in_review_mode() -> bool:
    """Check if devforge-inference container is running in review mode (llama-server on :8081)."""
    try:
        r = subprocess.run(
            ["podman", "exec", "devforge-inference", "pgrep", "-f", "llama-server.*8081"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def cmd_upload(args):
    """Upload a pipeline result or raw file to Azure Blob."""
    pipeline = args.pipeline
    session_id = getattr(args, "session_id", None)
    file_path = getattr(args, "file", None)
    title = getattr(args, "title", None)

    if not session_id:
        print("ERROR: --session-id is required")
        return

    from lib.blob_uploader import upload_raw, upload_review_bundle

    if file_path:
        # Raw file upload
        path = Path(file_path)
        if not path.exists():
            print(f"ERROR: file not found: {file_path}")
            return
        content = path.read_text()
        url = upload_raw(content, pipeline, session_id, path.name)
        print(f"Uploaded raw: {path.name}")
    else:
        # Auto-generate review-bundle from known output locations
        content = _find_pipeline_output(pipeline, session_id)
        if content is None:
            print(f"ERROR: no output found for {pipeline}/{session_id}")
            print("  Use --file to upload a specific file")
            return
        url = upload_review_bundle(
            content=content,
            pipeline=pipeline,
            session_id=session_id,
            metadata={"title": title} if title else None,
        )
        print("Uploaded review-bundle")

    print(f"  Pipeline: {pipeline}")
    print(f"  Session:  {session_id}")
    print(f"  SAS URL:  {url}")


def cmd_extract(args):
    """Run extract pipeline on turns."""
    from pipelines.extract import extract_pipeline as _run_extract

    result = _run_extract(
        turn_id=getattr(args, "turn_id", None),
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print(f"  processed: {result['processed']}")
    print(f"  failed:    {result['failed']}")
    print(f"  facts:     {result['facts']}")
    if result.get("elapsed_s"):
        print(f"  elapsed:   {result['elapsed_s']}s")


def cmd_enrich_consume(args):
    """Read and format enrichment metadata from review_facts."""
    from lib.enrich_consumer import consume_enrich

    results = consume_enrich(
        limit=getattr(args, "limit", 50),
        dry_run=getattr(args, "dry_run", False),
    )
    print(f"  formatted: {len(results)} enrichment items")
    if args.json:
        import json as _json

        for r in results:
            print(_json.dumps(r, ensure_ascii=False))


def _find_pipeline_output(pipeline: str, session_id: str) -> Optional[str]:
    """Auto-find pipeline output for review-bundle generation."""
    candidates = {
        "debate": [
            f"/opt/ai_data/debate_sessions/{session_id}/final_report.md",
        ],
        "code_mod": [
            f"/var/tmp/code_mod_tests/task{session_id}_*.json",
        ],
        "extract": [
            f"/opt/ai_data/extractions/{session_id}/report.json",
        ],
    }
    patterns = candidates.get(pipeline, [])
    for pattern in patterns:
        import glob as _glob

        for p in sorted(_glob.glob(pattern)):
            return Path(p).read_text()
    return None


AUTO_TASKS_FILE = "/opt/projects/server/data/auto_tasks.md"
AUTO_HEADER = "# Auto Tasks"
AUTO_COMMENT = "<!--"
AUTO_COMMENT_END = "-->"


def _read_auto_tasks():
    """Return list of (title, body) tuples from auto_tasks.md, excluding comments."""
    import re

    try:
        content = Path(AUTO_TASKS_FILE).read_text()
    except FileNotFoundError:
        return []
    # Remove HTML comment blocks
    content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    tasks = []
    for match in re.finditer(r"^## (.+)$", content, re.MULTILINE):
        title = match.group(1).strip()
        if title.startswith("Example:"):
            continue
        start = match.end()
        next_match = re.search(r"^## ", content[start:], re.MULTILINE)
        end = start + next_match.start() if next_match else len(content)
        body = content[start:end].strip()
        tasks.append((title, body))
    return tasks


def _write_auto_tasks(tasks):
    """Write tasks list back to auto_tasks.md with header."""
    lines = [
        AUTO_HEADER,
        "",
        AUTO_COMMENT,
        " Tasks execute via day cycle (03:00 KST / 18:00 UTC).",
        ' CLI: python3 cli.py auto add "title" "description"',
        " Each ## section = a separate Claude Code invocation.",
        " Full permissions granted. Results logged to auto_logs/.",
        AUTO_COMMENT_END,
        "",
    ]
    for title, body in tasks:
        lines.append(f"## {title}")
        lines.append(body)
        lines.append("")
    Path(AUTO_TASKS_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(AUTO_TASKS_FILE).write_text("\n".join(lines))


def cmd_auto_add(args):
    """Add a task to auto mode."""
    tasks = _read_auto_tasks()
    tasks.append((args.title, args.description))
    _write_auto_tasks(tasks)
    print(f"+ auto task: {args.title}")


def cmd_auto_list(args):
    """List current auto mode tasks."""
    tasks = _read_auto_tasks()
    if not tasks:
        print("(empty — no tasks scheduled)")
        return
    for i, (title, body) in enumerate(tasks, 1):
        body_preview = body[:100].replace("\n", " ") + ("..." if len(body) > 100 else "")
        print(f"[{i}] {title}")
        print(f"    {body_preview}")
        print()


def cmd_auto_clear(args):
    """Clear all auto mode tasks."""
    _write_auto_tasks([])
    print("Auto tasks cleared")


def cmd_dev_poll(args):
    """Poll for unassigned issues not yet seen."""
    issues = poll_issues(label=args.label, auto_safe=bool(args.auto_safe))
    if not issues:
        print("처리할 이슈가 없습니다.")
        return
    for i in issues:
        num = i["number"]
        ttl = i["title"]
        print(f"  #{num} {ttl}")
    if args.claim:
        claimed = 0
        for i in issues:
            ok = claim_issue(i["number"])
            if ok:
                claimed += 1
        print(f"\n{claimed}/{len(issues)} issues claimed.")
    elif not args.once:
        state_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "dev_pipeline_state.json")
        if os.path.isfile(state_path):
            with open(state_path) as f:
                state = json.load(f)
            seen = state.get("seen_issues", [])
        else:
            seen = []
        new_count = len(issues)
        total = len(seen) + new_count
        print(f"\n{new_count} new / {total} total tracked issues")


def cmd_dev_claim(args):
    """Claim an issue: assign, branch, auto task."""
    ok = claim_issue(args.number)
    if ok:
        print(f"Issue #{args.number} assigned, branch created, auto task written.")
    else:
        print(f"Issue #{args.number} claim failed — check gh auth & issue number.")


def cmd_dev_pr(args):
    """Create a PR from the issue branch."""
    url = create_pr(args.number)
    if url:
        print(f"PR created: {url}")
    else:
        print("PR creation failed — push branch first, then retry.")


def cmd_dashboard(args):
    """Show review_facts model performance dashboard."""
    sql_model = """
SELECT extract_model,
  count(*) AS total,
  count(*) FILTER (WHERE verdict='valid') AS valid,
  count(*) FILTER (WHERE verdict='hallucinated') AS halluc,
  count(*) FILTER (WHERE verdict='context_dependent') AS ctx,
  round(avg(gen_rate)::numeric, 2) AS rate,
  round(avg(elapsed_ms/1000.0)::numeric, 1) AS secs,
  round(avg(cache_hit)::numeric, 0) AS cache_pct
FROM review_facts
GROUP BY extract_model ORDER BY total DESC
"""
    result = _sql(sql_model)
    if not result:
        print("데이터 없음")
        return
    print("═══ Model Performance Dashboard ═══\n")
    print(
        f"{'Model':<32} {'Total':>5} {'Valid%':>7} {'Halluc%':>8} {'Rate':>6} {'Sec':>5} {'Cache%':>6}"
    )
    print("─" * 78)
    for line in result.split("\n"):
        if not line:
            continue
        p = line.split("|")
        if len(p) < 8:
            continue
        model = p[0].strip()[:30]
        total = int(p[1].strip() or 0)
        valid = int(p[2].strip() or 0)
        halluc = int(p[3].strip() or 0)
        valid_pct = f"{valid / total * 100:.1f}" if total else "-"
        halluc_pct = f"{halluc / total * 100:.1f}" if total else "-"
        rate = p[5].strip() or "-"
        secs = p[6].strip() or "-"
        cache = p[7].strip() or "-"
        print(
            f"{model:<32} {total:>5} {valid_pct:>7} {halluc_pct:>8} {rate:>6} {secs:>5} {cache:>6}"
        )

    sql_daily = """
SELECT created_at::date AS day,
  count(*) AS total,
  count(*) FILTER (WHERE verdict='valid') AS valid,
  round(avg(gen_rate)::numeric, 2) AS rate
FROM review_facts
GROUP BY day ORDER BY day DESC LIMIT 7
"""
    daily = _sql(sql_daily)
    if daily:
        print(f"\n{'─' * 78}")
        print(f"\n{'Date':<12} {'Facts':>6} {'Valid%':>7} {'Rate(t/s)':>10}")
        print("─" * 40)
        for line in daily.split("\n"):
            if not line:
                continue
            p = line.split("|")
            if len(p) < 4:
                continue
            day = p[0].strip()
            total = int(p[1].strip() or 0)
            valid = int(p[2].strip() or 0)
            rate = p[3].strip() or "-"
            valid_pct = f"{valid / total * 100:.1f}" if total else "-"
            print(f"{day:<12} {total:>6} {valid_pct:>7} {rate:>10}")

    sql_summary = """
SELECT count(*), count(*) FILTER (WHERE verdict='valid'),
  count(DISTINCT turn_id), count(DISTINCT extract_model)
FROM review_facts
"""
    s = _sql(sql_summary)
    if s:
        p = s.strip().split("|")
        if len(p) >= 4:
            total, valid, turns, models = int(p[0]), int(p[1]), int(p[2]), int(p[3])
            print(
                f"\n총 {total} facts / {turns} turns / {models} models — overall valid {valid / total * 100:.1f}%"
            )


# ═══════════════════════════════════════════════════════════════
# status — live system query, single source of truth
# ═══════════════════════════════════════════════════════════════


def cmd_task_list(args):
    """List tasks from DB."""
    from lib.db import esc_sql
    from lib.db import psql_json as _pj

    where = ""
    if args.status:
        where = f"WHERE status = '{esc_sql(args.status)}'"
    sql = f"""SELECT id, title, status, priority, substring(description,1,80) AS excerpt,
       created_at, updated_at FROM tasks
    {where} ORDER BY
       CASE status WHEN 'in_progress' THEN 1 WHEN 'pending' THEN 2 WHEN 'blocked' THEN 3 ELSE 4 END,
       CASE priority WHEN 'P0' THEN 1 WHEN 'P1' THEN 2 WHEN 'P2' THEN 3 ELSE 4 END,
       created_at DESC"""
    rows = _pj(sql)
    if not rows:
        print("(no tasks)")
        return
    print(f"{'ID':<5} {'Status':<12} {'Priority':<8} {'Title':<60} {'Created'}")
    print("-" * 110)
    for r in rows:
        print(
            f"{r['id']:<5} {r['status']:<12} {str(r['priority'] or ''):<8} "
            f"{str(r['title'])[:58]:<60} {str(r['created_at'])[:19]}"
        )


def cmd_task_add(args):
    """Add a new task to DB."""
    from lib.db import esc_sql
    from lib.db import psql as _sql

    title = esc_sql(args.title)
    priority = args.priority
    priority_sql = f"'{esc_sql(priority)}'" if priority else "NULL"
    desc = esc_sql(args.description or "")
    result = _sql(f"""INSERT INTO tasks (title, priority, description)
    VALUES ('{title}', {priority_sql}, '{desc}') RETURNING id""")
    if result and result.strip():
        print(f"Task created: id={result.strip()} - {args.title[:60]}")


def cmd_task_update(args):
    """Update task status/notes."""
    import json

    from lib.db import esc_sql
    from lib.db import psql as _sql
    from lib.db import psql_json as _pj

    rows = _pj(f"SELECT id, title, status, notes FROM tasks WHERE id = {args.id}")
    if not rows:
        print(f"ERROR: task id={args.id} not found")
        return
    t = rows[0]
    update_cols = []
    if args.status:
        update_cols.append(f"status = '{esc_sql(args.status)}'")
    if args.description:
        update_cols.append(f"description = '{esc_sql(args.description)}'")
    if args.status == "completed" or t["status"] != "completed" and args.status == "completed":
        update_cols.append("completed_at = NOW()")
    if args.note:
        notes = t.get("notes", [])
        if not isinstance(notes, list):
            notes = []
        notes.append(args.note)
        update_cols.append(f"notes = '{json.dumps(notes)}'::jsonb")
    if not update_cols:
        print("No changes specified")
        return
    update_cols.append("updated_at = NOW()")
    sql = f"UPDATE tasks SET {', '.join(update_cols)} WHERE id = {args.id} RETURNING title"
    result = _sql(sql)
    if result:
        print(f"Updated: {result.strip()}")


def cmd_task_delete(args):
    """Soft-delete a task (set status = 'deleted')."""
    from lib.db import psql as _sql

    sql = f"UPDATE tasks SET status = 'deleted', updated_at = NOW() WHERE id = {args.id} RETURNING title"
    result = _sql(sql)
    if result and result.strip():
        print(f"Deleted: {result.strip()}")


def cmd_task_show(args):
    """Show full task details."""
    from lib.db import psql_json as _pj

    rows = _pj(f"SELECT * FROM tasks WHERE id = {args.id}")
    if not rows:
        print(f"ERROR: task id={args.id} not found")
        return
    r = rows[0]
    print(f"Task #{r['id']}: {r['title']}")
    print(f"  Status:    {r['status']}")
    if r.get("priority"):
        print(f"  Priority:  {r['priority']}")
    if r.get("description"):
        print("  Description:")
        for line in (r["description"] or "").split("\n"):
            print(f"    {line}")
    notes = r.get("notes", [])
    if notes and isinstance(notes, list) and len(notes) > 0:
        print("  Notes:")
        for n in notes:
            print(f"    - {n}")
    print(f"  Created:   {r['created_at']}")
    print(f"  Updated:   {r['updated_at']}")
    if r.get("completed_at"):
        print(f"  Completed: {r['completed_at']}")
    if r.get("agent"):
        print(f"  Agent:     {r['agent']}")
    if r.get("tags") and isinstance(r["tags"], list) and r["tags"]:
        print(f"  Tags:      {', '.join(r['tags'])}")


def cmd_status(args):
    """Live system status — single source of truth for LLM and humans."""
    containers = _get_containers()
    models = _get_models()
    resources = _get_resources()

    result = {
        "host": {
            "hostname": os.uname().nodename,
            "arch": os.uname().machine,
            "os": f"{os.uname().sysname} {os.uname().release}",
        },
        "containers": containers,
        "models": models,
        "timers": _get_timers(),
        "services": _get_services(),
        "resources": resources,
        "experiments": _get_experiments(),
        "active_config": _get_active_config(),
        "tasks": _get_tasks(),
        "alerts": _get_alerts(containers, resources),
        "rules": _get_rule_status(),
        "glossary": _get_glossary(),
        "references": _get_references(),
    }

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        # Human-readable summary
        print("═══ DevForge Status ═══")
        print(
            f"Host: {result['host']['hostname']} ({result['host']['arch']}) — {resources.get('uptime', '?')} up"
        )
        print(
            f"Load: {resources.get('load', {}).get('1min', '?')} {resources.get('load', {}).get('5min', '?')} {resources.get('load', {}).get('15min', '?')}"
        )
        mem = resources.get("memory", {})
        print(
            f"Mem: {mem.get('used', '?')}/{mem.get('total', '?')} (avail {mem.get('available', '?')})"
        )
        swap = resources.get("swap", {})
        if swap:
            print(f"Swap: {swap.get('used', '?')}/{swap.get('total', '?')}")

        print("\n── Containers ──")
        for name, info in containers.items():
            print(f"  {name}: {info['status']}")

        print("\n── Models ──")
        for pod, model_list in models.items():
            if isinstance(model_list, list):
                print(f"  {pod} ({' '.join(model_list)})")
            else:
                print(f"  {pod}: {model_list}")

        tasks = result.get("tasks", {})
        if isinstance(tasks, dict) and "error" not in tasks:
            print("\n── Tasks ──")
            print(f"  in_progress: {len(tasks.get('in_progress', []))}")
            print(f"  pending: {len(tasks.get('pending', []))}")
            print(f"  blocked: {len(tasks.get('blocked', []))}")
            print(f"  completed: {tasks.get('completed_count', 0)} / {tasks.get('total', 0)}")

        alerts = result.get("alerts", [])
        if alerts:
            print("\n── Alerts ──")
            for a in alerts:
                print(f"  ⚠ {a}")

        print("\nUse --json for machine-readable output.")


def cmd_glossary_sync(args):
    """Sync docs/domain-glossary.yaml → DB glossary_terms (idempotent upsert).

    YAML is the single source of truth. This is the ONLY write path to DB.
    """
    import yaml
    from lib.db import esc_sql as _esc
    from lib.db import psql_ok as _ok

    yaml_path = Path("/opt/projects/server/docs/domain-glossary.yaml")
    raw = yaml_path.read_text()
    if raw.startswith("#"):
        _, _, raw = raw.partition("\n")
    data = yaml.safe_load(raw)
    if not data or "bounded_contexts" not in data:
        print("ERROR: domain-glossary.yaml empty or invalid")
        return 1

    ctx_count = 0
    term_count = 0
    errors = []

    for bc in data["bounded_contexts"]:
        bc_id = int(bc["id"])
        bc_name = _esc(bc["name"])
        sql = (
            f"INSERT INTO bounded_contexts (id, name) VALUES ({bc_id}, '{bc_name}') "
            f"ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name"
        )
        if _ok(sql):
            ctx_count += 1
        else:
            errors.append(f"context {bc['name']}")

        for term_entry in bc.get("terms", []):
            term = _esc(term_entry["term"])
            definition = _esc(term_entry["definition"])
            tables = term_entry.get("tables", [])
            files = term_entry.get("related_files", [])
            tables_pg = (
                "'{}'::text[]"
                if not tables
                else "ARRAY[" + ", ".join(f"'{_esc(t)}'" for t in tables) + "]"
            )
            files_pg = (
                "'{}'::text[]"
                if not files
                else "ARRAY[" + ", ".join(f"'{_esc(f)}'" for f in files) + "]"
            )

            sql = (
                f"INSERT INTO glossary_terms (term, definition, bounded_context_id, tables_ref, related_files) "
                f"VALUES ('{term}', '{definition}', {bc_id}, {tables_pg}, {files_pg}) "
                f"ON CONFLICT (term, bounded_context_id) DO UPDATE SET "
                f"definition = EXCLUDED.definition, tables_ref = EXCLUDED.tables_ref, "
                f"related_files = EXCLUDED.related_files"
            )
            if _ok(sql):
                term_count += 1
            else:
                errors.append(term_entry["term"])

    if errors:
        print(f"⚠️  Synced {ctx_count} contexts, {term_count} terms with {len(errors)} error(s)")
        for e in errors:
            print(f"  FAIL: {e}")
        return 1
    else:
        print(f"✅ Synced {ctx_count} contexts, {term_count} terms — DB is up to date")
        return 0


# ═══════════════════════════════════════════════════════════════════
# File Management commands
# ═══════════════════════════════════════════════════════════════════


def cmd_file_find(args):
    """Search files by keyword (description, filename, tags)."""
    from lib.file_registry import search_files

    results = search_files(args.query, limit=args.limit)
    if not results:
        print("No files found.")
        return
    for r in results:
        tags = " ".join(f"#{t}" for t in r.get("tags", []) or [])
        desc = (r.get("description") or "")[:80]
        print(f"  [{r['id'][:8]}] {r['filename']} ({r.get('size', 0)} bytes)")
        if desc:
            print(f"       {desc}")
        print(f"       source={r['source']}  created={r['created_at'][:19]}  {tags}")
        print()


def cmd_file_list(args):
    """List recent files, optionally filtered by source."""
    from lib.file_registry import list_files

    results = list_files(source=args.source, limit=args.limit)
    if not results:
        print("No files found.")
        return
    print(f"{'ID':<12} {'Filename':<30} {'Source':<20} {'Size':<10} {'Created'}")
    print("-" * 90)
    for r in results:
        fid = r["id"][:12]
        fname = r["filename"][:29]
        src = r["source"][:19]
        sz = str(r.get("size", 0))
        utc_timestamp = r["created_at"][:19]
        print(f"{fid:<12} {fname:<30} {src:<20} {sz:<10} {utc_timestamp}")


def cmd_file_get(args):
    """Show full details of a single file by UUID."""
    from lib.file_registry import get_file

    rec = get_file(args.id)
    if not rec:
        print(f"File not found: {args.id}")
        return
    for k, v in rec.items():
        print(f"  {k}: {v}")


def cmd_file_push(args):
    """Register a local file into file_registry DB."""
    from lib.file_registry import register_file

    tags = args.tags.split(",") if args.tags else None
    fid = register_file(
        src_path=args.path,
        source=args.source or "agent_generate",
        description=args.description,
        tags=tags,
    )
    if fid:
        print(f"Registered: {fid}")
    else:
        print("Failed to register file (possibly already exists / not found).")


def cmd_file_delete(args):
    """Delete file record from registry, optionally remove local file."""
    from lib.file_registry import delete_file

    if delete_file(args.id, remove_local=args.remove_local):
        print(f"Deleted: {args.id}")
    else:
        print(f"Failed to delete: {args.id}")


def cmd_lint(args):
    """Check code against enforced rules."""
    from lint_rules import SCRIPTS_DIR, find_python_files, run_all_checks

    if args.files:
        files = [Path(f) for f in args.files]
    else:
        files = find_python_files(SCRIPTS_DIR)

    result = run_all_checks(files)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        if result["passed"]:
            print("✅ All rules passed — no violations found.")
        else:
            print(f"❌ {result['status']}")
            for v in result["violations"]:
                loc = f"{v['file']}:{v.get('line', '')}" if v.get("line") else v["file"]
                print(f"  [{v['severity']}] {v['rule']}: {v['message']}")
                print(f"        at {loc}")
            print(f"\n  Files: {result['total_files']} | Violations: {result['violations_total']}")
            print(
                f"  P0:{result['violations_by_severity']['P0']} P1:{result['violations_by_severity']['P1']} P2:{result['violations_by_severity']['P2']}"
            )

    if args.fix:
        print("\n── Suggested Fixes ──")
        # Group by type
        seen = set()
        for v in result["violations"]:
            key = v["message"][:60]
            if key not in seen:
                seen.add(key)
                if "Rename to" in v["message"]:
                    print(f"  {v['message']}")

    if not result["passed"]:
        sys.exit(1)


async def main():
    parser = argparse.ArgumentParser(description="DevForge CLI")
    sub = parser.add_subparsers(dest="command")

    p_mem_search = sub.add_parser("mem-search", help="Search memories")
    p_mem_search.add_argument("query")
    p_mem_search.add_argument("--source", "-s", help="Filter by source (e.g., claude, copilot, gemini)")
    p_mem_search.add_argument("--limit", "-n", type=int, default=20)
    p_mem_search.add_argument("--json", "-j", action="store_true")

    p_save = sub.add_parser("save", help="Save a memory")
    p_save.add_argument("--source", "-s", required=True)
    p_save.add_argument("--summary", "-m", help="Title/summary")
    p_save.add_argument("--detail", "-d", help="Detail as JSON string")
    p_save.add_argument("--model", help="AI model name")

    p_recent = sub.add_parser("recent", help="Show recent entries")
    p_recent.add_argument("--source", "-s")
    p_recent.add_argument("--limit", "-n", type=int, default=10)

    p_worklog = sub.add_parser("worklog", help="Worklog management")
    wl_sub = p_worklog.add_subparsers(dest="wl_command")

    wl_add = wl_sub.add_parser("add", help="Add a new worklog entry to DB")
    wl_add.add_argument("title", help="Entry title")
    wl_add.add_argument("summary", help="Entry summary")
    wl_add.add_argument("--tags", help="Comma-separated tags")
    wl_add.add_argument("--files", help="Comma-separated file paths")
    wl_add.add_argument("--details", help="Comma-separated detail items")
    wl_add.add_argument("--agent", help="AI agent name (e.g., claude-code)")
    wl_add.add_argument("--model", help="AI model name (e.g., claude-opus-4-7)")

    wl_recent = wl_sub.add_parser("recent", help="Show recent worklog entries")
    wl_recent.add_argument("--limit", "-n", type=int, default=3)

    wl_search = wl_sub.add_parser("search", help="Search worklog entries")
    wl_search.add_argument("query", nargs="?", help="Keyword to search")
    wl_search.add_argument("--tag", "-t", help="Filter by tag")
    wl_search.add_argument("--limit", "-n", type=int, default=20)

    p_disc = sub.add_parser("discussion", help="Start multi-agent LLM debate (DRAG or Tool-MAD)")
    disc_sub = p_disc.add_subparsers(dest="method")
    drag_p = disc_sub.add_parser(
        "drag", help="DRAG: 2-stage debate — query consensus → fetch → synthesize"
    )
    drag_p.add_argument("question", nargs="?", help="Debate topic / question")
    drag_p.add_argument(
        "--skip-drag", action="store_true", help="Skip Round 0 DRAG query consensus"
    )
    drag_p.add_argument("--dry-run", action="store_true", help="Simulate without LLM calls")
    drag_p.add_argument(
        "--with-api",
        action="store_true",
        help="Use DeepSeek API for real search (internet knowledge)",
    )
    tmad_p = disc_sub.add_parser(
        "toolmad", help="Tool-MAD: adaptive real-time search during debate rounds"
    )
    tmad_p.add_argument("question", nargs="?", help="Debate topic / question")
    tmad_p.add_argument(
        "--skip-drag", action="store_true", help="Skip Round 0 DRAG query consensus"
    )
    tmad_p.add_argument("--dry-run", action="store_true", help="Simulate without LLM calls")
    tmad_p.add_argument(
        "--with-api",
        action="store_true",
        help="Use DeepSeek API for real search (internet knowledge)",
    )

    sub.add_parser("dashboard", help="Model performance dashboard")

    p_extract = sub.add_parser("extract", help="Run extract pipeline on conversation turns")
    p_extract.add_argument("--turn-id", help="Process a specific turn UUID")
    p_extract.add_argument("--limit", "-n", type=int, default=100, help="Max turns to process")
    p_extract.add_argument("--dry-run", action="store_true", help="Simulate without DB writes")

    p_enrich = sub.add_parser(
        "enrich-consume", help="Format enrichment metadata from review_facts for MCP tools"
    )
    p_enrich.add_argument("--limit", "-n", type=int, default=50)
    p_enrich.add_argument("--dry-run", action="store_true", help="Read only, no verdict update")
    p_enrich.add_argument("--json", action="store_true", help="JSON output")

    p_upload = sub.add_parser("upload", help="Upload pipeline result to Azure Blob")
    p_upload.add_argument(
        "--pipeline",
        "-p",
        required=True,
        choices=["debate", "code_mod", "extract"],
        help="Pipeline name",
    )
    p_upload.add_argument("--session-id", "-s", required=True, help="Session identifier")
    p_upload.add_argument(
        "--file", "-f", help="Raw file path to upload (skip review-bundle wrapping)"
    )
    p_upload.add_argument("--title", "-t", help="Bundle title (for review-bundle mode)")

    p_auto = sub.add_parser("auto", help="Auto mode task management")
    auto_sub = p_auto.add_subparsers(dest="auto_command")

    auto_add = auto_sub.add_parser("add", help="Add a task to auto mode")
    auto_add.add_argument("title", help="Task title (## heading)")
    auto_add.add_argument("description", help="Task description / prompt for Claude Code")

    auto_sub.add_parser("list", help="List scheduled auto tasks")

    auto_sub.add_parser("clear", help="Clear all auto tasks")

    p_dev = sub.add_parser("dev", help="Dev(Devin-like) — GitHub Issue → PR pipeline")
    dev_sub = p_dev.add_subparsers(dest="dev_command")
    dev_poll = dev_sub.add_parser("poll", help="Poll for unassigned issues")
    dev_poll.add_argument("--label", help="Filter by label")
    dev_poll.add_argument("--once", action="store_true", help="Don't read state file")
    dev_poll.add_argument("--auto-safe", action="store_true", help="Only auto-safe labeled issues")
    dev_poll.add_argument(
        "--claim",
        action="store_true",
        help="Auto-claim all polled issues (use with --auto-safe)",
    )
    dev_claim = dev_sub.add_parser("claim", help="Claim an issue and create branch")
    dev_claim.add_argument("number", type=int, help="Issue number")
    dev_pr = dev_sub.add_parser("pr", help="Create PR from issue branch")
    dev_pr.add_argument("number", type=int, help="Issue number")

    p_experiment = sub.add_parser("experiment", help="실험 레지스트리 관리")
    exp_sub = p_experiment.add_subparsers(dest="exp_command")

    exp_list = exp_sub.add_parser("list", help="List experiments")
    exp_list.add_argument("--all", action="store_true", help="Show all experiments")
    exp_list.add_argument("--category", "-c", help="Filter by category")
    exp_list.add_argument("--limit", "-n", type=int, default=20, help="Max results (default: 20)")

    exp_compare = exp_sub.add_parser("compare", help="Compare experiments")
    exp_compare.add_argument("experiment_ids", nargs="+", help="Experiment IDs to compare")

    exp_sub.add_parser("active", help="Show active config")

    exp_adopt = exp_sub.add_parser("adopt", help="Adopt experiment as active config")
    exp_adopt.add_argument("experiment_id", help="Experiment ID to adopt")
    exp_adopt.add_argument(
        "--component",
        "-c",
        required=True,
        choices=["inference-day", "inference-night"],
        help="Component to update",
    )

    p_task = sub.add_parser("task", help="Task management (DB)")
    task_sub = p_task.add_subparsers(dest="task_command")

    task_list = task_sub.add_parser("list", help="List tasks")
    task_list.add_argument(
        "--status", choices=["pending", "in_progress", "blocked", "completed", "deleted"]
    )

    task_add = task_sub.add_parser("add", help="Add a new task")
    task_add.add_argument("title", help="Task title")
    task_add.add_argument("--priority", "-p", choices=["P0", "P1", "P2"])
    task_add.add_argument("--description", "-d")

    task_update = task_sub.add_parser("update", help="Update a task")
    task_update.add_argument("id", type=int, help="Task ID")
    task_update.add_argument(
        "--status", choices=["pending", "in_progress", "blocked", "completed", "deleted"]
    )
    task_update.add_argument("--description", "-d")
    task_update.add_argument("--note")

    task_delete = task_sub.add_parser("delete", help="Soft-delete a task")
    task_delete.add_argument("id", type=int, help="Task ID to delete")

    task_show = task_sub.add_parser("show", help="Show full task details")
    task_show.add_argument("id", type=int, help="Task ID")

    p_act = sub.add_parser("activity", help="Activity log management")
    act_sub = p_act.add_subparsers(dest="act_command")

    act_recent = act_sub.add_parser("recent", help="Show recent activity_log entries")
    act_recent.add_argument("--limit", "-n", type=int, default=10)
    act_recent.add_argument("--today", action="store_true", help="Today only")

    act_sub.add_parser("stats", help="Activity log statistics")

    act_add = act_sub.add_parser("add", help="Add a manual activity entry")
    act_add.add_argument("title", help="Entry title")
    act_add.add_argument("summary", help="Entry summary")
    act_add.add_argument("--tags", help="Comma-separated tags")

    p_search = sub.add_parser("search", help="대화 검색 — BM25 또는 hybrid (RRF fusion)")
    search_sub = p_search.add_subparsers(dest="search_command")

    p_bm25 = search_sub.add_parser("bm25", help="FTS5 BM25 키워드 검색")
    p_bm25.add_argument("query", help="검색어 (Kiwi 형태소 분석 추천)")
    p_bm25.add_argument("--limit", "-n", type=int, default=20, help="Max results")
    p_bm25.add_argument("--json", action="store_true", help="JSON output")

    p_hybrid = search_sub.add_parser("hybrid", help="BM25 + Dense 하이브리드 (RRF k=60)")
    p_hybrid.add_argument("query", help="검색어 (자연어)")
    p_hybrid.add_argument("--limit", "-n", type=int, default=20, help="Max results")
    p_hybrid.add_argument("--json", action="store_true", help="JSON output")
    p_hybrid.add_argument(
        "--no-short-circuit", action="store_true", help="BM25 dominant여도 항상 Dense 실행"
    )

    p_research = sub.add_parser("research", help="서버측 리서치 (web/exa/docs/fetch) — MCP 대체")
    res_sub = p_research.add_subparsers(dest="research_command")

    r_search = res_sub.add_parser("search", help="웹/시맨틱 검색")
    r_search.add_argument("query")
    r_search.add_argument("--mode", default="auto", choices=["auto", "web", "exa"])
    r_search.add_argument("--candidate-k", type=int, default=30, help="수집 후보 수")
    r_search.add_argument("--top-k", type=int, default=5, help="최종 결과 수")
    r_search.add_argument("--no-rerank", action="store_true", help="리랭크 비활성(P5a: 기본 비활성)")
    r_search.add_argument("--json", action="store_true")

    r_docs = res_sub.add_parser("docs", help="Context7 문서 조회")
    r_docs.add_argument("library", help="라이브러리명 (예: FastAPI)")
    r_docs.add_argument("question", help="질문")
    r_docs.add_argument("--top-k", type=int, default=5)
    r_docs.add_argument("--json", action="store_true")

    r_fetch = res_sub.add_parser("fetch", help="URL 본문 추출")
    r_fetch.add_argument("url")
    r_fetch.add_argument("--max-chars", type=int, default=50000)
    r_fetch.add_argument("--json", action="store_true")

    p_status = sub.add_parser(
        "status", help="Live system status — containers, models, timers, tasks, resources"
    )
    p_status.add_argument("--json", "-j", action="store_true", help="Machine-readable JSON output")

    p_lint = sub.add_parser(
        "lint", help="Check code against enforced rules (naming, status, security)"
    )
    p_lint.add_argument("--json", "-j", action="store_true", help="Machine-readable JSON output")
    p_lint.add_argument(
        "--files", nargs="*", help="Specific files to check (default: all scripts/)"
    )
    p_lint.add_argument("--fix", action="store_true", help="Suggest fixes for violations")

    p_glossary = sub.add_parser("glossary", help="Glossary (SSOT: docs/domain-glossary.yaml)")
    gl_sub = p_glossary.add_subparsers(dest="gl_command")
    gl_sub.add_parser("sync", help="Sync YAML → DB (idempotent upsert)")

    # File management
    p_file = sub.add_parser("file", help="File management (file_registry)")
    file_sub = p_file.add_subparsers(dest="file_command")

    file_find = file_sub.add_parser("find", help="Search files by keyword")
    file_find.add_argument("query", help="Search keyword (description, filename, tags)")
    file_find.add_argument("--limit", "-n", type=int, default=20)

    file_list = file_sub.add_parser("list", help="List recent files")
    file_list.add_argument(
        "--source", "-s", help="Filter by source (telegram_upload, pipeline_output, agent_generate)"
    )
    file_list.add_argument("--limit", "-n", type=int, default=20)

    file_get = file_sub.add_parser("get", help="Show file details by UUID")
    file_get.add_argument("id", help="File UUID")

    file_push = file_sub.add_parser("push", help="Register a local file")
    file_push.add_argument("path", help="Path to file on disk")
    file_push.add_argument(
        "--source",
        "-s",
        default="agent_generate",
        choices=["telegram_upload", "pipeline_output", "agent_generate"],
    )
    file_push.add_argument("--description", "-d", help="File description")
    file_push.add_argument("--tags", help="Comma-separated tags")

    file_del = file_sub.add_parser("delete", help="Delete file from registry")
    file_del.add_argument("id", help="File UUID")
    file_del.add_argument("--remove-local", action="store_true", help="Also delete local file")

    # ── Obs (observations 통합) ──────────────────────────────────
    p_obs = sub.add_parser("obs", help="Observations — 기록 및 검색")
    obs_sub = p_obs.add_subparsers(dest="obs_command")

    obs_search_p = obs_sub.add_parser("search", help="관찰 검색")
    obs_search_p.add_argument(
        "--category", "-c", help="Category filter (insight, decision, test_result, 등)"
    )
    obs_search_p.add_argument(
        "--source", "-s", help="Source filter (hook:PostToolUse, mcp:obs_write, cli:obs, 등)"
    )
    obs_search_p.add_argument("--tags", help='Tags JSON filter — {"domain": ["mcp"]} 형식')
    obs_search_p.add_argument("--query", "-q", help="Observation 텍스트 부분 검색")
    obs_search_p.add_argument("--limit", "-n", type=int, default=10, help="Max results")
    obs_search_p.add_argument("--json", action="store_true", help="JSON output")

    obs_add_p = obs_sub.add_parser("add", help="관찰 기록")
    obs_add_p.add_argument("observation", help="관찰 내용")
    obs_add_p.add_argument("--category", "-c", default="general", help="Category")
    obs_add_p.add_argument("--source", default="cli:obs", help="Source")
    obs_add_p.add_argument("--tags", help='Tags JSON — {"domain": ["search"]} 형식')

    obs_stats_p = obs_sub.add_parser("stats", help="카테고리별 통계")
    obs_stats_p.add_argument("--days", "-d", type=int, default=7, help="조회 기간 (일)")

    # ── Reflex Rules (Pattern 2+4 auto-fix system) ────────────
    p_reflex = sub.add_parser("reflex", help="Reflex rules — auto-fix pattern 관리")
    ref_sub = p_reflex.add_subparsers(dest="ref_command")

    ref_list = ref_sub.add_parser("list", help="List rules")
    ref_list.add_argument(
        "--status", choices=["candidate", "approved", "dormant", "archived"], help="Status filter"
    )
    ref_list.add_argument(
        "--action", choices=["auto_fix", "notify", "escalate"], help="Action type filter"
    )
    ref_list.add_argument("--limit", "-n", type=int, default=20, help="Max results")
    ref_list.add_argument("--json", action="store_true", help="JSON output")

    ref_show = ref_sub.add_parser("show", help="Show a single rule")
    ref_show.add_argument("id", help="Rule UUID")

    ref_add = ref_sub.add_parser("add", help="Add a new rule manually")
    ref_add.add_argument("--description", required=True, help="Rule description")
    ref_add.add_argument("--category", help="Trigger category (observation category)")
    ref_add.add_argument("--source", help="Trigger source (observation source)")
    ref_add.add_argument("--pattern", help="Trigger ILIKE pattern")
    ref_add.add_argument(
        "--min-count", type=int, default=1, help="Minimum trigger count (default 1)"
    )
    ref_add.add_argument(
        "--window-hours", type=int, default=24, help="Trigger window in hours (default 24)"
    )
    ref_add.add_argument(
        "--action",
        choices=["auto_fix", "notify", "escalate"],
        default="notify",
        help="Action type (default notify)",
    )
    ref_add.add_argument(
        "--action-params",
        help='Action params JSON — {"function": "restart_container", "args": {"container_name": "..."}}',
    )

    ref_approve = ref_sub.add_parser("approve", help="Approve a candidate rule → approved")
    ref_approve.add_argument("id", help="Rule UUID")

    ref_reject = ref_sub.add_parser("reject", help="Reject/archive a rule")
    ref_reject.add_argument("id", help="Rule UUID")

    ref_match = ref_sub.add_parser("match", help="Test rule matching against an observation")
    ref_match.add_argument("observation", help="Observation text to match")
    ref_match.add_argument("--category", help="Observation category")
    ref_match.add_argument("--tags", help='Tags JSON — {"domain": ["mcp"]}')

    ref_detect = ref_sub.add_parser("detect", help="Detect patterns from recent observations")
    ref_detect.add_argument("--hours", type=int, default=24, help="Lookback hours (default 24)")
    ref_detect.add_argument(
        "--min-occurrences", type=int, default=3, help="Min occurrences (default 3)"
    )

    ref_sub.add_parser("promote", help="Auto-promote candidate rules to approved")
    ref_sub.add_parser("decay", help="Decay unused rules to dormant")
    ref_sub.add_parser("report", help="Daily rule activity summary")

    # ── Fact (user feedback on NEUTRAL facts) ───────────────────────
    p_fact = sub.add_parser("fact", help="Manage review_facts — user feedback on NEUTRAL facts")
    fact_sub = p_fact.add_subparsers(dest="fact_command")

    fact_list = fact_sub.add_parser("list", help="List facts")
    fact_list.add_argument(
        "--pending", action="store_true", help="Only NEUTRAL facts awaiting user verdict"
    )
    fact_list.add_argument("--limit", "-n", type=int, default=20, help="Max results (default: 20)")

    fact_confirm = fact_sub.add_parser("confirm", help="Set user_verdict=CONFIRM for a fact")
    fact_confirm.add_argument("id", help="Fact UUID")

    fact_reject = fact_sub.add_parser("reject", help="Set user_verdict=REJECT for a fact")
    fact_reject.add_argument("id", help="Fact UUID")

    # ── Watch (Watchdog 통합) ────────────────────────────────────
    p_watch = sub.add_parser("watch", help="서버 감시 — 상태, 알람, pulse 큐, 이벤트 로그")
    watch_sub = p_watch.add_subparsers(dest="watch_command")

    watch_sub.add_parser("status", help="서버 생존 + pulse 큐 + 이벤트 한눈에")
    watch_sub.add_parser("alerts", help="현재 PENDING/HUMAN_REQUIRED pulse 목록")
    p_wp = watch_sub.add_parser("pulses", help="pulse 큐 관리")
    wdp_sub = p_wp.add_subparsers(dest="pulse_command")
    wp_list = wdp_sub.add_parser("list", help="PENDING pulse 목록")
    wp_list.add_argument("--limit", "-n", type=int, default=20)
    wp_create = wdp_sub.add_parser("create", help="새 pulse 생성")
    wp_create.add_argument("instruction", help="작업 지시 내용")
    wp_create.add_argument(
        "--priority",
        "-p",
        default="P1_CONTEXT",
        choices=["P0_HOT_FIX", "P1_CONTEXT", "HUMAN_REQUIRED"],
    )
    wp_create.add_argument("--target-file", "-f", default="", help="대상 파일")
    wp_create.add_argument("--target-test", "-t", default="", help="대상 테스트")
    wp_create.add_argument("--category", "-c", default="", help="분류 태그")
    wp_resolve = wdp_sub.add_parser("resolve", help="pulse 완료 처리")
    wp_resolve.add_argument("pulse_id", help="pulse ID")
    wp_resolve.add_argument("--ignore", action="store_true", help="RESOLVED 대신 IGNORED")
    wp_show = wdp_sub.add_parser("show", help="pulse 상세 정보")
    wp_show.add_argument("pulse_id", help="pulse ID")
    p_wlog = watch_sub.add_parser("log", help="catchdog_events 최근 로그")
    p_wlog.add_argument("--limit", "-n", type=int, default=20)
    p_wlog.add_argument("--component", "-c", default="", help="특정 컴포넌트 필터")

    p_winc = watch_sub.add_parser("incidents", help="watchdog incident 목록 (감지→조치→결과)")
    p_winc.add_argument("--open", action="store_true", help="미해결만")
    p_winc.add_argument("--since", help="기간 (예: '24 hours', '7 days')")
    p_winc.add_argument("--limit", "-n", type=int, default=20)
    p_wishow = watch_sub.add_parser("incident", help="incident 상세 (컨텍스트 포함)")
    p_wishow.add_argument("incident_id", type=int)

    args = parser.parse_args()

    if args.command == "search":
        if args.search_command == "bm25":
            cmd_search_bm25(args)
        elif args.search_command == "hybrid":
            cmd_search_hybrid(args)
        else:
            p_search.print_help()
    elif args.command == "research":
        if args.research_command == "search":
            cmd_research_search(args)
        elif args.research_command == "docs":
            cmd_research_docs(args)
        elif args.research_command == "fetch":
            cmd_research_fetch(args)
        else:
            p_research.print_help()
    elif args.command == "mem-search":
        await cmd_search(args)
    elif args.command == "save":
        await cmd_save(args)
    elif args.command == "recent":
        await cmd_recent(args)
    elif args.command == "worklog":
        if args.wl_command == "add":
            cmd_worklog_add(args)
        elif args.wl_command == "recent":
            cmd_worklog_recent(args)
        elif args.wl_command == "search":
            cmd_worklog_search(args)
        else:
            p_worklog.print_help()
    elif args.command == "activity":
        if args.act_command == "recent":
            cmd_activity_recent(args)
        elif args.act_command == "stats":
            cmd_activity_stats(args)
        elif args.act_command == "add":
            cmd_activity_add(args)
        else:
            p_act.print_help()
    elif args.command == "experiment":
        if args.exp_command == "list":
            cmd_experiment_list(args)
        elif args.exp_command == "compare":
            cmd_experiment_compare(args)
        elif args.exp_command == "active":
            cmd_experiment_active(args)
        elif args.exp_command == "adopt":
            cmd_experiment_adopt(args)
        else:
            p_experiment.print_help()
    elif args.command == "task":
        if args.task_command == "list":
            cmd_task_list(args)
        elif args.task_command == "add":
            cmd_task_add(args)
        elif args.task_command == "update":
            cmd_task_update(args)
        elif args.task_command == "delete":
            cmd_task_delete(args)
        elif args.task_command == "show":
            cmd_task_show(args)
        else:
            p_task.print_help()
    elif args.command == "discussion":
        if args.method in ("drag", "toolmad"):
            cmd_discussion(args)
        else:
            p_disc.print_help()
    elif args.command == "upload":
        cmd_upload(args)
    elif args.command == "extract":
        cmd_extract(args)
    elif args.command == "enrich-consume":
        cmd_enrich_consume(args)
    elif args.command == "auto":
        if args.auto_command == "add":
            cmd_auto_add(args)
        elif args.auto_command == "list":
            cmd_auto_list(args)
        elif args.auto_command == "clear":
            cmd_auto_clear(args)
        else:
            p_auto.print_help()
    elif args.command == "dashboard":
        cmd_dashboard(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "lint":
        cmd_lint(args)
    elif args.command == "glossary":
        if args.gl_command == "sync":
            raise SystemExit(cmd_glossary_sync(args))
        else:
            p_glossary.print_help()
    elif args.command == "file":
        if args.file_command == "find":
            cmd_file_find(args)
        elif args.file_command == "list":
            cmd_file_list(args)
        elif args.file_command == "get":
            cmd_file_get(args)
        elif args.file_command == "push":
            cmd_file_push(args)
        elif args.file_command == "delete":
            cmd_file_delete(args)
        else:
            p_file.print_help()
    elif args.command == "obs":
        if args.obs_command == "search":
            cmd_obs_search(args)
        elif args.obs_command == "add":
            cmd_obs_add(args)
        elif args.obs_command == "stats":
            cmd_obs_stats(args)
        else:
            p_obs.print_help()
    elif args.command == "reflex":
        _cmd_reflex(args, p_reflex)
    elif args.command == "fact":
        if args.fact_command == "list":
            cmd_fact_list(args)
        elif args.fact_command == "confirm":
            cmd_fact_confirm(args)
        elif args.fact_command == "reject":
            cmd_fact_reject(args)
        else:
            p_fact.print_help()
    elif args.command == "watch":
        if args.watch_command == "pulses":
            if args.pulse_command == "list":
                cmd_watch_pulses_list(args)
            elif args.pulse_command == "create":
                cmd_watch_pulse_create(args)
            elif args.pulse_command == "resolve":
                cmd_watch_pulse_resolve(args)
            elif args.pulse_command == "show":
                cmd_watch_pulse_show(args)
            else:
                wdp_sub.print_help()
        elif args.watch_command == "alerts":
            cmd_watch_alerts(args)
        elif args.watch_command == "status":
            cmd_watch_status(args)
        elif args.watch_command == "log":
            cmd_watch_log(args)
        elif args.watch_command == "incidents":
            cmd_watch_incidents(args)
        elif args.watch_command == "incident":
            cmd_watch_incident_show(args)
        else:
            p_watch.print_help()
    elif args.command == "dev":
        if args.dev_command == "poll":
            cmd_dev_poll(args)
        elif args.dev_command == "claim":
            cmd_dev_claim(args)
        elif args.dev_command == "pr":
            cmd_dev_pr(args)
        else:
            p_dev.print_help()
    else:
        parser.print_help()

    if args.command in ("save", "recent", "mem-search"):
        from api.async_pg import close_pool

        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
