#!/usr/bin/env python3
"""code_mod_pipeline.py — Qwen32B 4-stage code modification pipeline.

SLOC-exempt: 852 lines — single cohesive 4-stage pipeline (ANALYZE→PLAN→IMPLEMENT
→PACKAGE). Each stage shares RateEstimator, prompt templates, LLM client, Slack
notifier, and activity logger. Splitting would scatter shared state across files.

4-stage pipeline (32B local llama.cpp):
  Stage 1: ANALYZE   — identify affected sections, change type, dependencies
  Stage 2: PLAN      — design minimal change approach, estimate impact
  Stage 3: IMPLEMENT — produce unified diff
  Stage 4: PACKAGE   — JSON package for downstream API review

Each stage result is recorded in activity_log (type='stage') for traceability.

Usage:
  python3 code_mod_pipeline.py                    # run all tasks
  python3 code_mod_pipeline.py --task 1           # single task
  python3 code_mod_pipeline.py --local-only       # 32B only
  python3 code_mod_pipeline.py --api-only         # APIs only
  python3 code_mod_pipeline.py --report           # generate comparison report
  python3 code_mod_pipeline.py --start-stage 3 --resume-from result.json  # resume
"""
import glob
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from lib.llm.client import call_llm
from lib.llm.rate_estimator import PromptCompletionRateEstimator as RateEstimator
from lib.db import esc_sql
from lib.code_mod.shared import (
    read_file, extract_json_from_llm_response, save_result,
    TASKS_FILE, OUTPUT_DIR, DEEPSEEK_KEY, LLAMA_ENDPOINT,
)
SLACK_BOT_TOKEN = "xoxb-10781519811159-11168454462293-A9nR8gdlZiPkrSwdE656CAHK"
SLACK_CHANNEL = "U0APJGD8CBW"  # DM
# token-based dynamic timeout parameters
# Formula: timeout = (prompt_tokens / prompt_eval_rate) + (max_tokens / GEN_RATE) + BUFFER
# 32B IQ4_XS on ARM CPU benchmarked at:
#   - prompt eval:  ~3-5 tok/s (CPU prefill, degrades with longer ctx)
#   - generation:   ~2.15 tok/s (measured from prior runs)
GEN_RATE = 2.15          # tokens/sec for generation (stable)
TIMEOUT_BUFFER = 120     # extra seconds for network/overhead

STAGE_MAX_TOKENS = {1: 2048, 2: 3072, 3: 3072, 4: 2048}

def calc_timeout_v2(estimator: RateEstimator, prompt_tokens: int,
                     max_tokens: int) -> int:
    """RateEstimator의 실측 rate로 timeout 계산. self-calibrating."""
    rate = estimator.median_prompt_rate()
    prompt_time = prompt_tokens / rate if rate > 0 else prompt_tokens / 2.0
    gen_time = max_tokens / estimator.gen_rate if estimator.gen_rate > 0 else max_tokens / GEN_RATE
    return int(prompt_time + gen_time + TIMEOUT_BUFFER)


def estimate_prompt_tokens(code: str, task_desc: str, stage: int,
                           stage1_body: dict = None,
                           stage2_body: dict = None,
                           stage3_body: dict = None,
                           sliced_code: str = "") -> int:
    """Estimate prompt tokens for each stage before execution.
    Uses char/3.5 heuristic (Python code ≈ 3.5-3.7 chars/token, verified against
    32B actual prompt_tokens from activity_log: 4634 actual vs 5365 estimated).
    Stages 2-3 use sliced_code (affected sections only)."""
    def tok(s: str) -> int:
        return int(len(s) / 3.5)
    code_tokens = tok(code)
    sliced_tokens = tok(sliced_code) if sliced_code else code_tokens
    task_tokens = tok(task_desc)

    if stage == 1:
        return code_tokens + task_tokens + 300
    elif stage == 2:
        analysis_tokens = tok(json.dumps(stage1_body or {}))
        return sliced_tokens + task_tokens + analysis_tokens + 280
    elif stage == 3:
        plan_tokens = tok(json.dumps(stage2_body or {}))
        return sliced_tokens + plan_tokens + 230
    elif stage == 4:
        diff_text = ""
        if isinstance(stage3_body, dict):
            diff_text = stage3_body.get("text", str(stage3_body))
        elif isinstance(stage3_body, str):
            diff_text = stage3_body
        diff_tokens = tok(diff_text)
        return diff_tokens + 230
    return 1000

SYSTEM_32B = (
    "You are a CODE PREPROCESSOR. Your output is NOT final — it will be reviewed "
    "by a senior engineer via API. Your job: produce minimal, surgical changes. "
    "When unsure, annotate with [REVIEW] instead of guessing."
)

STAGE1_ANALYZE = """You are in STAGE 1: ANALYZE.
Read the code and the modification request.
Identify affected lines/functions and cross-dependencies.

CODE:
{code}

REQUEST:
{task}

Output JSON:
{{"affected_sections": ["func_name:line_range", ...],
  "change_type": "bug_fix|refactor|new_feature|structural",
  "dependencies": ["func_name", ...]}}"""

STAGE2_PLAN = """Stage 2: PLAN. Based on the analysis, design the minimal change.

ANALYSIS:
{analysis}

AFFECTED CODE (only the sections that need changes):
{code}

REQUEST:
{task}

{feedback}
Output JSON:
{{"approach": "...",
  "steps": ["step1", ...],
  "files_to_modify": ["file_path"],
  "estimated_lines_changed": {{"added": N, "removed": M}},
  "backward_compatible": true|false,
  "edge_cases": ["case1", ...]}}"""

