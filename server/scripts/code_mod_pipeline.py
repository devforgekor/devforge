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
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from lib.llm.client import call_llm
from lib.llm.rate_estimator import PromptCompletionRateEstimator as RateEstimator
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
    Uses char/3 heuristic (English code ≈ 3 chars/token).
    Stages 2-3 use sliced_code (affected sections only) ≈ 55% reduction."""
    code_tokens = len(code) // 3
    sliced_tokens = len(sliced_code) // 3 if sliced_code else code_tokens
    task_tokens = len(task_desc) // 3

    if stage == 1:
        return code_tokens + task_tokens + 300
    elif stage == 2:
        analysis_tokens = len(json.dumps(stage1_body or {})) // 3
        return sliced_tokens + task_tokens + analysis_tokens + 280
    elif stage == 3:
        plan_tokens = len(json.dumps(stage2_body or {})) // 3
        return sliced_tokens + plan_tokens + 230
    elif stage == 4:
        # REQUEST removed from Stage 4 — diff + template + SYSTEM_32B
        diff_text = ""
        if isinstance(stage3_body, dict):
            diff_text = stage3_body.get("text", str(stage3_body))
        elif isinstance(stage3_body, str):
            diff_text = stage3_body
        diff_tokens = len(diff_text) // 3
        return diff_tokens + 230
    return 1000

SYSTEM_32B = (
    "You are a CODE PREPROCESSOR. Your output is NOT final — it will be reviewed "
    "by a senior engineer via API. Your job: produce minimal, surgical changes. "
    "When unsure, annotate with [REVIEW] instead of guessing."
)

STAGE1_ANALYZE = """You are in STAGE 1: ANALYZE.
Read the following code and the modification request.
Identify: (a) which lines/functions are affected, (b) what type of change is needed,
(c) any cross-dependencies or side effects.

CODE:
{code}

REQUEST:
{task}

Output format (JSON):
{{"affected_sections": ["func_name:line_range", ...],
  "change_type": "helper_extraction|bug_fix|validation|cross_function|dedup|interface|structural|analysis",
  "dependencies": ["func_name", ...],
  "risk_assessment": "low|medium|high",
  "notes": "..."}}"""

STAGE2_PLAN = """Stage 2: PLAN. Based on the analysis, design the minimal change.

ANALYSIS:
{analysis}

AFFECTED CODE (only the sections that need changes):
{code}

