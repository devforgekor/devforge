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

from lib.tracking.agent_names import normalize as normalize_agent
from lib.db import psql as _sql, esc_sql
from lib.cli_worklog import cmd_worklog_add, cmd_worklog_recent, cmd_worklog_search
from lib.cli_experiment import cmd_experiment_list, cmd_experiment_compare, cmd_experiment_active, cmd_experiment_adopt


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
    print(f"{'ID':<6} {'Created':<20} {'Type':<10} {'Source':<14} {'Title':<50} {'Sum.Status':<12} {'Q.Status'}")
    print("-" * 130)
    for row in rows:
        if not row.strip():
            continue
        parts = row.split("|", 6)
        if len(parts) >= 7:
            print(f"{parts[0]:<6} {parts[1]:<20} {parts[2]:<10} {parts[3]:<14} {parts[4][:48]:<50} {parts[5]:<12} {parts[6]}")


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
    tags_sql = "ARRAY[" + ",".join(f"'{esc_sql(t.strip())}'" for t in tags.split(",") if t.strip()) + "]" if tags else "'{}'"
    result = _sql(f"""INSERT INTO activity_log (type, source, title, summary, tags, summary_status)
        VALUES ('manual', 'cli', '{title}', '{summary}', {tags_sql}, 'raw') RETURNING id""")
    if result and result.strip():
        print(f"  Added: {title} (id={result.strip()})")
    else:
        print("  Failed to add entry")


def cmd_search_bm25(args):
    """FTS5 BM25 search via local_index."""
    from lib.search.local_index import FTS5Index
    idx = FTS5Index()
    t0 = time.monotonic()
    results = idx.bm25_search(args.query, limit=args.limit)
    elapsed = round(time.monotonic() - t0, 3)

    if args.json:
        print(json.dumps({"results": results, "meta": {"count": len(results), "elapsed_s": elapsed}},
                          ensure_ascii=False, indent=2))
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
    from lib.search.hybrid import hybrid_search, bm25_only
    from lib.text_cleaner import get_cleaner

    # Preprocess query through Kiwi for BM25
    cl = get_cleaner()
    query_terms = " ".join(cl.extract_terms(args.query))
    query_for_embed = args.query

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
        meta = {"mode": "bm25_short_circuit", "bm25_time": bm25_time,
                "bm25_count": len(bm25_list), "short_circuit": True,
                "top1_score": top1_score, "gap": round(top2_score - top1_score, 3)}
        results = [{"turn_id": r["turn_id"], "conversation_id": r["conversation_id"],
                     "created_at": r["created_at"], "agent": r["agent"],
                     "seq": r["seq"],
                     "text_clean": (r.get("text_clean") or "")[:200],
                     "bm25_rank": i, "dense_rank": None, "rrf_score": 0}
                    for i, r in enumerate(bm25_list[:args.limit])]
    else:
        result = hybrid_search(args.query, limit=args.limit)
        results = result["results"]
        meta = result["meta"]
        meta["mode"] = "hybrid_rrf"

        meta["bm25_time"] = bm25_time
        meta["short_circuit"] = False

    if args.json:
        print(json.dumps({"results": results, "meta": meta},
                          ensure_ascii=False, indent=2))
        return

    mode_label = "BM25 SHORT-CIRCUIT" if short_circuited else "HYBRID RRF"
    print(f"{mode_label}: {len(results)} results for '{args.query}'")
    if short_circuited:
        print(f"  (top1 score={top1_score:.2f}, gap={meta.get('gap', 0):.2f} — dense skipped)")
    else:
        if meta.get("embed_error"):
            print(f"  [warn] Dense search: {meta['embed_error']}")
        print(f"  BM25={meta.get('bm25_count', 0)} dense={meta.get('dense_count', 0)} "
              f"bm25_time={meta.get('bm25_time', 0)}s dense_time={meta.get('dense_time', 0)}s")

    print()
    for r in results:
        label = ""
        if r.get("bm25_rank") is not None and r.get("dense_rank") is not None:
            label = f" B{r['bm25_rank']} D{r['dense_rank']}"
        elif r.get("dense_rank") is None:
            label = f" B{r.get('bm25_rank', '?')}"
        score_str = f"rrf={r.get('rrf_score', 0):.4f}" if r.get("rrf_score") else ""
        print(f"  [{r.get('rrf_score', r.get('bm25_score', 0)):.2f}{label}] {r.get('agent', '?')} {r.get('created_at', '')[:19]}")
        txt = (r.get("text_clean") or "")[:160]
        if txt:
            print(f"       {txt}")
        print()