STAGE3_IMPLEMENT = """Stage 3: IMPLEMENT. Produce a unified diff. Minimal and surgical.

PLAN:
{plan}

AFFECTED CODE:
{code}

Output: unified diff only. --- / +++ headers, context lines."""

STAGE4_PACKAGE = """Stage 4: PACKAGE. Format the diff for API review.

DIFF:
{diff}

Output JSON:
{{"task": "<one-line summary>",
  "analysis": "<brief analysis>",
  "diff": "<escaped unified diff>",
  "rationale": ["per-hunk reason", ...],
  "confidence": 0.0-1.0,
  "review_points": ["verification items", ...]}}"""

# Prompts for analysis-only tasks (expected_category=analysis_only).
# Stages 1-2 run normally (analyze + plan). Stages 3-4 switch to these.
STAGE3_ANALYSIS_REPORT = """Stage 3: ANALYSIS REPORT. Based on the plan, produce a detailed
analysis report. Do NOT produce code changes — this is analysis-only.

PLAN:
{plan}

CODE (for reference):
{code}

Output JSON:
{{"issues_found": [{{"severity": "critical|high|medium|low",
                      "description": "...",
                      "location": "func_name:line_range"}}, ...],
  "root_causes": ["cause1", ...],
  "recommendations": [{{"action": "...",
                          "effort": "small|medium|large",
                          "risk": "low|medium|high",
                          "rationale": "..."}}, ...],
  "priority_order": ["most-urgent", "next", ..., "lowest"]}}"""

STAGE4_ANALYSIS_PACKAGE = """Stage 4: PACKAGE ANALYSIS. Format the analysis report for review.

ANALYSIS RESULT:
{analysis}

Output JSON:
{{"task": "<one-line summary>",
  "analysis_type": "code_review|security_audit|race_condition|architecture_review",
  "summary": "<executive summary in 2-3 sentences>",
  "critical_issues": N,
  "total_issues": N,
  "top_recommendations": ["rec1", "rec2", "rec3"],
  "confidence": 0.0-1.0}}"""

API_PROMPT = """You are a senior code reviewer. Given a code file and modification request,
produce the change as a structured JSON output.

CODE:
{code}

REQUEST:
{task}

Output format (JSON):
{{"task": "<one-line summary>",
  "analysis": "<what needs to change and why>",
  "diff": "<unified diff>",
  "rationale": ["per-hunk reason", ...],
  "confidence": 0.0-1.0,
  "review_points": ["verification items", ...],
  "model": "<your model name>",
  "tokens_used": {{"prompt": N, "completion": M}}}}"""


def load_tasks() -> dict:
    with open(TASKS_FILE) as f:
        return yaml.safe_load(f)


def _system_info() -> str:
    """Read /proc/meminfo for lightweight memory snapshot. Returns short string."""
    try:
        d = {}
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith(("MemTotal:", "MemFree:", "MemAvailable:",
                                    "SwapTotal:", "SwapFree:")):
                    key, val = line.split(":", 1)
                    d[key.strip()] = int(val.strip().split()[0]) // 1024
        mem_total = d.get("MemTotal", 0)
        mem_free = d.get("MemFree", 0)
        mem_avail = d.get("MemAvailable", 0)
        swap_total = d.get("SwapTotal", 0)
        swap_free = d.get("SwapFree", 0)
        swap_used = swap_total - swap_free if swap_total > 0 else 0
        return (f"mem: {mem_free}M free / {mem_avail}M avail ({mem_total}M total)"
                f" | swap: {swap_used}M / {swap_total}M")
    except Exception:
        return "mem: n/a"


def _get_mem_available() -> int:
    """Return MemAvailable in MB."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split(":")[1].strip().split()[0]) // 1024
    except Exception:
        pass
    return 0


def _evict_page_cache(file_path: str) -> bool:
    """Evict a file's pages from kernel page cache via posix_fadvise(DONTNEED)."""
    if not os.path.isfile(file_path):
        return False
    try:
        fd = os.open(file_path, os.O_RDONLY)
        try:
            size = os.fstat(fd).st_size
            os.posix_fadvise(fd, 0, size, os.POSIX_FADV_DONTNEED)
            return True
        finally:
            os.close(fd)
    except Exception:
        return False


def _pre_task_memory_check(task_id: int, warn_mb: int = 1500,
                            critical_mb: int = 400,
                            keep_model: str = "Qwen2.5-Coder-32B-Instruct-IQ4_XS.gguf") -> bool:
    """Check available memory before a task. Evict unused model page cache if tight.

    Returns True if safe to proceed, False only if critically low (<critical_mb).
    Keeps the actively-loaded model's page cache (skip_model) to avoid refault latency.
    KV cache (llama.cpp --cache-ram) is in anonymous process memory — unaffected.
    """
    avail = _get_mem_available()
    if avail < warn_mb:
        print(f"\n[memory] Tight: {avail}MB available before Task {task_id} "
              f"(warn={warn_mb}MB) — evicting unused page cache...", flush=True)
        models_dir = "/opt/ai_data/models/gguf"
        if os.path.isdir(models_dir):
            for f in sorted(os.listdir(models_dir)):
                if f.endswith(".gguf") and f != keep_model:
                    path = os.path.join(models_dir, f)
                    _evict_page_cache(path)
        avail = _get_mem_available()
        if avail >= warn_mb:
            print(f"[memory] Freed enough: {avail}MB available", flush=True)
        elif avail < critical_mb:
            print(f"[memory] CRITICAL: only {avail}MB after eviction "
                  f"(critical={critical_mb}MB) — skipping Task {task_id}", flush=True)
            return False
        else:
            print(f"[memory] Improved to {avail}MB (below warn but above critical) — "
                  f"proceeding", flush=True)
    return True