REQUEST:
{task}

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
                           max_retries: int = 2) -> Tuple[int, dict, float, bool]:
    """Stage 호출 + connection drop 시 재시도 (prompt cache 활용).

    32B ARM CPU에서 prompt eval이 38분+ 걸릴 수 있음.
    TCP keepalive로 대부분 해결되지만, 만약 connection이 끊기면:
    - 서버는 계속 처리 중 (prompt cache에 저장됨)
    - 대기 후 재시도하면 cache hit으로 빠르게 완료
    """
    total_elapsed = 0.0
    was_retry = False
    for attempt in range(max_retries + 1):
        t0 = time.monotonic()
        status, body = call_llm(endpoint, messages, model=model,
                                timeout=timeout, max_tokens=max_tokens)
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
                          sliced_code: str, analysis_only: bool = False) -> str:
    """Build the user message for a pipeline stage."""
    if stage_num == 1:
        return STAGE1_ANALYZE.format(code=code, task=task_desc)
    elif stage_num == 2:
        return STAGE2_PLAN.format(
            analysis=json.dumps(s1_body, indent=2), code=sliced_code, task=task_desc)
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
                   resume_from: dict = None) -> dict:
    """Run a single task through Qwen32B 4-stage pipeline with dynamic timeouts.
    start_stage: 1-4, skip earlier stages if resume_from provided."""
    code = read_file(task["file"])
    task_desc = task["description"]
    code_tokens = len(code) // 3
    task_tokens = len(task_desc) // 3

    _notify_slack(f"T{task['id']} [{task['name']}] 파이프라인 시작 — Stage {start_stage}/4, 예상 코드토큰 ~{code_tokens}")

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

    # Pre-compute sliced_code from resume data (fresh run: stage1 not done yet, so sliced_code = code)
    s1_body = results.get("stage1", {}).get("body", {})
    sliced_code = _slice_code(code, s1_body.get("affected_sections", [])) if s1_body else code
    if sliced_code != code:
        results["sliced_code_tokens_est"] = len(sliced_code) // 3

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
                          len(sliced_code) // 3, len(code) // 3), flush=True)

        messages = [
            {"role": "system", "content": SYSTEM_32B},
            {"role": "user", "content": _build_stage_user_msg(stage_num, code, task_desc, s1, s2, s3, sliced_code, analysis_only)},
        ]
        status, body_dict, elapsed, was_retry = _call_stage_with_retry(
            LLAMA_ENDPOINT, messages, "qwen2.5-coder-32b", timeout, STAGE_MAX_TOKENS[stage_num])

        results[stage_key] = extract_json_from_llm_response((status, body_dict))
        results[stage_key]["elapsed_s"] = round(elapsed, 1)
        if not was_retry:
            _update_estimator(status, body_dict, elapsed)
        else:
            results[stage_key]["rate_skip"] = "retry_cache_hit"
        _record_rate(stage_key)

        if status == 200:
            _insert_activity_stage(task["id"], stage_num, status,
                                   results[stage_key].get("body", {}),
                                   elapsed,
                                   results[stage_key].get("tokens", {}).get("prompt", 0),
                                   results[stage_key].get("tokens", {}).get("completion", 0),
                                   run_id)

        print(f"  Stage {stage_num} done: {elapsed:.0f}s, status={status}", flush=True)
        if status == 200:
            st = results[stage_key].get("tokens", {})
            extra = ""
            if stage_num == 4:
                conf = results[stage_key].get("body", {}).get("confidence", "?")
                extra = f", confidence={conf}"
            _notify_slack(f"T{task['id']} Stage {stage_num}/4 {name.strip()} 완료 — {elapsed:.0f}s, status=200, tokens={st.get('prompt','?')}/{st.get('completion','?')}{extra}")
        else:
            _notify_slack(f"T{task['id']} Stage {stage_num}/4 {name.strip()} 실패 — status={status}")

        if status != 200:
            results["finished"] = datetime.now(timezone.utc).isoformat()
            results["error"] = f"Stage {stage_num} failed (status={status})"
            return results

        # After stage 1: recompute sliced_code from fresh analysis
        if stage_num == 1:
            s1_body = results["stage1"].get("body", {})
            sliced_code = _slice_code(code, s1_body.get("affected_sections", [])) if s1_body else code
            results["sliced_code_tokens_est"] = len(sliced_code) // 3

    results["rate_final"] = {"prompt_eval": round(estimator.prompt_eval_rate, 2),
                             "gen": round(estimator.gen_rate, 2)}
    results["rate_samples_n"] = len(estimator.prompt_samples)
    results["finished"] = datetime.now(timezone.utc).isoformat()
    # Final notification
    if results.get("error"):
        _notify_slack(f"T{task['id']} 파이프라인 중단 — {results['error']}")
    else:
        pkg = results.get("stage4", {}).get("body", {})
        conf = pkg.get("confidence", "?")
        total_elapsed = sum(
            results.get(k, {}).get("elapsed_s", 0)
            for k in ["stage1", "stage2", "stage3", "stage4"])
        _notify_slack(f"T{task['id']} 파이프라인 완료 — 총 {total_elapsed:.0f}s, confidence={conf}")
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
                           run_id: str) -> bool:
    """Insert a pipeline stage result into activity_log for traceability."""
    try:
        from lib.db import psql_ok, esc_sql
    except ImportError:
        return False

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
        f"'{body_esc}', 'qwen2.5-coder-32b', 'qwen2.5-coder-32b', "
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
    Returns True if server responded successfully."""
    print("[warmup] Sending small request to load 32B model weights...", flush=True)
    t0 = time.monotonic()
    status, body = call_llm(LLAMA_ENDPOINT, [
        {"role": "user", "content": "Return the word 'ready'."},
    ], model="qwen2.5-coder-32b", timeout=300, max_tokens=16)
    elapsed = time.monotonic() - t0
    if status == 200:
        print(f"[warmup] OK in {elapsed:.1f}s — model loaded", flush=True)
        return True
    print(f"[warmup] FAILED (status={status}): {body.get('error', '')[:100]}", flush=True)
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
            # Verify model is still loaded before each task
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
                                        resume_from=resume_data)
                name = save_result(result, "local32b", task["id"])
                print(f"  Saved: {name}")
                pkg = result.get("stage4", {}).get("body", {})
                conf = pkg.get("confidence", "N/A")
                print(f"  Confidence: {conf}")
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
