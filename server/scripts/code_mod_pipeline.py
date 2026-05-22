#!/usr/bin/env python3
"""code_mod_pipeline.py — Qwen32B 4-stage code modification pipeline.

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
import http.client
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

TASKS_FILE = Path(__file__).parent.parent / "code_mod_test_tasks.yaml"
OUTPUT_DIR = Path("/var/tmp/code_mod_tests")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")
LLAMA_ENDPOINT = "http://127.0.0.1:8081"
# token-based dynamic timeout parameters
# Formula: timeout = (prompt_tokens / PROMPT_EVAL_RATE) + (max_tokens / GEN_RATE) + BUFFER
# 32B IQ4_XS on ARM CPU benchmarked at:
#   - prompt eval:  ~3-5 tok/s (CPU prefill, degrades with longer ctx)
#   - generation:   ~2.15 tok/s (measured from prior runs)
# Conservative prompt eval rate (3 tok/s) accounts for ctx degradation
PROMPT_EVAL_RATE = 2.0   # tokens/sec — T01/T02 실측 기반 보정 (was 3.0)
GEN_RATE = 2.15          # tokens/sec for generation (stable)
TIMEOUT_BUFFER = 120     # extra seconds for network/overhead

STAGE_MAX_TOKENS = {1: 2048, 2: 3072, 3: 3072, 4: 2048}

class RateEstimator:
    """EMA + median 기반 rate tracker. 실측값으로 self-calibrating."""
    def __init__(self, initial_prompt_eval: float = 2.0, initial_gen: float = 2.15,
                 alpha: float = 0.3):
        self.prompt_eval_rate = initial_prompt_eval
        self.gen_rate = initial_gen
        self.alpha = alpha
        self.prompt_samples: list = []
        self.gen_samples: list = []

    def update(self, prompt_tokens: int, completion_tokens: int, elapsed_s: float):
        """실측 elapsed + usage token으로 rate 갱신.
        /v1/chat/completions는 timings 필드가 없으므로 근사 방식 사용:
        eval_time = elapsed * 0.7, gen_time = elapsed * 0.3"""
        if elapsed_s <= 0 or (prompt_tokens <= 0 and completion_tokens <= 0):
            return
        eval_time = elapsed_s * 0.7
        gen_time = elapsed_s * 0.3
        if eval_time > 0 and prompt_tokens > 0:
            rate = prompt_tokens / eval_time
            self.prompt_samples.append(rate)
            if len(self.prompt_samples) > 20:
                self.prompt_samples.pop(0)
            self.prompt_eval_rate = self._ema(self.prompt_eval_rate, rate)
        if gen_time > 0 and completion_tokens > 0:
            rate = completion_tokens / gen_time
            self.gen_samples.append(rate)
            if len(self.gen_samples) > 20:
                self.gen_samples.pop(0)
            self.gen_rate = self._ema(self.gen_rate, rate)

    def _ema(self, old: float, new: float) -> float:
        return self.alpha * new + (1 - self.alpha) * old

    def median_prompt_rate(self) -> float:
        if len(self.prompt_samples) >= 3:
            return sorted(self.prompt_samples)[len(self.prompt_samples) // 2]
        return self.prompt_eval_rate

    def median_gen_rate(self) -> float:
        if len(self.gen_samples) >= 3:
            return sorted(self.gen_samples)[len(self.gen_samples) // 2]
        return self.gen_rate


def calc_timeout_v2(estimator: RateEstimator, prompt_tokens: int,
                     max_tokens: int) -> int:
    """RateEstimator의 실측 rate로 timeout 계산. self-calibrating."""
    rate = estimator.median_prompt_rate()
    prompt_time = prompt_tokens / rate if rate > 0 else prompt_tokens / 2.0
    gen_time = max_tokens / estimator.gen_rate if estimator.gen_rate > 0 else max_tokens / 2.15
    return int(prompt_time + gen_time + TIMEOUT_BUFFER)


def estimate_prompt_tokens(code: str, task_desc: str, stage: int,
                           stage1_body: dict = None,
                           stage2_body: dict = None,
                           stage3_body: dict = None) -> int:
    """Estimate prompt tokens for each stage before execution.
    Uses char/3 heuristic (English code ≈ 3 chars/token)."""
    code_tokens = len(code) // 3
    task_tokens = len(task_desc) // 3

    if stage == 1:
        # SYSTEM_32B + STAGE1_ANALYZE template + code + task
        return code_tokens + task_tokens + 300
    elif stage == 2:
        # SYSTEM_32B + STAGE2_PLAN + analysis JSON + code + task
        analysis_tokens = len(json.dumps(stage1_body or {})) // 3
        return code_tokens + task_tokens + analysis_tokens + 400
    elif stage == 3:
        # SYSTEM_32B + STAGE3_IMPLEMENT + plan JSON + code
        plan_tokens = len(json.dumps(stage2_body or {})) // 3
        return code_tokens + plan_tokens + 400
    elif stage == 4:
        # SYSTEM_32B + STAGE4_PACKAGE + diff text + task
        diff_text = ""
        if isinstance(stage3_body, dict):
            diff_text = stage3_body.get("text", str(stage3_body))
        elif isinstance(stage3_body, str):
            diff_text = stage3_body
        diff_tokens = len(diff_text) // 3
        return diff_tokens + task_tokens + 300
    return 1000  # fallback

def calc_timeout(prompt_tokens: int, max_tokens: int) -> int:
    """Dynamic timeout based on actual token counts + known inference rates."""
    prompt_time = prompt_tokens / PROMPT_EVAL_RATE
    gen_time = max_tokens / GEN_RATE
    return int(prompt_time + gen_time + TIMEOUT_BUFFER)

def estimate_and_timeout(stage: int, code: str, task_desc: str,
                         stage1_body: dict = None,
                         stage2_body: dict = None,
                         stage3_body: dict = None) -> int:
    """One-shot: estimate prompt tokens and return dynamic timeout for a stage."""
    prompt_tokens = estimate_prompt_tokens(code, task_desc, stage,
                                           stage1_body, stage2_body, stage3_body)
    timeout = calc_timeout(prompt_tokens, STAGE_MAX_TOKENS[stage])
    return timeout

# ── prompt templates ────────────────────────────────────────────────────
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

STAGE2_PLAN = """You are in STAGE 2: PLAN.
Based on the Stage 1 analysis, design the minimal change approach.