def _notify_slack(text: str) -> None:
    """Send Slack DM notification. Non-blocking — failures are silent."""
    try:
        payload = json.dumps({"channel": SLACK_CHANNEL, "text": text}).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=payload,
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                     "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _read_previous_feedback(task_id: int) -> str:
    """Read prior pipeline results from DB, extract key fields, cap at 600 chars.

    Returns empty string if no prior data. Otherwise returns a compact
    feedback line for injection into Stage 2 PLAN.
    Uses the Recovery Ladder pattern: json.loads → json_repair → raw fallback.
    """
    import subprocess

    PSQL_TAB = ["podman", "exec", "-i", "postgres", "psql",
                "-U", "postgres", "-d", "devforge_app",
                "--no-align", "--tuples-only", "--quiet",
                "--field-separator=\t"]
    tid = esc_sql(str(task_id))
    snippets = []

    # Prior pipeline result (most recent completed Stage 4)
    try:
        r = subprocess.run(PSQL_TAB + ["-c",
            f"SELECT left(body::text, 3000) FROM activity_log "
            f"WHERE run_id LIKE 'task{tid}\\_%' AND type='stage' "
            f"AND summary LIKE '%Stage 4%' AND summary LIKE '%status=200%' "
            f"ORDER BY id DESC LIMIT 1"],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            raw_body = r.stdout.strip()

            # Recovery Ladder: Rung 1 → Rung 2 → Rung 3 (raw fallback)
            body_json = _parse_llm_json(raw_body)
            if body_json and isinstance(body_json, dict):
                analysis = body_json.get("analysis", "")
                if analysis:
                    snippets.append(f"prev_analysis: {analysis[:200]}")
                rationale = body_json.get("rationale", [])
                if isinstance(rationale, list) and rationale:
                    snippets.append(f"prev_rationale: {'; '.join(rationale)[:200]}")
                review = body_json.get("review_points", [])
                if isinstance(review, list) and review:
                    snippets.append(f"prev_review: {'; '.join(review)[:200]}")
            elif not body_json:
                # Rung 3: raw fallback — tracebacks have the useful info at the tail
                clean = raw_body.strip()[-400:]
                if clean:
                    snippets.append(f"prev_result: {clean}")
    except Exception:
        pass

    if not snippets:
        return ""

    # Build compact feedback, hard cap at 600 chars
    combined = " | ".join(snippets)
    if len(combined) > 600:
        combined = combined[:597] + "..."
    return f"PREVIOUS ATTEMPT: {combined}"


def _parse_llm_json(text: str):
    """Thin wrapper — delegates to shared Recovery Ladder in lib.llm.json_parser."""
    from lib.llm.json_parser import parse_llm_json
    return parse_llm_json(text)


def _slice_code(code: str, affected_sections: list) -> str:
    """Extract only affected function/line ranges from code.

    affected_sections format from Stage 1: ["func_name:line_range", ...]
    line_range examples: "10-25", "10", "func_name:10-25"

    Returns sliced code string with # --- section markers.
    """
    if not affected_sections:
        return code  # fallback: full file

    lines = code.split("\n")
    max_line = len(lines)

    # Parse line ranges from affected_sections
    ranges = []
    for entry in affected_sections:
        # Extract line range (last colon-separated segment with numbers)
        parts = entry.split(":")
        for part in reversed(parts):
            part = part.strip()
            if "-" in part:
                try:
                    a, b = part.split("-", 1)
                    start, end = int(a.strip()), int(b.strip())
                    # Add context padding: 3 lines before, 5 after to capture def/class headers
                    ranges.append((max(1, start - 3), min(max_line, end + 5)))
                    break
                except ValueError:
                    continue
            elif part.isdigit():
                try:
                    n = int(part)
                    ranges.append((max(1, n - 5), min(max_line, n + 5)))
                    break
                except ValueError:
                    continue
        else:
            # No line range found — grep for function name
            for p in parts[:2]:  # first 2 segments may be func name
                p = p.strip()
                if p and not p.isdigit():
                    for i, line in enumerate(lines, 1):
                        if p in line and ("def " in line or "class " in line):
                            # Include until next def/class or +-20 lines
                            end = min(max_line, i + 20)
                            ranges.append((max(1, i - 3), end))
                            break
                    break

    if not ranges:
        print(f"  [WARN] _slice_code could not parse any line ranges from "
              f"affected_sections={affected_sections[:3]} — using full code",
              flush=True)
        return code  # fallback: couldn't parse

    # Merge overlapping ranges
    ranges.sort()
    merged = [ranges[0]]
    for r in ranges[1:]:
        if r[0] <= merged[-1][1] + 3:
            merged[-1] = (merged[-1][0], max(merged[-1][1], r[1]))
        else:
            merged.append(r)

    # Build sliced output
    chunks = []
    for start, end in merged:
        chunk = "\n".join(lines[start - 1:end])
        chunks.append(f"# --- lines {start}-{end} ---\n{chunk}")

    return "\n\n".join(chunks)


def _call_stage_with_retry(endpoint: str, messages: list, model: str,
                           timeout: int, max_tokens: int,
                           max_retries: int = 2,
                           api_key: str = "") -> Tuple[int, dict, float, bool]:
    """Stage 호출 + connection drop 시 재시도 (prompt cache 활용).

    32B ARM CPU에서 prompt eval이 38분+ 걸릴 수 있음.
    TCP keepalive로 대부분 해결되지만, 만약 connection이 끊기면:
    - 서버는 계속 처리 중 (prompt cache에 저장됨)
    - 대기 후 재시도하면 cache hit으로 빠르게 완료

    api_key가 제공되면 Authorization 헤더를 추가 (DeepSeek 등 원격 API).
    """
    total_elapsed = 0.0
    was_retry = False
    for attempt in range(max_retries + 1):
        t0 = time.monotonic()
        status, body = call_llm(endpoint, messages, model=model,
                                timeout=timeout, max_tokens=max_tokens,
                                api_key=api_key)
        elapsed = time.monotonic() - t0
        total_elapsed += elapsed
        if status == 200:
            return status, body, total_elapsed, was_retry
        err_msg = body.get("error", "")
        is_connection_drop = any(s in err_msg for s in
            ("RemoteDisconnected", "Remote end closed", "ConnectionReset",
             "BrokenPipe", "timed out", "Connection refused"))
        is_model_loading = any(s in err_msg for s in
            ("Loading model", "unavailable", "503"))
        if attempt < max_retries and (is_connection_drop or is_model_loading):
            wait = min(60, 15 * (attempt + 1))
            reason = "model loading" if is_model_loading else "connection drop"
            print(f"  [retry] {reason} after {elapsed:.0f}s, "
                  f"waiting {wait}s... (attempt {attempt+1}/{max_retries})",
                  flush=True)
            time.sleep(wait)
            was_retry = True
            continue
        break
    return status, body, total_elapsed, was_retry


STAGE_NAMES = {1: "ANALYZE", 2: "PLAN   ", 3: "IMPL   ", 4: "PACKAGE"}
STAGE_NAMES_ANALYSIS = {1: "ANALYZE", 2: "PLAN   ", 3: "REPORT ", 4: "SUMMARY"}


def _build_stage_user_msg(stage_num: int, code: str, task_desc: str,
                          s1_body: dict, s2_body: dict, s3_body: dict,
                          sliced_code: str, analysis_only: bool = False,
                          feedback: str = "") -> str:
    """Build the user message for a pipeline stage.

    Stage 1: no feedback (unbiased structural analysis).
    Stage 2: feedback injected for informed planning.
    Stages 3-4: no feedback (execution stages).
    """
    if stage_num == 1:
        nonce = f"[cache:{time.time():.6f}]"
        return nonce + "\n" + STAGE1_ANALYZE.format(code=code, task=task_desc)
    elif stage_num == 2:
        fb = f"PREVIOUS ATTEMPT NOTES:\n{feedback}\n\n" if feedback else ""
        return STAGE2_PLAN.format(
            analysis=json.dumps(s1_body, indent=2), code=sliced_code, task=task_desc,
            feedback=fb)
    elif stage_num == 3:
        if analysis_only:
            plan = json.dumps(s2_body, indent=2) if s2_body else "{}"
            return STAGE3_ANALYSIS_REPORT.format(plan=plan, code=code)
        plan = json.dumps(s2_body, indent=2) if s2_body else "{}"
        return STAGE3_IMPLEMENT.format(plan=plan, code=sliced_code)
    else:  # stage 4
        if analysis_only:
            analysis = json.dumps(s3_body, indent=2) if s3_body else "{}"
            return STAGE4_ANALYSIS_PACKAGE.format(analysis=analysis)
        if isinstance(s3_body, dict):
            diff_text = s3_body.get("text", "") or json.dumps(s3_body)
        else:
            diff_text = str(s3_body) if s3_body else ""
        return STAGE4_PACKAGE.format(diff=diff_text)


def _estimate_stage_tokens(stage_num: int, code: str, task_desc: str,
                           s1_body: dict, s2_body: dict, s3_body: dict,
                           sliced_code: str) -> int:
    """Estimate prompt tokens for a pipeline stage."""
    kwargs = {}
    if stage_num >= 2:
        kwargs["stage1_body"] = s1_body
        kwargs["sliced_code"] = sliced_code
    if stage_num >= 3:
        kwargs["stage2_body"] = s2_body
    if stage_num >= 4:
        kwargs["stage3_body"] = s3_body
    return estimate_prompt_tokens(code, task_desc, stage_num, **kwargs)


def _stage_label(stage_num: int, prompt_tokens: int, timeout: int,
                 sliced_tokens: int, total_tokens: int) -> str:
    """Build the 'Stage N/4 NAME — timeout=...' print label."""
    name = STAGE_NAMES[stage_num]
    base = f"  Stage {stage_num}/4 {name} — timeout={timeout}s, ~{prompt_tokens} prompt tokens"
    if stage_num == 2:
        return f"{base} (sliced {sliced_tokens}/{total_tokens} tok)"
    elif stage_num == 3:
        return f"{base} (sliced {sliced_tokens} tok)"
    return base


def run_32b_4stage(task: dict, start_stage: int = 1,
                   resume_from: dict = None,
                   with_api: bool = False) -> dict:
    """Run a single task through Qwen32B 4-stage pipeline with dynamic timeouts.
    start_stage: 1-4, skip earlier stages if resume_from provided.
    with_api: use DeepSeek API for Stage 2 PLAN (internet knowledge)."""
    code = read_file(task["file"])
    task_desc = task["description"]
    code_tokens = int(len(code) / 3.5)
    task_tokens = int(len(task_desc) / 3.5)

    sinfo = _system_info()
    _notify_slack(
        f"T{task['id']} [{task['name']}] 파이프라인 시작 (Stage {start_stage}/4)\n"
        f"  code: ~{code_tokens} tok, task: ~{task_tokens} tok | file: {task['file']}\n"
        f"  initial rate: prompt=~2.0 gen=~{GEN_RATE} t/s (default, calibrates after Stage 1)\n"
        f"  {sinfo}"
    )

    if resume_from and start_stage > 1:
        results = resume_from.copy()
        results["resumed_from_stage"] = start_stage
    else:
        results = {"task_id": task["id"], "task_name": task["name"],
                   "file": task["file"], "started": datetime.now(timezone.utc).isoformat(),
                   "code_tokens_est": code_tokens, "task_tokens_est": task_tokens}

    analysis_only = task.get("expected_category") == "analysis_only"
    stage_names = STAGE_NAMES_ANALYSIS if analysis_only else STAGE_NAMES
    if analysis_only:
        results["analysis_only"] = True

    run_id = f"task{task['id']}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"

    estimator = RateEstimator()
    if "rate_initial" not in results:
        results["rate_initial"] = {"prompt_eval": estimator.prompt_eval_rate,
                                   "gen": estimator.gen_rate}

    def _update_estimator(status: int, body: dict, elapsed: float) -> None:
        if status == 200:
            usage = body.get("usage", {})
            estimator.update(usage.get("prompt_tokens", 0),
                             usage.get("completion_tokens", 0), elapsed)

    def _record_rate(stage_key: str) -> None:
        results[stage_key]["rate_after"] = {
            "prompt_eval": round(estimator.prompt_eval_rate, 2),
            "gen": round(estimator.gen_rate, 2)}

    # Read previous feedback from DB for closed-loop iteration
    feedback = ""
    if start_stage == 1 and not resume_from:
        feedback = _read_previous_feedback(task["id"])
        if feedback:
            results["feedback_found"] = True
            results["feedback_chars"] = len(feedback)
            print(f"  Feedback from prior work found for Task {task['id']} ({len(feedback)} chars)", flush=True)

    # Pre-compute sliced_code from resume data (fresh run: stage1 not done yet, so sliced_code = code)
    s1_body = results.get("stage1", {}).get("body", {})
    sliced_code = _slice_code(code, s1_body.get("affected_sections", [])) if s1_body else code
    if sliced_code != code:
        results["sliced_code_tokens_est"] = int(len(sliced_code) / 3.5)

    for stage_num in range(1, 5):
        stage_key = f"stage{stage_num}"
        name = stage_names[stage_num]

        if start_stage > stage_num:
            print(f"  Stage {stage_num}/4 {name} — skipped (resuming from stage {start_stage})", flush=True)
            continue

        # Body references from previous stages
        s1 = results.get("stage1", {}).get("body", {})
        s2 = results.get("stage2", {}).get("body", {})
        s3 = results.get("stage3", {}).get("body", {})

        prompt_tokens = _estimate_stage_tokens(stage_num, code, task_desc, s1, s2, s3, sliced_code)
        timeout = calc_timeout_v2(estimator, prompt_tokens, STAGE_MAX_TOKENS[stage_num])
        results[f"{stage_key}_timeout_calc"] = timeout
        print(_stage_label(stage_num, prompt_tokens, timeout,
                          int(len(sliced_code) / 3.5), int(len(code) / 3.5)), flush=True)

        messages = [
            {"role": "system", "content": SYSTEM_32B},
            {"role": "user", "content": _build_stage_user_msg(stage_num, code, task_desc, s1, s2, s3, sliced_code, analysis_only, feedback)},
        ]
        stage_model = "qwen2.5-coder-32b"
        if with_api and stage_num == 2:
            if not DEEPSEEK_KEY:
                print("  [WARN] --with-api set but DEEPSEEK_API_KEY empty — falling back to local 32B",
                      flush=True)
            else:
                status, body_dict, elapsed, was_retry = _call_stage_with_retry(
                    "https://api.deepseek.com/v1/chat/completions", messages,
                    "deepseek-chat", timeout, STAGE_MAX_TOKENS[stage_num],
                    api_key=DEEPSEEK_KEY)
                stage_model = "deepseek-chat"
        if stage_model == "qwen2.5-coder-32b":
            status, body_dict, elapsed, was_retry = _call_stage_with_retry(
                LLAMA_ENDPOINT, messages, "qwen2.5-coder-32b", timeout, STAGE_MAX_TOKENS[stage_num])

        results[stage_key] = extract_json_from_llm_response((status, body_dict))
        results[stage_key]["elapsed_s"] = round(elapsed, 1)
        results[stage_key]["model"] = stage_model
        if not was_retry and stage_model == "qwen2.5-coder-32b":
            _update_estimator(status, body_dict, elapsed)
        elif stage_model != "qwen2.5-coder-32b":
            results[stage_key]["rate_skip"] = "api_call"
        elif was_retry:
            results[stage_key]["rate_skip"] = "retry_cache_hit"
        _record_rate(stage_key)

        if status == 200:
            _insert_activity_stage(task["id"], stage_num, status,
                                   results[stage_key].get("body", {}),
                                   elapsed,
                                   results[stage_key].get("tokens", {}).get("prompt", 0),
                                   results[stage_key].get("tokens", {}).get("completion", 0),
                                   run_id, model_name=stage_model)

        print(f"  Stage {stage_num} done: {elapsed:.0f}s, status={status}", flush=True)
        sinfo = _system_info()
        rate_info = ""
        if estimator.prompt_samples:
            rate_info = f"rate: prompt={estimator.prompt_eval_rate:.1f} gen={estimator.gen_rate:.1f} t/s"
        else:
            rate_info = f"rate: calib... (1st call)"
        sliced_info = ""
        if stage_num >= 2 and sliced_code != code:
            reduction = (1 - len(sliced_code) / max(len(code), 1)) * 100
            sliced_info = f" | sliced: {len(sliced_code)//3} tok ({reduction:.0f}% reduction)"
        timeout_info = f" | timeout: {timeout}s"

        if status == 200:
            st = results[stage_key].get("tokens", {})
            p_tok = st.get("prompt", "?")
            c_tok = st.get("completion", "?")
            mins = elapsed / 60
            extra = ""
            if stage_num == 4:
                conf = results[stage_key].get("body", {}).get("confidence", "?")
                extra = f" | confidence: {conf}"
            retry_note = " [CACHE HIT retry]" if was_retry else ""
            _notify_slack(
                f"T{task['id']} [{task['name']}] Stage {stage_num}/4 {name.strip()} 완료{retry_note}\n"
                f"  elapsed: {elapsed:.0f}s ({mins:.1f}분) | tokens: {p_tok}/{c_tok}{extra}\n"
                f"  {rate_info}{sliced_info}{timeout_info}\n"
                f"  {sinfo}"
            )
        else:
            err = results[stage_key].get("body", {}).get("error", "unknown")[:200]
            _notify_slack(
                f"T{task['id']} [{task['name']}] Stage {stage_num}/4 {name.strip()} 실패\n"
                f"  elapsed: {elapsed:.0f}s | status={status} | error: {err}\n"
                f"  {sinfo}"
            )

        if status != 200:
            results["finished"] = datetime.now(timezone.utc).isoformat()
            results["error"] = f"Stage {stage_num} failed (status={status})"
            return results

        # After stage 1: recompute sliced_code from fresh analysis
        if stage_num == 1:
            s1_body = results["stage1"].get("body", {})
            sliced_code = _slice_code(code, s1_body.get("affected_sections", [])) if s1_body else code
            results["sliced_code_tokens_est"] = int(len(sliced_code) / 3.5)

    results["rate_final"] = {"prompt_eval": round(estimator.prompt_eval_rate, 2),
                             "gen": round(estimator.gen_rate, 2)}
    results["rate_samples_n"] = len(estimator.prompt_samples)
    results["finished"] = datetime.now(timezone.utc).isoformat()
    # Final notification
    sinfo = _system_info()
    total_elapsed = sum(
        results.get(k, {}).get("elapsed_s", 0)
        for k in ["stage1", "stage2", "stage3", "stage4"])
    rate_init = results.get("rate_initial", {})
    rate_end = results.get("rate_final", {})
    rate_delta = ""
    if rate_init and rate_end:
        rate_delta = (f" | rate evolution: prompt {rate_init.get('prompt_eval','?')}→{rate_end.get('prompt_eval','?')}"
                      f" gen {rate_init.get('gen','?')}→{rate_end.get('gen','?')} t/s")
    if results.get("error"):
        _notify_slack(
            f"T{task['id']} [{task['name']}] 파이프라인 중단\n"
            f"  {results['error']} | elapsed: {total_elapsed:.0f}s\n"
            f"  {sinfo}"
        )
    else:
        pkg = results.get("stage4", {}).get("body", {})
        conf = pkg.get("confidence", "?")
        mins = total_elapsed / 60
        sliced_saved = results.get("sliced_code_tokens_est", 0)
        orig_tokens = results.get("code_tokens_est", 1)
        sliced_pct = (1 - sliced_saved / max(orig_tokens, 1)) * 100 if sliced_saved > 0 else 0
        sliced_line = ""
        if sliced_pct > 0:
            sliced_line = f" | code slicing: ~{sliced_pct:.0f}% token reduction"
        _notify_slack(
            f"T{task['id']} [{task['name']}] 파이프라인 완료\n"
            f"  total: {total_elapsed:.0f}s ({mins:.1f}분) | confidence: {conf} | stages: {len(estimator.prompt_samples)} calls{sliced_line}\n"
            f"  {rate_delta}\n"
            f"  {sinfo}"
        )
    return results


def run_api(task: dict, api_cfg: dict) -> dict:
    """Run a single task through one API target."""
    code = read_file(task["file"])
    task_desc = task["description"]
    started = time.time()

    status, body = call_llm(
        api_cfg["endpoint"],
        [{"role": "user", "content": API_PROMPT.format(code=code, task=task_desc)}],
        api_key=DEEPSEEK_KEY if "deepseek" in api_cfg["name"] else "",
        model=api_cfg.get("model", ""),
    )
    elapsed = time.time() - started

    return {
        "api": api_cfg["name"],
        "task_id": task["id"],
        "task_name": task["name"],
        "status": status,
        "body": extract_json_from_llm_response((status, body))["body"] if status == 200 else body,
        "elapsed_s": round(elapsed, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _insert_activity_stage(task_id: int, stage: int, status: int,
                           body: dict, elapsed_s: float,
                           prompt_tokens: int, completion_tokens: int,
                           run_id: str, model_name: str = "qwen2.5-coder-32b") -> bool:
    """Insert a pipeline stage result into activity_log for traceability."""
    try:
        from lib.db import psql_ok, esc_sql
    except ImportError:
        return False

    model_esc = esc_sql(model_name)
    title = esc_sql(f"Task {task_id} Stage {stage} {'OK' if status == 200 else 'FAIL'}")
    summary = esc_sql(
        f"Stage {stage}/4 status={status} elapsed={elapsed_s:.0f}s "
        f"tokens={prompt_tokens}/{completion_tokens}")
    body_json = json.dumps(body, ensure_ascii=False)
    body_esc = body_json.replace("'", "''")
    run_id_esc = esc_sql(run_id)

    ok = psql_ok(
        f"INSERT INTO activity_log (type, source, title, summary, body, "
        f"agent, model, run_id, summary_status) "
        f"VALUES ('stage', 'pipeline', '{title}', '{summary}', "
        f"'{body_esc}', '{model_esc}', '{model_esc}', "
        f"'{run_id_esc}', 'raw')")
    if not ok:
        print(f"  [WARN] activity_log insert failed for Task {task_id} Stage {stage}", flush=True)
    return ok


def compare_results(task_id: int) -> dict:
    """Load all results for a task and generate comparison."""
    files = sorted(OUTPUT_DIR.glob(f"*_task{task_id:02d}_*.json"))
    models = {}
    for fp in files:
        try:
            data = json.loads(fp.read_text())
            if "stage4" in data:
                models["32B-4stage"] = data
            elif "api" in data:
                models[data["api"]] = data
        except Exception:
            continue

    comparison = {"task_id": task_id, "models": {}}
    for name, data in models.items():
        entry = {"name": name}
        if "stage4" in data:
            pkg = data["stage4"].get("body", {})
            entry["confidence"] = pkg.get("confidence")
            entry["review_points"] = len(pkg.get("review_points", []))
            entry["rationale_count"] = len(pkg.get("rationale", []))
            entry["has_diff"] = bool(pkg.get("diff"))
            entry["elapsed"] = _parse_elapsed(data.get("started"), data.get("finished"))
        else:
            body = data.get("body", {})
            entry["confidence"] = body.get("confidence")
            entry["review_points"] = len(body.get("review_points", []))
            entry["rationale_count"] = len(body.get("rationale", []))
            entry["has_diff"] = bool(body.get("diff"))
            entry["elapsed"] = data.get("elapsed_s")
            entry["tokens"] = body.get("tokens_used")
        comparison["models"][name] = entry

    return comparison


def _upload_pipeline_result(result: dict, task: dict) -> Optional[str]:
    """Upload pipeline result as review-bundle to Azure Blob.

    Returns SAS URL on success, None on failure.
    """
    try:
        from lib.blob_uploader import upload_review_bundle
    except ImportError:
        return None

    pkg = result.get("stage4", {}).get("body", {})
    diff = pkg.get("diff", "")
    if isinstance(diff, str) and len(diff) > 8000:
        diff = diff[:8000] + "\n... (truncated)"

    rationale_lines = "\n".join(f"- {r}" for r in pkg.get("rationale", [])[:10])
    stages_elapsed = ", ".join(
        f"Stage {i}: {result.get(f'stage{i}', {}).get('elapsed_s', '?')}s"
        for i in range(1, 5)
    )

    bundle = f"""## Task {task['id']}: {task['name']}

**File:** `{task['file']}`
**Stage:** {task.get('stage', 'N/A')}
**Confidence:** {pkg.get('confidence', 'N/A')}

### Timings
{stages_elapsed}

### Diff
```diff
{diff if diff else 'N/A'}
```

### Rationale
{rationale_lines if rationale_lines else 'N/A'}

### Review Points
{chr(10).join(f'- {r}' for r in pkg.get('review_points', [])[:10]) if pkg.get('review_points') else 'N/A'}
"""
    session_id = f"task{task['id']}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    return upload_review_bundle(
        content=bundle,
        pipeline="code_mod",
        session_id=session_id,
        metadata={
            "task": task["name"],
            "file": task["file"],
            "confidence": str(pkg.get("confidence", "")),
            "elapsed_total_s": str(sum(
                result.get(f"stage{i}", {}).get("elapsed_s", 0) for i in range(1, 5)
            )),
        },
    )


def _parse_elapsed(started: Optional[str], finished: Optional[str]) -> Optional[float]:
    if not started or not finished:
        return None
    try:
        s = datetime.fromisoformat(started)
        f = datetime.fromisoformat(finished)
        return round((f - s).total_seconds(), 1)
    except Exception:
        return None


def print_comparison(comparison: dict):
    """Print side-by-side comparison table."""
    models = comparison["models"]
    if not models:
        print("No results to compare.")
        return

    header = f"{'Model':<25} {'Conf':>6} {'Diff':>6} {'RevPts':>7} {'Time':>8} {'Tokens':>8}"
    print(f"\n=== Task {comparison['task_id']} Comparison ===")
    print(header)
    print("-" * len(header))

    for name, m in models.items():
        conf = f"{m['confidence']:.2f}" if m.get('confidence') else "N/A"
        diff = "Yes" if m.get('has_diff') else "No"
        rp = str(m.get('review_points', 'N/A'))
        elapsed = f"{m['elapsed']}s" if m.get('elapsed') else "N/A"
        toks = ""
        if m.get('tokens'):
            t = m['tokens']
            if isinstance(t, dict):
                toks = f"p{t.get('prompt',0)}/c{t.get('completion',0)}"
        print(f"{name:<25} {conf:>6} {diff:>6} {rp:>7} {elapsed:>8} {toks:>8}")
    print()


def warmup_32b() -> bool:
    """Send a tiny request to load model weights and verify connectivity.
    Retries once after a 30s wait if the first attempt fails (mlock can delay loading).
    Returns True if server responded successfully."""
    for attempt in (1, 2):
        print(f"[warmup] Sending small request to load 32B model weights... (attempt {attempt}/2)", flush=True)
        t0 = time.monotonic()
        status, body = call_llm(LLAMA_ENDPOINT, [
            {"role": "user", "content": "Return the word 'ready'."},
        ], model="qwen2.5-coder-32b", timeout=600, max_tokens=16)
        elapsed = time.monotonic() - t0
        if status == 200:
            print(f"[warmup] OK in {elapsed:.1f}s — model loaded", flush=True)
            return True
        print(f"[warmup] FAILED (status={status}): {body.get('error', '')[:100]}", flush=True)
        if attempt == 1:
            print("[warmup] Waiting 30s then retrying...", flush=True)
            time.sleep(30)
    return False


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Code modification test harness")
    ap.add_argument("--task", type=int, help="Run single task by ID")
    ap.add_argument("--local-only", action="store_true", help="32B pipeline only")
    ap.add_argument("--api-only", action="store_true", help="API targets only")
    ap.add_argument("--report", action="store_true", help="Print comparison report")
    ap.add_argument("--list", action="store_true", help="List available tasks")
    ap.add_argument("--start-stage", type=int, choices=[1,2,3,4], default=1,
                    help="Skip to stage (requires previous results)")
    ap.add_argument("--resume-from", type=str,
                    help="Path to previous result JSON for stage skip")
    ap.add_argument("--with-api", action="store_true",
                    help="Use DeepSeek API for Stage 2 PLAN (internet knowledge)")
    args = ap.parse_args()

    config = load_tasks()
    tasks = config["tasks"]
    api_targets = config["api_targets"]

    if args.list:
        for t in tasks:
            print(f"  {t['id']}: [{t['stage']}] {t['name']}")
        return

    if args.report:
        if args.task:
            comp = compare_results(args.task)
            print_comparison(comp)
        else:
            for t in tasks:
                comp = compare_results(t["id"])
                if comp["models"]:
                    print_comparison(comp)
        return

    selected = tasks
    if args.task:
        selected = [t for t in tasks if t["id"] == args.task]
        if not selected:
            print(f"Task {args.task} not found.")
            sys.exit(1)

    if not args.api_only:
        if not warmup_32b():
            print("WARNING: 32B warmup failed — server may not be ready", flush=True)

    for task in selected:
        print(f"\n{'='*60}")
        print(f"Task {task['id']}: [{task['stage']}] {task['name']}")
        print(f"File: {task['file']}")
        print(f"{'='*60}")

        if not args.api_only:
            # Verify model is still loaded + memory is sufficient
            if task["id"] > 1:
                print(f"\n[Pre-check] Verifying 32B model still loaded...", flush=True)
                status, body = call_llm(LLAMA_ENDPOINT, [
                    {"role": "user", "content": "Return 'ok'."},
                ], model="qwen2.5-coder-32b", timeout=60, max_tokens=8)
                if status != 200:
                    err = body.get("error", "unknown")[:100]
                    print(f"  FATAL: Model unloaded before Task {task['id']}: {err}", flush=True)
                    print(f"  Skipping Task {task['id']} — container restart required.", flush=True)
                    continue
                print(f"  Model OK", flush=True)

            if not _pre_task_memory_check(task["id"]):
                continue

            # Load resume data if start_stage > 1
            resume_data = None
            if args.start_stage > 1:
                if args.resume_from:
                    resume_data = json.loads(Path(args.resume_from).read_text())
                else:
                    # Auto-find latest result for this task
                    pattern = f"/var/tmp/code_mod_tests/local32b_task{task['id']:02d}_*.json"
                    files = sorted(glob.glob(pattern))
                    if files:
                        resume_data = json.loads(Path(files[-1]).read_text())
                        print(f"  Resuming from {files[-1]}", flush=True)
                    else:
                        print(f"  No previous result found for task {task['id']}, starting fresh", flush=True)
                        args.start_stage = 1

                # Validate resume data has required previous stage
                if resume_data and args.start_stage > 1:
                    required = f"stage{args.start_stage - 1}"
                    if required not in resume_data:
                        print(f"  [WARN] Resume data missing '{required}' key — starting fresh",
                              flush=True)
                        resume_data = None
                        args.start_stage = 1

            print(f"\n[32B 4-Stage Pipeline] Starting from stage {args.start_stage}...")
            result = None
            try:
                result = run_32b_4stage(task, start_stage=args.start_stage,
                                        resume_from=resume_data,
                                        with_api=args.with_api)
                name = save_result(result, "local32b", task["id"])
                print(f"  Saved: {name}")
                pkg = result.get("stage4", {}).get("body", {})
                conf = pkg.get("confidence", "N/A")
                print(f"  Confidence: {conf}")
                if not result.get("error"):
                    url = _upload_pipeline_result(result, task)
                    if url:
                        print(f"  Review: {url}")
            except Exception as e:
                print(f"  32B pipeline failed: {e}")
                import traceback
                traceback.print_exc()
                if result is not None:
                    name = save_result(result, "local32b", task["id"])
                    print(f"  Partial results saved: {name}")

        if not args.local_only:
            for api in api_targets:
                print(f"\n[{api['name']}] Starting...")
                try:
                    result = run_api(task, api)
                    name = save_result(result, "api", task["id"], f"_{api['name']}")
                    print(f"  Saved: {name}")
                    print(f"  Status: {result['status']}, Time: {result['elapsed_s']}s")
                except Exception as e:
                    print(f"  {api['name']} failed: {e}")

        # Print immediate comparison if both ran
        if not args.local_only and not args.api_only:
            comp = compare_results(task["id"])
            print_comparison(comp)


if __name__ == "__main__":
    main()