MODE_FILE_A = "/opt/ai_data/scripts/current-mode-pod-a.env"
MODE_FILE_B = "/opt/ai_data/scripts/current-mode-pod-b.env"
SYSTEM_MODE_FILE = "/opt/ai_data/scripts/current-system-mode.env"
MODE_MAP = {
    "day":     ("reserved", "day"),      # Pod A reserved(:8080) + Pod B extractor(:8082)
    "verify":  ("reserved", "verify"),   # Pod B verifier(:8084), Pod A stopped
}

def _switch_mode(mode: str) -> bool:
    """Write mode files and restart containers. mode: day|review|verify."""
    if mode not in MODE_MAP:
        print(f"Unknown mode: {mode}")
        return False

    mode_a, mode_b = MODE_MAP[mode]

    # Write mode files
    for fpath, m in [(MODE_FILE_A, mode_a), (MODE_FILE_B, mode_b)]:
        with open(fpath, "w") as f:
            f.write(f"MODE={m}\n")
    print(f"Switched to {mode} (Pod A: {mode_a}, Pod B: {mode_b})")

    # Restart Pod B
    print("Restarting container-devforge-pod-b (Pod B)...")
    r = subprocess.run(
        ["systemctl", "--user", "restart", "container-devforge-pod-b"],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        print(f"Error restarting container-devforge-pod-b: {r.stderr}")
        return False

    # Handle Pod A
    if mode_a == "verify":
        print("Stopping container-devforge-pod-a (Pod A, not needed in verify)...")
        subprocess.run(
            ["systemctl", "--user", "stop", "container-devforge-pod-a"],
            capture_output=True, text=True, timeout=30,
        )
    else:
        print("Restarting container-devforge-pod-a (Pod A)...")
        r = subprocess.run(
            ["systemctl", "--user", "restart", "container-devforge-pod-a"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            print(f"Warning: container-devforge-pod-a restart: {r.stderr}")

    # Wait for Pod B model to load
    print("Waiting for models to load...")
    for _ in range(120):
        try:
            req = urllib.request.Request("http://127.0.0.1:8084/health")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read())
                    if data.get("status") == "ok":
                        print(f"  Ready: {data.get('slots_idle', '?')} idle / {data.get('slots_processing', '?')} processing")
                        return True
        except Exception:
            pass
        time.sleep(2)
    print("Warning: model health check timed out, may still be loading")
    return True


def cmd_discussion(args):
    """Launch multi-agent LLM debate (DRAG or Tool-MAD)."""
    # ── Night window guard ──────────────────────────────────
    try:
        content = open(SYSTEM_MODE_FILE).read().strip()
        if "MODE=night" in content:
            print("ERROR: nightly pipeline active (MODE=night) — discussion blocked")
            print("  Pod A+B are managed by night_cycle.sh. Retry after KST 07:00.")
            return
    except FileNotFoundError:
        pass

    method = args.method  # "drag" or "toolmad"
    question = getattr(args, "question", None)
    skip_drag = getattr(args, "skip_drag", False)
    dry_run = getattr(args, "dry_run", False)
    with_api = getattr(args, "with_api", False)

    # Ensure container is in review mode (14B for DRAG + verification)
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
        print(f"  DRAG: skipped")
    if dry_run:
        print(f"  Dry-run: enabled")

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
    """Check if devforge-pod-b container is running in review mode (llama-server on :8081)."""
    try:
        r = subprocess.run(
            ["podman", "exec", "devforge-pod-b", "pgrep", "-f", "llama-server.*8081"],
            capture_output=True, text=True, timeout=5,
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

    from lib.blob_uploader import upload_review_bundle, upload_raw

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
            print(f"  Use --file to upload a specific file")
            return
        url = upload_review_bundle(
            content=content,
            pipeline=pipeline,
            session_id=session_id,
            metadata={"title": title} if title else None,
        )
        print(f"Uploaded review-bundle")

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
        mcp_model=getattr(args, "mcp_model", "day_mcp"),
    )
    print(f"  processed: {result['processed']}")
    print(f"  failed:    {result['failed']}")
    print(f"  facts:     {result['facts']}")
    if result.get("elapsed_s"):
        print(f"  elapsed:   {result['elapsed_s']}s")


def cmd_mcp_consume(args):
    """Read and format MCP metadata from review_facts."""
    from lib.mcp_consumer import consume_mcp
    results = consume_mcp(
        limit=getattr(args, "limit", 50),
        dry_run=getattr(args, "dry_run", False),
    )
    print(f"  formatted: {len(results)} MCP items")
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
        " Tasks execute via night_cycle.sh (03:00 KST / 18:00 UTC).",
        " CLI: python3 cli.py auto add \"title\" \"description\"",
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
    print(f"{'Model':<32} {'Total':>5} {'Valid%':>7} {'Halluc%':>8} {'Rate':>6} {'Sec':>5} {'Cache%':>6}")
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
        valid_pct = f"{valid/total*100:.1f}" if total else "-"
        halluc_pct = f"{halluc/total*100:.1f}" if total else "-"
        rate = p[5].strip() or "-"
        secs = p[6].strip() or "-"
        cache = p[7].strip() or "-"
        print(f"{model:<32} {total:>5} {valid_pct:>7} {halluc_pct:>8} {rate:>6} {secs:>5} {cache:>6}")

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
            valid_pct = f"{valid/total*100:.1f}" if total else "-"
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
            print(f"\n총 {total} facts / {turns} turns / {models} models — overall valid {valid/total*100:.1f}%")


# ═══════════════════════════════════════════════════════════════
# status — live system query, single source of truth
# ═══════════════════════════════════════════════════════════════

def _run(cmd, timeout=10):
    """Run a shell command, return (stdout, stderr, returncode)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout.strip(), p.stderr.strip(), p.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return "", str(e), 1


def _get_containers():
    """Query podman for live container status."""
    out, _, rc = _run(["podman", "ps", "--format", "{{.Names}}|{{.Status}}|{{.Ports}}|{{.Image}}"])
    if rc != 0:
        return {"error": out or "podman not available"}
    containers = {}
    for line in out.split("\n"):
        parts = line.split("|", 3)
        if len(parts) < 2:
            continue
        name, status, ports, image = parts[0], parts[1], parts[2] if len(parts) > 2 else "", parts[3] if len(parts) > 3 else ""
        containers[name] = {"status": status, "ports": ports, "image": image.split("/")[-1] if image else ""}
    return containers


def _get_models():
    """Query llama.cpp /v1/models on both pods."""
    import urllib.request
    models = {}
    for label, port in [("pod-a", 8080), ("pod-b", 8082)]:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                models[label] = [m.get("name", m.get("model", "?")) for m in data.get("models", data.get("data", []))]
        except Exception as e:
            models[label] = f"unreachable: {e}"
    return models


def _get_timers():
    """Query systemd user timers. Parse by finding .timer/.service tokens."""
    import re
    out, _, rc = _run(["systemctl", "--user", "list-timers", "--no-pager", "--no-legend"])
    if rc != 0:
        return {"error": out}
    timers = {"active": [], "inactive": [], "other": []}
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        # find timer name — token ending with .timer
        m = re.search(r"(\S+\.timer)\s+\S+\.service", line)
        if not m:
            continue
        timer_name = m.group(1)
        # classify by NEXT column: day-of-week → active, "-" → inactive, "n/a" → other
        if re.match(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s", line):
            timers["active"].append(timer_name)
        elif line.startswith("n/a"):
            timers["inactive"].append(timer_name)
        else:
            timers["other"].append(timer_name)
    return timers


def _get_services():
    """Query systemd user services."""
    out, _, rc = _run(["systemctl", "--user", "list-units", "--type=service", "--no-pager", "--no-legend"])
    if rc != 0:
        return {"error": out}
    services = {}
    for line in out.split("\n"):
        parts = line.split()
        if len(parts) < 4:
            continue
        name = parts[0].replace(".service", "")
        load, active, sub_state = parts[1], parts[2], parts[3]
        desc_start = line.find(parts[3]) + len(parts[3])
        description = line[desc_start:].strip() if desc_start < len(line) else ""
        # only report running/failed services
        if active in ("active", "failed"):
            services[name] = {"state": active, "sub": sub_state, "desc": description}
    return services


def _get_resources():
    """Read /proc for live system resources."""
    resources = {}
    # memory
    out, _, _ = _run(["free", "-h"])
    if out:
        for line in out.split("\n"):
            if line.startswith("Mem:"):
                parts = line.split()
                resources["memory"] = {"total": parts[1], "used": parts[2], "free": parts[3], "available": parts[6]} if len(parts) >= 7 else {}
            elif line.startswith("Swap:"):
                parts = line.split()
                resources["swap"] = {"total": parts[1], "used": parts[2], "free": parts[3]} if len(parts) >= 4 else {}
    # disk
    out, _, _ = _run(["df", "-h", "/", "/opt/ai_data", "/mnt/lv_db", "/mnt/secure_meta", "/var/log", "/var/tmp", "/opt/projects"])
    disks = {}
    if out:
        for line in out.split("\n")[1:]:
            parts = line.split()
            if len(parts) >= 6:
                disks[parts[5]] = {"size": parts[1], "used": parts[2], "avail": parts[3], "use_pct": parts[4]}
    resources["disks"] = disks
    # load
    try:
        with open("/proc/loadavg") as f:
            lavg = f.read().split()
            resources["load"] = {"1min": float(lavg[0]), "5min": float(lavg[1]), "15min": float(lavg[2])}
        with open("/proc/uptime") as f:
            up_sec = float(f.read().split()[0])
            d = int(up_sec) // 86400
            h = (int(up_sec) % 86400) // 3600
            m = (int(up_sec) % 3600) // 60
            resources["uptime"] = f"{d}d {h}h {m}m"
    except Exception:
        pass
    return resources


def _get_experiments():
    """Query experiment_registry from DB."""
    sql = "SELECT experiment_id, category, verdict, substring(rationale,1,100) as excerpt, created_at FROM experiment_registry ORDER BY created_at DESC LIMIT 7"
    out = _sql(sql)
    if not out:
        return []
    exps = []
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        exps.append({"id": parts[0].strip(), "category": parts[1].strip(), "verdict": parts[2].strip(),
                      "excerpt": parts[3].strip(), "created_at": parts[4].strip()})
    return exps


def _get_active_config():
    """Query active_config from DB."""
    sql = "SELECT component, config, rationale FROM active_config"
    out = _sql(sql)
    if not out:
        return []
    configs = []
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        configs.append({"component": parts[0].strip(), "config": parts[1].strip()[:120], "rationale": parts[2].strip()[:120]})
    return configs


def _get_tasks():
    """Query tasks DB table for current task status."""
    from lib.db import psql_json as _pj
    rows = _pj(
        "SELECT id, title, status, priority FROM tasks WHERE status IN ('in_progress', 'pending', 'blocked', 'completed')"
    )
    if not rows:
        return {"error": "tasks table empty"}
    summary = {"in_progress": [], "pending": [], "blocked": [], "completed_count": 0, "total": 0}
    for t in rows:
        summary["total"] += 1
        sid = t.get("id", 0)
        if t["status"] == "in_progress":
            summary["in_progress"].append({"id": sid, "title": t["title"]})
        elif t["status"] == "pending":
            summary["pending"].append({"id": sid, "priority": t.get("priority", ""), "title": t["title"]})
        elif t["status"] == "blocked":
            summary["blocked"].append({"id": sid, "title": t["title"]})
        elif t["status"] == "completed":
            summary["completed_count"] += 1
    return summary


def cmd_task_list(args):
    """List tasks from DB."""
    from lib.db import psql_json as _pj, esc_sql
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
        print(f"{r['id']:<5} {r['status']:<12} {str(r['priority'] or ''):<8} "
              f"{str(r['title'])[:58]:<60} {str(r['created_at'])[:19]}")


def cmd_task_add(args):
    """Add a new task to DB."""
    from lib.db import psql as _sql, esc_sql
    title = esc_sql(args.title)
    priority = args.priority or ''
    desc = esc_sql(args.description or '')
    result = _sql(f"""INSERT INTO tasks (title, priority, description)
    VALUES ('{title}', '{priority}', '{desc}') RETURNING id""")
    if result and result.strip():
        print(f"Task created: id={result.strip()} - {args.title[:60]}")


def cmd_task_update(args):
    """Update task status/notes."""
    from lib.db import psql as _sql, psql_json as _pj, esc_sql
    import json
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
    if args.status == 'completed' or t['status'] != 'completed' and args.status == 'completed':
        update_cols.append("completed_at = NOW()")
    if args.note:
        notes = t.get('notes', [])
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
    if r.get('priority'): print(f"  Priority:  {r['priority']}")
    if r.get('description'):
        print(f"  Description:")
        for line in (r['description'] or '').split('\n'): print(f"    {line}")
    notes = r.get('notes', [])
    if notes and isinstance(notes, list) and len(notes) > 0:
        print(f"  Notes:")
        for n in notes: print(f"    - {n}")
    print(f"  Created:   {r['created_at']}")
    print(f"  Updated:   {r['updated_at']}")
    if r.get('completed_at'): print(f"  Completed: {r['completed_at']}")
    if r.get('agent'): print(f"  Agent:     {r['agent']}")
    if r.get('tags') and isinstance(r['tags'], list) and r['tags']:
        print(f"  Tags:      {', '.join(r['tags'])}")


def _get_alerts(containers, resources):
    """Derive alerts from thresholds."""
    alerts = []
    # container down
    expected = ["postgres", "devforge-pod-a", "devforge-pod-b"]
    for name in expected:
        if name not in containers:
            alerts.append(f"Container {name} is DOWN")
    # disk > 90%
    for mount, info in resources.get("disks", {}).items():
        pct = info.get("use_pct", "0%").replace("%", "")
        try:
            if int(pct) > 90:
                alerts.append(f"Disk {mount} at {pct}%")
        except ValueError:
            pass
    # memory > 95%
    mem = resources.get("memory", {})
    if mem:
        try:
            import re
            used = re.sub(r"[^0-9.]", "", mem.get("used", "0"))
            total = re.sub(r"[^0-9.]", "", mem.get("total", "1"))
            if float(used) / float(total) > 0.95:
                alerts.append(f"Memory {used}/{total}")
        except (ValueError, ZeroDivisionError):
            pass
    return alerts


def _get_rule_status():
    """Run lint_rules and return summary + violation counts."""
    from lint_rules import run_all_checks, find_python_files, SCRIPTS_DIR as LINT_DIR
    try:
        result = run_all_checks(find_python_files(LINT_DIR))
        return {
            "passed": result["passed"],
            "status": result["status"],
            "files_checked": result["total_files"],
            "violations": result["violations_by_severity"],
            # Only include actual violations for P0 (show-stoppers)
            "p0_violations": [
                {"file": v["file"], "line": v.get("line", ""), "message": v["message"]}
                for v in result["violations"] if v["severity"] == "P0"
            ][:10],  # cap at 10 to avoid bloat
        }
    except Exception as e:
        return {"error": str(e)}


def _get_glossary():
    """Return glossary terms with bounded context names."""
    from lib.db import psql_json as _pj
    return _pj(
        "SELECT gt.term, gt.definition, bc.name as context "
        "FROM glossary_terms gt LEFT JOIN bounded_contexts bc ON gt.bounded_context_id = bc.id "
        "ORDER BY bc.id, gt.term"
    )


def _get_references():
    """Return static references grouped by category."""
    from lib.db import psql_json as _pj
    return _pj(
        "SELECT category, name, url, description FROM static_references ORDER BY category, name"
    )


def cmd_status(args):
    """Live system status — single source of truth for LLM and humans."""
    containers = _get_containers()
    models = _get_models()
    resources = _get_resources()

    result = {
        "host": {"hostname": os.uname().nodename, "arch": os.uname().machine,
                  "os": f"{os.uname().sysname} {os.uname().release}"},
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
        print(f"Host: {result['host']['hostname']} ({result['host']['arch']}) — {resources.get('uptime', '?')} up")
        print(f"Load: {resources.get('load', {}).get('1min', '?')} {resources.get('load', {}).get('5min', '?')} {resources.get('load', {}).get('15min', '?')}")
        mem = resources.get("memory", {})
        print(f"Mem: {mem.get('used', '?')}/{mem.get('total', '?')} (avail {mem.get('available', '?')})")
        swap = resources.get("swap", {})
        if swap:
            print(f"Swap: {swap.get('used', '?')}/{swap.get('total', '?')}")

        print(f"\n── Containers ──")
        for name, info in containers.items():
            print(f"  {name}: {info['status']}")

        print(f"\n── Models ──")
        for pod, model_list in models.items():
            if isinstance(model_list, list):
                print(f"  {pod} ({' '.join(model_list)})")
            else:
                print(f"  {pod}: {model_list}")

        tasks = result.get("tasks", {})
        if isinstance(tasks, dict) and "error" not in tasks:
            print(f"\n── Tasks ──")
            print(f"  in_progress: {len(tasks.get('in_progress', []))}")
            print(f"  pending: {len(tasks.get('pending', []))}")
            print(f"  blocked: {len(tasks.get('blocked', []))}")
            print(f"  completed: {tasks.get('completed_count', 0)} / {tasks.get('total', 0)}")

        alerts = result.get("alerts", [])
        if alerts:
            print(f"\n── Alerts ──")
            for a in alerts:
                print(f"  ⚠ {a}")

        print(f"\nUse --json for machine-readable output.")


def cmd_glossary_sync(args):
    """Sync docs/domain-glossary.yaml → DB glossary_terms (idempotent upsert).

    YAML is the single source of truth. This is the ONLY write path to DB.
    """
    import yaml

    from lib.db import psql_ok as _ok, esc_sql as _esc

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
        sql = (f"INSERT INTO bounded_contexts (id, name) VALUES ({bc_id}, '{bc_name}') "
               f"ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name")
        if _ok(sql):
            ctx_count += 1
        else:
            errors.append(f"context {bc['name']}")

        for term_entry in bc.get("terms", []):
            term = _esc(term_entry["term"])
            definition = _esc(term_entry["definition"])
            tables = term_entry.get("tables", [])
            files = term_entry.get("related_files", [])
            tables_pg = "'{}'::text[]" if not tables else "ARRAY[" + ", ".join(f"'{_esc(t)}'" for t in tables) + "]"
            files_pg = "'{}'::text[]" if not files else "ARRAY[" + ", ".join(f"'{_esc(f)}'" for f in files) + "]"

            sql = (f"INSERT INTO glossary_terms (term, definition, bounded_context_id, tables_ref, related_files) "
                   f"VALUES ('{term}', '{definition}', {bc_id}, {tables_pg}, {files_pg}) "
                   f"ON CONFLICT (term, bounded_context_id) DO UPDATE SET "
                   f"definition = EXCLUDED.definition, tables_ref = EXCLUDED.tables_ref, "
                   f"related_files = EXCLUDED.related_files")
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
    from lint_rules import run_all_checks, find_python_files, SCRIPTS_DIR

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
            print(f"  P0:{result['violations_by_severity']['P0']} P1:{result['violations_by_severity']['P1']} P2:{result['violations_by_severity']['P2']}")

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

    p_search = sub.add_parser("search", help="Search memories")
    p_search.add_argument("query")
    p_search.add_argument("--source", "-s", help="Filter by source (e.g., claude, copilot, gemini)")
    p_search.add_argument("--limit", "-n", type=int, default=20)
    p_search.add_argument("--json", "-j", action="store_true")

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
    drag_p = disc_sub.add_parser("drag", help="DRAG: 2-stage debate — query consensus → fetch → synthesize")
    drag_p.add_argument("question", nargs="?", help="Debate topic / question")
    drag_p.add_argument("--skip-drag", action="store_true", help="Skip Round 0 DRAG query consensus")
    drag_p.add_argument("--dry-run", action="store_true", help="Simulate without LLM calls")
    drag_p.add_argument("--with-api", action="store_true", help="Use DeepSeek API for real search (internet knowledge)")
    tmad_p = disc_sub.add_parser("toolmad", help="Tool-MAD: adaptive real-time search during debate rounds")
    tmad_p.add_argument("question", nargs="?", help="Debate topic / question")
    tmad_p.add_argument("--skip-drag", action="store_true", help="Skip Round 0 DRAG query consensus")
    tmad_p.add_argument("--dry-run", action="store_true", help="Simulate without LLM calls")
    tmad_p.add_argument("--with-api", action="store_true", help="Use DeepSeek API for real search (internet knowledge)")

    sub.add_parser("dashboard", help="Model performance dashboard")

    p_extract = sub.add_parser("extract", help="Run extract pipeline on conversation turns")
    p_extract.add_argument("--turn-id", help="Process a specific turn UUID")
    p_extract.add_argument("--limit", "-n", type=int, default=100, help="Max turns to process")
    p_extract.add_argument("--dry-run", action="store_true", help="Simulate without DB writes")
    p_extract.add_argument("--mcp-model", default="day_mcp",
                           help="Model for MCP fields generation (default: day_mcp)")

    p_mcp = sub.add_parser("mcp-consume", help="Format MCP metadata from review_facts for MCP tools")
    p_mcp.add_argument("--limit", "-n", type=int, default=50)
    p_mcp.add_argument("--dry-run", action="store_true", help="Read only, no verdict update")
    p_mcp.add_argument("--json", action="store_true", help="JSON output")

    p_upload = sub.add_parser("upload", help="Upload pipeline result to Azure Blob")
    p_upload.add_argument("--pipeline", "-p", required=True,
                          choices=["debate", "code_mod", "extract"],
                          help="Pipeline name")
    p_upload.add_argument("--session-id", "-s", required=True,
                          help="Session identifier")
    p_upload.add_argument("--file", "-f",
                          help="Raw file path to upload (skip review-bundle wrapping)")
    p_upload.add_argument("--title", "-t",
                          help="Bundle title (for review-bundle mode)")

    p_auto = sub.add_parser("auto", help="Auto mode task management")
    auto_sub = p_auto.add_subparsers(dest="auto_command")

    auto_add = auto_sub.add_parser("add", help="Add a task to auto mode")
    auto_add.add_argument("title", help="Task title (## heading)")
    auto_add.add_argument("description", help="Task description / prompt for Claude Code")

    auto_list = auto_sub.add_parser("list", help="List scheduled auto tasks")

    auto_clear = auto_sub.add_parser("clear", help="Clear all auto tasks")

    p_experiment = sub.add_parser("experiment", help="실험 레지스트리 관리")
    exp_sub = p_experiment.add_subparsers(dest="exp_command")

    exp_list = exp_sub.add_parser("list", help="List experiments")
    exp_list.add_argument("--all", action="store_true", help="Show all experiments")
    exp_list.add_argument("--category", "-c", help="Filter by category")
    exp_list.add_argument("--limit", "-n", type=int, default=20, help="Max results (default: 20)")

    exp_compare = exp_sub.add_parser("compare", help="Compare experiments")
    exp_compare.add_argument("experiment_ids", nargs="+", help="Experiment IDs to compare")

    exp_active = exp_sub.add_parser("active", help="Show active config")

    exp_adopt = exp_sub.add_parser("adopt", help="Adopt experiment as active config")
    exp_adopt.add_argument("experiment_id", help="Experiment ID to adopt")
    exp_adopt.add_argument("--component", "-c", required=True,
                           choices=["pod-a-day", "pod-b-day", "pod-b-night"],
                           help="Component to update")

    p_task = sub.add_parser("task", help="Task management (DB)")
    task_sub = p_task.add_subparsers(dest="task_command")

    task_list = task_sub.add_parser("list", help="List tasks")
    task_list.add_argument("--status", choices=["pending", "in_progress", "blocked", "completed", "deleted"])

    task_add = task_sub.add_parser("add", help="Add a new task")
    task_add.add_argument("title", help="Task title")
    task_add.add_argument("--priority", "-p", choices=["P0", "P1", "P2"])
    task_add.add_argument("--description", "-d")

    task_update = task_sub.add_parser("update", help="Update a task")
    task_update.add_argument("id", type=int, help="Task ID")
    task_update.add_argument("--status", choices=["pending", "in_progress", "blocked", "completed", "deleted"])
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

    act_stats = act_sub.add_parser("stats", help="Activity log statistics")

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
    p_hybrid.add_argument("--no-short-circuit", action="store_true",
                           help="BM25 dominant여도 항상 Dense 실행")

    p_status = sub.add_parser("status", help="Live system status — containers, models, timers, tasks, resources")
    p_status.add_argument("--json", "-j", action="store_true", help="Machine-readable JSON output")

    p_lint = sub.add_parser("lint", help="Check code against enforced rules (naming, status, security)")
    p_lint.add_argument("--json", "-j", action="store_true", help="Machine-readable JSON output")
    p_lint.add_argument("--files", nargs="*", help="Specific files to check (default: all scripts/)")
    p_lint.add_argument("--fix", action="store_true", help="Suggest fixes for violations")

    p_glossary = sub.add_parser("glossary", help="Glossary (SSOT: docs/domain-glossary.yaml)")
    gl_sub = p_glossary.add_subparsers(dest="gl_command")
    gl_sync = gl_sub.add_parser("sync", help="Sync YAML → DB (idempotent upsert)")

    # File management
    p_file = sub.add_parser("file", help="File management (file_registry)")
    file_sub = p_file.add_subparsers(dest="file_command")

    file_find = file_sub.add_parser("find", help="Search files by keyword")
    file_find.add_argument("query", help="Search keyword (description, filename, tags)")
    file_find.add_argument("--limit", "-n", type=int, default=20)

    file_list = file_sub.add_parser("list", help="List recent files")
    file_list.add_argument("--source", "-s", help="Filter by source (telegram_upload, pipeline_output, agent_generate)")
    file_list.add_argument("--limit", "-n", type=int, default=20)

    file_get = file_sub.add_parser("get", help="Show file details by UUID")
    file_get.add_argument("id", help="File UUID")

    file_push = file_sub.add_parser("push", help="Register a local file")
    file_push.add_argument("path", help="Path to file on disk")
    file_push.add_argument("--source", "-s", default="agent_generate",
                          choices=["telegram_upload", "pipeline_output", "agent_generate"])
    file_push.add_argument("--description", "-d", help="File description")
    file_push.add_argument("--tags", help="Comma-separated tags")

    file_del = file_sub.add_parser("delete", help="Delete file from registry")
    file_del.add_argument("id", help="File UUID")
    file_del.add_argument("--remove-local", action="store_true", help="Also delete local file")

    args = parser.parse_args()

    if args.command == "search":
        if args.search_command == "bm25":
            cmd_search_bm25(args)
        elif args.search_command == "hybrid":
            cmd_search_hybrid(args)
        else:
            p_search.print_help()
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
    elif args.command == "mcp-consume":
        cmd_mcp_consume(args)
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
    else:
        parser.print_help()

    if args.command in ("save", "recent"):
        from api.async_pg import close_pool
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