ANALYSIS:
{analysis}

CODE:
{code}

REQUEST:
{task}

Output format (JSON):
{{"approach": "...",
  "steps": ["step1", "step2", ...],
  "files_to_modify": ["file_path"],
  "estimated_lines_changed": {{"added": N, "removed": M}},
  "backward_compatible": true|false,
  "edge_cases": ["case1", ...]}}"""

STAGE3_IMPLEMENT = """You are in STAGE 3: IMPLEMENT.
Produce a unified diff that implements the change. Be minimal and surgical.

PLAN:
{plan}

ORIGINAL CODE:
{code}

Output: unified diff only. Start with --- / +++ headers. Include context lines."""

STAGE4_PACKAGE = """You are in STAGE 4: PACKAGE.
Format the implementation for API review. Produce JSON.

DIFF:
{diff}

REQUEST:
{task}

Output format (JSON):
{{"task": "<one-line summary>",
  "analysis": "<brief analysis of what was changed>",
  "diff": "<the complete unified diff, escaped for JSON>",
  "rationale": ["per-hunk one-line reason", ...],
  "confidence": 0.0-1.0,
  "review_points": ["specific things API should verify", ...]}}"""

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


def read_file(path: str) -> str:
    with open(path) as f:
        return f.read()


def _enable_keepalive(sock: socket.socket) -> None:
    """Enable aggressive TCP keepalive to prevent idle connection drops by pasta/podman.
    Probes start at 10s idle, every 10s — keeps connection alive during long prompt eval."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 127)
    except (OSError, AttributeError):
        pass


def call_llm(endpoint: str, messages: list, api_key: str = "",
             model: str = "", timeout: int = 1200, max_tokens: int = 2048) -> Tuple[int, dict]:
    """Call any OpenAI-compatible chat completions endpoint. Returns (status, body).

    Uses TCP keepalive to prevent pasta/podman from dropping idle connections
    during long prompt evaluation (32B on ARM CPU: ~1.87 tok/s).
    """
    body = {"messages": messages, "temperature": 0.1, "max_tokens": max_tokens}
    if model:
        body["model"] = model

    data = json.dumps(body).encode()
    u = endpoint
    if u.endswith("/"):
        u = u[:-1]
    path = "/v1/chat/completions"
    base = u
    if u.endswith("/v1/chat/completions"):
        base = u[: -len(path)]
        path = "/v1/chat/completions"

    host = base.replace("https://", "").replace("http://", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        if endpoint.startswith("https://"):
            import ssl
            conn = http.client.HTTPSConnection(
                host, timeout=timeout,
                context=ssl.create_default_context()
            )
        else:
            conn = http.client.HTTPConnection(host, timeout=timeout)

        conn.connect()
        _enable_keepalive(conn.sock)
        conn.request("POST", path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
        if status == 200:
            return status, json.loads(raw)
        return status, {"error": raw.decode()[:500]}
    except Exception as e:
        return 0, {"error": str(e)}
    finally:
        conn.close()


def _call_stage_with_retry(endpoint: str, messages: list, model: str,
                           timeout: int, max_tokens: int,
                           max_retries: int = 2) -> Tuple[int, dict, float]:
    """Stage 호출 + connection drop 시 재시도 (prompt cache 활용).

    32B ARM CPU에서 prompt eval이 38분+ 걸릴 수 있음.
    TCP keepalive로 대부분 해결되지만, 만약 connection이 끊기면:
    - 서버는 계속 처리 중 (prompt cache에 저장됨)
    - 대기 후 재시도하면 cache hit으로 빠르게 완료
    """
    total_elapsed = 0.0
    for attempt in range(max_retries + 1):
        t0 = time.monotonic()
        status, body = call_llm(endpoint, messages, model=model,
                                timeout=timeout, max_tokens=max_tokens)
        elapsed = time.monotonic() - t0
        total_elapsed += elapsed
        if status == 200:
            return status, body, total_elapsed
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
            continue
        break
    return status, body, total_elapsed


def run_32b_4stage(task: dict, start_stage: int = 1,
                   resume_from: dict = None) -> dict:
    """Run a single task through Qwen32B 4-stage pipeline with dynamic timeouts.
    start_stage: 1-4, skip earlier stages if resume_from provided."""
    code = read_file(task["file"])
    task_desc = task["description"]
    code_tokens = len(code) // 3
    task_tokens = len(task_desc) // 3

    if resume_from and start_stage > 1:
        results = resume_from.copy()
        results["resumed_from_stage"] = start_stage
    else:
        results = {"task_id": task["id"], "task_name": task["name"],
                   "file": task["file"], "started": datetime.now(timezone.utc).isoformat(),
                   "code_tokens_est": code_tokens, "task_tokens_est": task_tokens}

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

    # Stage 1: ANALYZE
    if start_stage <= 1:
        t1 = estimate_and_timeout(1, code, task_desc)
        results["stage1_timeout_calc"] = t1
        print(f"  Stage 1/4 ANALYZE — timeout={t1}s, ~{code_tokens+task_tokens+300} prompt tokens",
              flush=True)
        s1_status, s1_body_dict, elapsed1 = _call_stage_with_retry(
            LLAMA_ENDPOINT, [
                {"role": "system", "content": SYSTEM_32B},
                {"role": "user", "content": STAGE1_ANALYZE.format(code=code, task=task_desc)},
            ], "qwen2.5-coder-32b", t1, STAGE_MAX_TOKENS[1])
        results["stage1"] = _safe_json((s1_status, s1_body_dict))
        results["stage1"]["elapsed_s"] = round(elapsed1, 1)
        _update_estimator(s1_status, s1_body_dict, elapsed1)
        _record_rate("stage1")
        if s1_status == 200:
            _insert_activity_stage(task["id"], 1, s1_status,
                                   results["stage1"].get("body", {}),
                                   elapsed1,
                                   results["stage1"].get("tokens", {}).get("prompt", 0),
                                   results["stage1"].get("tokens", {}).get("completion", 0),
                                   run_id)
        print(f"  Stage 1 done: {elapsed1:.0f}s, status={s1_status}", flush=True)
        if s1_status != 200:
            results["finished"] = datetime.now(timezone.utc).isoformat()
            results["error"] = f"Stage 1 failed (status={s1_status})"
            return results
    else:
        print(f"  Stage 1/4 ANALYZE — skipped (resuming from stage {start_stage})", flush=True)

    # Stage 2: PLAN
    s1_body = results["stage1"].get("body", {})
    if start_stage <= 2:
        prompt2 = estimate_prompt_tokens(code, task_desc, 2, stage1_body=s1_body)
        t2 = calc_timeout_v2(estimator, prompt2, STAGE_MAX_TOKENS[2])
        results["stage2_timeout_calc"] = t2
        print(f"  Stage 2/4 PLAN    — timeout={t2}s, ~{prompt2} prompt tokens", flush=True)
        s2_status, s2_body_dict, elapsed2 = _call_stage_with_retry(
            LLAMA_ENDPOINT, [
                {"role": "system", "content": SYSTEM_32B},
                {"role": "user", "content": STAGE2_PLAN.format(
                    analysis=json.dumps(s1_body, indent=2), code=code, task=task_desc)},
            ], "qwen2.5-coder-32b", t2, STAGE_MAX_TOKENS[2])
        results["stage2"] = _safe_json((s2_status, s2_body_dict))
        results["stage2"]["elapsed_s"] = round(elapsed2, 1)
        _update_estimator(s2_status, s2_body_dict, elapsed2)
        _record_rate("stage2")
        if s2_status == 200:
            _insert_activity_stage(task["id"], 2, s2_status,
                                   results["stage2"].get("body", {}),
                                   elapsed2,
                                   results["stage2"].get("tokens", {}).get("prompt", 0),
                                   results["stage2"].get("tokens", {}).get("completion", 0),
                                   run_id)
        print(f"  Stage 2 done: {elapsed2:.0f}s, status={s2_status}", flush=True)
        if s2_status != 200:
            results["finished"] = datetime.now(timezone.utc).isoformat()
            results["error"] = f"Stage 2 failed (status={s2_status})"
            return results
    else:
        print(f"  Stage 2/4 PLAN    — skipped (resuming from stage {start_stage})", flush=True)

    # Stage 3: IMPLEMENT (estimator rate + retry)
    s2_body = results["stage2"].get("body", {})
    if start_stage <= 3:
        prompt3 = estimate_prompt_tokens(code, task_desc, 3, stage2_body=s2_body)
        t3 = calc_timeout_v2(estimator, prompt3, STAGE_MAX_TOKENS[3])
        results["stage3_timeout_calc"] = t3
        print(f"  Stage 3/4 IMPL    — timeout={t3}s, ~{prompt3} prompt tokens", flush=True)
        s3_status, s3_body_dict, elapsed3 = _call_stage_with_retry(
            LLAMA_ENDPOINT, [
                {"role": "system", "content": SYSTEM_32B},
                {"role": "user", "content": STAGE3_IMPLEMENT.format(
                    plan=json.dumps(s2_body, indent=2) if s2_body else "{}", code=code)},
            ], "qwen2.5-coder-32b", t3, STAGE_MAX_TOKENS[3])
        results["stage3"] = _safe_json((s3_status, s3_body_dict))
        results["stage3"]["elapsed_s"] = round(elapsed3, 1)
        _update_estimator(s3_status, s3_body_dict, elapsed3)
        _record_rate("stage3")
        if s3_status == 200:
            _insert_activity_stage(task["id"], 3, s3_status,
                                   results["stage3"].get("body", {}),
                                   elapsed3,
                                   results["stage3"].get("tokens", {}).get("prompt", 0),
                                   results["stage3"].get("tokens", {}).get("completion", 0),
                                   run_id)
        print(f"  Stage 3 done: {elapsed3:.0f}s, status={s3_status}", flush=True)
        if s3_status != 200:
            results["finished"] = datetime.now(timezone.utc).isoformat()
            results["error"] = f"Stage 3 failed (status={s3_status})"
            return results
    else:
        print(f"  Stage 3/4 IMPL    — skipped (resuming from stage {start_stage})", flush=True)

    # Stage 4: PACKAGE
    s3_body = results["stage3"].get("body", {})
    if start_stage <= 4:
        prompt4 = estimate_prompt_tokens(code, task_desc, 4, stage3_body=s3_body)
        t4 = calc_timeout_v2(estimator, prompt4, STAGE_MAX_TOKENS[4])
        results["stage4_timeout_calc"] = t4
        print(f"  Stage 4/4 PACKAGE — timeout={t4}s, ~{prompt4} prompt tokens", flush=True)
        s4_status, s4_body_dict, elapsed4 = _call_stage_with_retry(
            LLAMA_ENDPOINT, [
                {"role": "system", "content": SYSTEM_32B},
                {"role": "user", "content": STAGE4_PACKAGE.format(
                    diff=json.dumps(s3_body) if isinstance(s3_body, dict) else str(s3_body),
                    task=task_desc)},
            ], "qwen2.5-coder-32b", t4, STAGE_MAX_TOKENS[4])
        results["stage4"] = _safe_json((s4_status, s4_body_dict))
        results["stage4"]["elapsed_s"] = round(elapsed4, 1)
        _update_estimator(s4_status, s4_body_dict, elapsed4)
        _record_rate("stage4")
        if s4_status == 200:
            _insert_activity_stage(task["id"], 4, s4_status,
                                   results["stage4"].get("body", {}),
                                   elapsed4,
                                   results["stage4"].get("tokens", {}).get("prompt", 0),
                                   results["stage4"].get("tokens", {}).get("completion", 0),
                                   run_id)
        print(f"  Stage 4 done: {elapsed4:.0f}s, status={s4_status}", flush=True)
    else:
        print(f"  Stage 4/4 PACKAGE — skipped", flush=True)

    results["rate_final"] = {"prompt_eval": round(estimator.prompt_eval_rate, 2),
                             "gen": round(estimator.gen_rate, 2)}
    results["rate_samples_n"] = len(estimator.prompt_samples)
    results["finished"] = datetime.now(timezone.utc).isoformat()
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
        "body": _safe_json((status, body))["body"] if status == 200 else body,
        "elapsed_s": round(elapsed, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _safe_json(llm_result: tuple) -> dict:
    """Extract JSON from LLM response, with fallback."""
    status, body = llm_result
    if status != 200:
        return {"error": body.get("error", f"HTTP {status}"), "body": body}

    content = ""
    try:
        choices = body.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")
    except Exception:
        pass

    result = {"status": status, "raw_content": content, "body": {}}

    # Try to extract JSON from content
    if content:
        # Try direct JSON parse first
        try:
            result["body"] = json.loads(content)
        except json.JSONDecodeError:
            # Try to find JSON block
            for marker in ("```json", "```"):
                if marker in content:
                    start = content.find(marker) + len(marker)
                    end = content.find("```", start)
                    if end > start:
                        try:
                            result["body"] = json.loads(content[start:end].strip())
                        except json.JSONDecodeError:
                            pass
                        break
            # Fallback: store as text
            if not result["body"]:
                result["body"] = {"text": content}

    prompt_tokens = body.get("usage", {}).get("prompt_tokens", 0)
    completion_tokens = body.get("usage", {}).get("completion_tokens", 0)
    result["tokens"] = {"prompt": prompt_tokens, "completion": completion_tokens}

    return result


def save_result(data: dict, prefix: str, task_id: int, suffix: str = ""):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    name = f"{prefix}_task{task_id:02d}{suffix}_{ts}.json"
    with open(OUTPUT_DIR / name, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return name


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
    body_esc = esc_sql(body_json)
    run_id_esc = esc_sql(run_id)

    return psql_ok(
        f"INSERT INTO activity_log (type, source, title, summary, body, "
        f"agent, model, run_id, summary_status) "
        f"VALUES ('stage', 'pipeline', '{title}', '{summary}', "
        f"'{body_esc}'::jsonb, 'qwen2.5-coder-32b', 'qwen2.5-coder-32b', "
        f"'{run_id_esc}', 'raw')")


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
                    print(f"  Previous task may have triggered container restart.", flush=True)
                    print(f"  Saving partial results and aborting.", flush=True)
                    sys.exit(1)
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

            print(f"\n[32B 4-Stage Pipeline] Starting from stage {args.start_stage}...")
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
