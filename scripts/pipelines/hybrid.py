#!/usr/bin/env python3
# Status: experimental
# Path: none — prototype of code_mod_pipeline. exec()/eval() present, not production-safe.
"""Multi-model code modification pipeline with debate + verification.

Modes:
  hybrid      DeepSeek API plan -> 32B execution with ASSERT validation
  local-multi 3-LLM debate -> 14B coding -> Phi-4 verify -> 32B cross-verify
  web-multi   DeepSeek API plan -> multi-model execution + verification + 32B cross-verify

Architecture:
  Phase 1: Planning (3-LLM debate or DeepSeek API) -> integrated plan + <step> + # ASSERT:
  Phase 2: Execution (14B or 32B) -> step-by-step with ASSERT validation
  Phase 3: Verification (different model reviews correctness)
  Phase 4: Cross-verification (32B compares output, measures divergence)

Infrastructure:
  Pod A (devforge-pod-a):  Qwen3-4B @ 8080  (debate analyst)
  Pod B (devforge-swap):  Phi-4 14B @ 8081  (debate critic + verify)
                           Qwen-14B  @ 8082  (debate pragmatist + execute)
  Mode switch -> code:    Qwen-32B  @ 8081  (cross-verify)
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Optional, Dict, List

from lib.llm.client import call_llm
from lib.code_mod.shared import (
    extract_json_from_llm_response, save_result, read_file, DEEPSEEK_KEY, LLAMA_ENDPOINT,
    TASKS_FILE,
)


def _build_module_namespace(file_path: str) -> dict:
    """Build namespace dict from target Python file, stubbing unavailable imports.

    Some target files (e.g. api/slack_operator.py) import container-only deps
    like fastapi / asyncpg that aren't installable on the host. We create
    lightweight stubs so exec() can build the module-level namespace (router,
    app, etc.) that generated code steps may reference.
    """
    # Modules not available on host — stub them before exec
    _STUBBED = {
        "fastapi": ["APIRouter", "FastAPI", "Request", "Depends", "HTTPException"],
        "fastapi.responses": ["JSONResponse", "HTMLResponse"],
        "asyncpg": ["create_pool", "Connection"],
    }

    saved = {}
    for mod_name, attrs in _STUBBED.items():
        saved[mod_name] = sys.modules.get(mod_name)
        if mod_name not in sys.modules:
            stub = ModuleType(mod_name)
            for attr in attrs:
                setattr(stub, attr, type(attr, (), {}))
            sys.modules[mod_name] = stub

    namespace: dict = {}
    try:
        exec(Path(file_path).read_text(), namespace)
    except Exception:
        pass  # best-effort: whatever loaded is in namespace

    # Restore original modules
    for mod_name, original in saved.items():
        if original is None:
            sys.modules.pop(mod_name, None)
        else:
            sys.modules[mod_name] = original

    return namespace

WEB_PLANNER_URL = "https://api.deepseek.com/v1/chat/completions"
WEB_PLANNER_MODEL = "deepseek-chat"


def detect_models() -> Dict[str, dict]:
    """Auto-detect loaded models on all local ports. Returns {key: {url, model_name}}."""
    available = {}
    ports = [8080, 8081, 8082]
    for port in ports:
        try:
            req = urllib.request.Request(f"http://localhost:{port}/v1/models")
            resp = urllib.request.urlopen(req, timeout=3)
            data = json.loads(resp.read())
            models = data.get("data", [])
            if models:
                model_id = models[0].get("id", "")
                # Map model name to endpoint key
                if "32B" in model_id or "32b" in model_id or "27B" in model_id or "27b" in model_id:
                    available["qwen-32b"] = {
                        "url": f"http://127.0.0.1:{port}/v1/chat/completions",
                        "model": "qwen3.6-27b", "port": port}
                elif "14b" in model_id.lower() or "phi-4" in model_id.lower():
                    # Check if it's Phi-4 or Qwen-14B
                    if "phi" in model_id.lower():
                        available["phi4-14b"] = {
                            "url": f"http://127.0.0.1:{port}/v1/chat/completions",
                            "model": "phi-4-14b", "port": port}
                    else:
                        available["qwen-14b"] = {
                            "url": f"http://127.0.0.1:{port}/v1/chat/completions",
                            "model": "qwen2.5-coder-14b", "port": port}
                elif "4B" in model_id or "4b" in model_id or "Qwen3" in model_id:
                    available["qwen3-4b"] = {
                        "url": f"http://127.0.0.1:{port}/v1/chat/completions",
                        "model": "qwen3-4b", "port": port}
        except Exception:
            pass
    return available


DEBATE_ANALYST_SYSTEM = """You are a SYSTEMATIC CODE ANALYST. Your role in this debate is to provide a thorough, structured analysis.

Given a code file and modification request:
1. Identify ALL affected sections (functions, classes, lines)
2. Classify the change type
3. Map dependencies and side effects
4. Assess risk level

Output JSON:
{"perspective": "analyst",
 "affected_sections": ["func:line_range", ...],
 "change_type": "helper_extraction|bug_fix|validation|cross_function|dedup|interface|structural|analysis",
 "dependencies": ["..."],
 "risk": "low|medium|high",
 "recommended_approach": "..."}"""

DEBATE_CRITIC_SYSTEM = """You are a CODE CRITIC. Your role is to find edge cases, risks, and potential failures.

Given a code file and modification request:
1. What could go wrong with naive changes?
2. What edge cases are NOT handled by the current code?
3. What cross-dependencies might break?
4. What implicit assumptions exist?

Output JSON:
{"perspective": "critic",
 "edge_cases": ["..."],
 "hidden_dependencies": ["..."],
 "implicit_assumptions": ["..."],
 "risk_of_breaking": "low|medium|high",
 "must_preserve": ["behavior that must not change"]}"""

DEBATE_PRAGMATIST_SYSTEM = """You are a PRAGMATIC IMPLEMENTER. Your role is to find the simplest change that fulfills the requirements.

Given a code file and modification request:
1. What is the MINIMAL change that satisfies the task?
2. What can be deferred or simplified?
3. What existing patterns can be reused?
4. How many lines actually need to change?

Output JSON:
{"perspective": "pragmatist",
 "minimal_change_description": "...",
 "estimated_lines": {"add": N, "remove": M},
 "can_reuse": ["existing functions/patterns to reuse"],
 "deferrable": ["things that can be done later"],
 "simplest_implementation_steps": ["step1", ...]}"""

DEBATE_SYNTHESIZER_SYSTEM = """You are a DEBATE MODERATOR. Synthesize 3 expert perspectives into a single consensus plan.

Output format:
<plan>
<analysis>Brief summary of the debate and consensus.</analysis>
<step id="1">
Python code for step 1.
Include this ASSERT comment:
# ASSERT: <boolean expression that must be True after this step>
</step>
<step id="2">
... more steps ...
</step>
</plan>

Rules:
- Each <step> contains executable Python code with NO markdown fences
- Each step MUST have at least one # ASSERT: comment with a concrete, testable boolean expression
- Good ASSERT: # ASSERT: _encode_payload({"a": 1}) == b'{"a": 1}'
- Bad ASSERT: # ASSERT: True
- Keep steps minimal — one logical change per step
- Use proper Python with existing imports/APIs only"""

DEBATE_USER_TEMPLATE = """FILE: {file}
TASK: {task}

CODE:
{code}

Provide your {role} perspective as specified."""

SYNTHESIZER_USER = """TASK: {task}
FILE: {file}

ANALYST says:
{analyst}

CRITIC says:
{critic}

PRAGMATIST says:
{pragmatist}

Synthesize these 3 perspectives into a single consensus plan with steps and ASSERT conditions."""


WEB_PLANNER_SYSTEM = """You are a senior software architect. Given a code modification task, produce a complete implementation plan.

Output format — use EXACTLY these XML tags:
<plan>
<analysis>Brief analysis: what needs to change and why.</analysis>
<step id="1">
Python code for step 1.
Include this ASSERT comment where correctness can be verified:
# ASSERT: <boolean expression that must be True after this step>
</step>
<step id="2">
... more steps as needed ...
</step>
</plan>

Rules:
- Each <step> contains executable Python code with NO markdown fences.
- Each step MUST have at least one # ASSERT: comment with a concrete, testable boolean expression.
- Good ASSERT examples:
    # ASSERT: _encode_payload({"a": 1}) == b'{"a": 1}'
    # ASSERT: hasattr(_post_to_response_url, '__wrapped__') is False
    # ASSERT: isinstance(result, dict) and "status" in result
- Bad ASSERT examples (NEVER use these):
    # ASSERT: True  (meaningless)
    # ASSERT: func.__code__.co_code != func.__code__.co_code  (always True)
- The ASSERT must verify real output/state, not runtime metadata.
- Keep steps minimal — one logical change per step.
- Use proper Python. Do NOT invent imports or APIs that don't exist.
- The code runs in a shared namespace across steps — variables persist."""

WEB_PLANNER_USER = """FILE: {file}
TASK: {task}

CODE:
{code}

Generate a complete implementation plan with ASSERT-validated steps."""


ERROR_CLASSIFIER_PROMPT = """Classify this Python error into exactly one word:
TYPE_ENV — environment issue (missing library, permission denied, file not found for system paths)
TYPE_CODE — code logic/syntax error, NameError, TypeError, AssertionError, wrong variable

Error:
{error}

Classification:"""


VERIFY_SYSTEM = """You are a CODE REVIEWER. Review the following implementation for correctness.

Check:
1. Does it fulfill the task requirements?
2. Are there any bugs, edge cases, or regressions?
3. Is the change minimal and surgical?
4. Are all ASSERT conditions valid and non-trivial?

Output JSON:
{"verdict": "pass|fail|needs_revision",
 "bugs_found": ["..."],
 "edge_cases_missed": ["..."],
 "style_issues": ["..."],
 "confidence": 0.0-1.0,
 "recommendation": "..."}"""

VERIFY_USER = """TASK: {task}

IMPLEMENTATION (executed steps):
{steps}

ORIGINAL CODE:
{code}

Review the implementation and provide your verdict."""


CROSS_VERIFY_SYSTEM = """You are a SENIOR CODE AUDITOR. Compare the multi-model implementation against what you would have produced.

Analyze:
1. Are there meaningful differences between the multi-model output and your approach?
2. Which approach is better and why?
3. What did the multi-model pipeline miss that you would catch?
4. Final confidence score for the multi-model output.

Output JSON:
{"divergence": "none|minor|significant|critical",
 "differences": ["..."],
 "multi_model_better_at": ["..."],
 "my_approach_better_at": ["..."],
 "missed_by_multi_model": ["..."],
 "final_confidence": 0.0-1.0,
 "verdict": "accept|revise|reject"}"""

CROSS_VERIFY_USER = """TASK: {task}
FILE: {file}

ORIGINAL CODE:
{code}

MULTI-MODEL IMPLEMENTATION:
{implementation}

DEBATE ANALYSIS:
{debate_analysis}

VERIFICATION REPORT:
{verify_report}

Compare the multi-model output against your own analysis. What would you have done differently?"""


# Phase 1: Planning (Debate or Web API)

def run_debate_plan(models: dict, file_path: str, task_desc: str) -> Optional[dict]:
    """3-LLM debate: analyst + critic + pragmatist -> synthesizer consensus plan."""
    code = read_file(file_path)
    if not code:
        print(f"  [ERROR] Cannot read {file_path}")
        return None

    # Pick debate participants
    participants = []
    for role, preferred in [("analyst", "qwen3-4b"), ("critic", "phi4-14b"), ("pragmatist", "qwen-14b")]:
        if preferred in models:
            participants.append((role, preferred, models[preferred]))
        elif models:
            # Fallback: use any available model
            key = next(iter(models))
            participants.append((role, key, models[key]))

    if len(participants) < 2:
        print(f"  [ERROR] Need at least 2 models for debate, found {len(participants)}")
        return None

    print(f"  [Debate] Participants: {[(r, k) for r, k, _ in participants]}")

    perspectives = {}
    role_prompts = {
        "analyst": DEBATE_ANALYST_SYSTEM,
        "critic": DEBATE_CRITIC_SYSTEM,
        "pragmatist": DEBATE_PRAGMATIST_SYSTEM,
    }

    for role, key, cfg in participants:
        print(f"  [Debate] Asking {key} ({role})...", flush=True)
        t0 = time.monotonic()
        user_msg = DEBATE_USER_TEMPLATE.format(file=file_path, task=task_desc, code=code, role=role)
        status, body = call_llm(
            cfg["url"],
            [{"role": "system", "content": role_prompts[role]},
             {"role": "user", "content": user_msg}],
            model=cfg["model"], timeout=120, max_tokens=1024,
        )
        elapsed = time.monotonic() - t0
        result = extract_json_from_llm_response((status, body))
        perspectives[role] = result.get("body", result.get("raw_content", ""))
        print(f"    {key} ({role}) done in {elapsed:.0f}s, status={status}", flush=True)

    # Synthesize: use the strongest model among participants
    synthesizer_key = "phi4-14b" if "phi4-14b" in models else list(models.keys())[0]
    synthesizer_cfg = models[synthesizer_key]
    print(f"  [Debate] Synthesizer: {synthesizer_key}", flush=True)

    syn_user = SYNTHESIZER_USER.format(
        task=task_desc, file=file_path,
        analyst=json.dumps(perspectives.get("analyst", {}), indent=2),
        critic=json.dumps(perspectives.get("critic", {}), indent=2),
        pragmatist=json.dumps(perspectives.get("pragmatist", {}), indent=2),
    )

    t0 = time.monotonic()
    status, body = call_llm(
        synthesizer_cfg["url"],
        [{"role": "system", "content": DEBATE_SYNTHESIZER_SYSTEM},
         {"role": "user", "content": syn_user}],
        model=synthesizer_cfg["model"], timeout=180, max_tokens=4096,
    )
    elapsed = time.monotonic() - t0
    result = extract_json_from_llm_response((status, body))
    plan_text = result.get("raw_content", "")
    print(f"  [Debate] Consensus plan in {elapsed:.0f}s ({len(plan_text)} chars)", flush=True)

    # Parse <step> tags
    steps = {}
    analysis = ""
    analysis_match = re.search(r"<analysis>(.*?)</analysis>", plan_text, re.DOTALL)
    if analysis_match:
        analysis = analysis_match.group(1).strip()

    step_pattern = r'<step id="(\d+)">(.*?)</step>'
    for step_id, code_block in re.findall(step_pattern, plan_text, re.DOTALL):
        code_block = re.sub(r'^```[a-z]*\n?', '', code_block.strip())
        code_block = re.sub(r'\n?```$', '', code_block)
        steps[int(step_id)] = code_block.strip()

    if not steps:
        step_pattern2 = r"<step>(.*?)</step>"
        for i, code_block in enumerate(re.findall(step_pattern2, plan_text, re.DOTALL), 1):
            code_block = re.sub(r'^```[a-z]*\n?', '', code_block.strip())
            code_block = re.sub(r'\n?```$', '', code_block)
            steps[i] = code_block.strip()

    if not steps:
        print(f"  [ERROR] No <step> tags found in consensus plan")
        print(f"  Plan text (first 300 chars): {plan_text[:300]}")
        return None

    print(f"  [Debate] Parsed {len(steps)} steps")
    return {
        "analysis": analysis,
        "steps": steps,
        "raw_plan": plan_text,
        "perspectives": {k: str(v)[:500] for k, v in perspectives.items()},
        "elapsed_s": elapsed,
    }


def generate_web_plan(file_path: str, task_desc: str) -> Optional[dict]:
    """Call DeepSeek API to generate a complete <step>-based implementation plan."""
    code = read_file(file_path)
    if not code:
        print(f"  [ERROR] Cannot read {file_path}")
        return None

    user_msg = WEB_PLANNER_USER.format(file=file_path, task=task_desc, code=code)
    messages = [
        {"role": "system", "content": WEB_PLANNER_SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    print(f"  [Phase 1] Calling DeepSeek for integrated plan... ({len(code)//3:,} tok est)")
    t0 = time.monotonic()
    status, body = call_llm(WEB_PLANNER_URL, messages, api_key=DEEPSEEK_KEY,
                            model=WEB_PLANNER_MODEL, timeout=120, max_tokens=4096)
    elapsed = time.monotonic() - t0

    result = extract_json_from_llm_response((status, body))
    if status != 200:
        print(f"  [ERROR] Web planner failed: {result.get('error', 'HTTP {status}')}")
        return None

    plan_text = result.get("raw_content", "")
    print(f"  [Phase 1] Plan received in {elapsed:.0f}s ({len(plan_text)} chars)")

    steps = {}
    analysis = ""
    analysis_match = re.search(r"<analysis>(.*?)</analysis>", plan_text, re.DOTALL)
    if analysis_match:
        analysis = analysis_match.group(1).strip()

    step_pattern = r'<step id="(\d+)">(.*?)</step>'
    for step_id, code_block in re.findall(step_pattern, plan_text, re.DOTALL):
        code_block = re.sub(r'^```[a-z]*\n?', '', code_block.strip())
        code_block = re.sub(r'\n?```$', '', code_block)
        steps[int(step_id)] = code_block.strip()

    if not steps:
        step_pattern2 = r"<step>(.*?)</step>"
        for i, code_block in enumerate(re.findall(step_pattern2, plan_text, re.DOTALL), 1):
            code_block = re.sub(r'^```[a-z]*\n?', '', code_block.strip())
            code_block = re.sub(r'\n?```$', '', code_block)
            steps[i] = code_block.strip()

    if not steps:
        print(f"  [ERROR] No <step> tags found in plan")
        print(f"  Plan text (first 300 chars): {plan_text[:300]}")
        return None

    print(f"  [Phase 1] Parsed {len(steps)} steps, analysis={len(analysis)} chars")
    return {
        "analysis": analysis,
        "steps": steps,
        "raw_plan": plan_text,
        "tokens": result.get("tokens", {}),
        "elapsed_s": elapsed,
    }


# Phase 2: Execution with ASSERT validation

def extract_assert(code: str) -> Optional[str]:
    """Extract ASSERT condition from code comment: # ASSERT: <expr>"""
    m = re.search(r'#\s*ASSERT:\s*(.+)', code)
    return m.group(1).strip() if m else None


def classify_error(error_text: str, models: dict = None) -> str:
    """Classify error as TYPE_ENV or TYPE_CODE using local model.

    If multiple models available, uses the smallest/fastest for quick classification.
    Falls back to heuristic if no models available.
    """
    # Prefer a fast model for classification
    classifier_cfg = None
    if models:
        for pref in ["qwen3-4b", "qwen-14b", "phi4-14b"]:
            if pref in models:
                classifier_cfg = models[pref]
                break

    if classifier_cfg:
        prompt = ERROR_CLASSIFIER_PROMPT.format(error=error_text[:2000])
        status, body = call_llm(
            classifier_cfg["url"],
            [{"role": "user", "content": prompt}],
            model=classifier_cfg["model"], timeout=30, max_tokens=16,
        )
        if status == 200:
            try:
                response = body["choices"][0]["message"]["content"].strip().upper()
                if "TYPE_ENV" in response:
                    return "TYPE_ENV"
            except Exception:
                pass
    else:
        # Heuristic fallback
        env_markers = ["ModuleNotFoundError", "ImportError", "PermissionError",
                        "FileNotFoundError", "No such file"]
        for marker in env_markers:
            if marker in error_text:
                return "TYPE_ENV"
    return "TYPE_CODE"


def request_web_fix(step_id: int, code: str, error: str, context: str) -> Optional[str]:
    """Request code fix from DeepSeek for a failed step."""
    prompt = WEB_FIX_PROMPT.format(step_id=step_id, code=code, error=error[:1500], context=context)
    print(f"  [WebFix] Requesting web fix for step {step_id}...")
    status, body = call_llm(
        WEB_PLANNER_URL,
        [{"role": "user", "content": prompt}],
        api_key=DEEPSEEK_KEY, model=WEB_PLANNER_MODEL,
        timeout=60, max_tokens=2048,
    )
    if status == 200:
        result = extract_json_from_llm_response((status, body))
        fixed = result.get("raw_content", "").strip()
        fixed = re.sub(r'^```[a-z]*\n?', '', fixed)
        fixed = re.sub(r'\n?```$', '', fixed)
        return fixed
    return None


def request_local_fix(step_id: int, code: str, error: str, context: str,
                      executor_cfg: dict) -> Optional[str]:
    """Request code fix from a local model for a failed step."""
    prompt = WEB_FIX_PROMPT.format(step_id=step_id, code=code, error=error[:1500], context=context)
    print(f"  [LocalFix] Requesting fix from {executor_cfg.get('model', '?')} for step {step_id}...")
    status, body = call_llm(
        executor_cfg["url"],
        [{"role": "user", "content": prompt}],
        model=executor_cfg["model"], timeout=60, max_tokens=1024,
    )
    if status == 200:
        result = extract_json_from_llm_response((status, body))
        fixed = result.get("raw_content", "").strip()
        fixed = re.sub(r'^```[a-z]*\n?', '', fixed)
        fixed = re.sub(r'\n?```$', '', fixed)
        return fixed
    return None


WEB_FIX_PROMPT = """Fix this Python code that failed during execution. Output ONLY the fixed code, no explanation.

ERROR:
{error}

FAILED CODE (step {step_id}):
{code}

ALL PREVIOUS STEPS (for context):
{context}

Fixed code for step {step_id}:"""


def execute_steps(steps: dict, executor_cfg: dict, models: dict = None,
                  use_web_fix: bool = False, max_retries: int = 2,
                  target_file: Optional[str] = None) -> dict:
    """Execute steps sequentially, validate ASSERTs, retry on TYPE_CODE.

    target_file: If provided, the file's module-level namespace is loaded
                 as the base namespace so generated code can reference
                 module-level objects (router, app, etc.).
    """
    namespace = _build_module_namespace(target_file) if target_file else {}
    results = {}
    context_steps = []

    for step_id in sorted(steps.keys()):
        code = steps[step_id]
        print(f"\n  [Phase 2] Step {step_id}/{len(steps)}...", flush=True)

        for attempt in range(max_retries + 1):
            error = None
            try:
                exec(code, namespace)
                cond = extract_assert(code)
                if cond:
                    if not eval(cond, namespace):
                        error = f"ASSERT failed: {cond}"
                if not error:
                    results[step_id] = {"status": "ok", "attempts": attempt + 1}
                    context_steps.append(f"Step {step_id} (OK): {code[:200]}...")
                    print(f"    OK (attempt {attempt+1})", flush=True)
                    break
            except Exception:
                import traceback
                error = traceback.format_exc()

            if error:
                print(f"    FAIL (attempt {attempt+1}): {error[:120]}", flush=True)
                err_type = classify_error(error, models)
                print(f"    Classified: {err_type}", flush=True)

                if err_type == "TYPE_ENV":
                    results[step_id] = {"status": "fatal_env", "error": error[:500]}
                    return results

                if attempt < max_retries:
                    context = "\n\n".join(context_steps)
                    if use_web_fix:
                        fixed = request_web_fix(step_id, code, error, context)
                    else:
                        fixed = request_local_fix(step_id, code, error, context, executor_cfg)
                    if fixed:
                        print(f"    Received fixed code ({len(fixed)} chars)")
                        steps[step_id] = fixed
                        code = fixed
                else:
                    results[step_id] = {"status": "failed", "error": error[:500],
                                        "attempts": attempt + 1}

        if step_id not in results:
            results[step_id] = {"status": "failed", "error": error[:500] if error else "unknown"}

    return results


# Phase 3: Verification

def run_verification(task: dict, plan: dict, exec_results: dict,
                     verifier_cfg: dict, code: str) -> dict:
    """Have a different model review the implementation for correctness."""
    steps_text = []
    for sid in sorted(exec_results.keys()):
        r = exec_results[sid]
        steps_text.append(f"Step {sid} ({r['status']}):\n{plan['steps'].get(sid, '')[:300]}")

    user_msg = VERIFY_USER.format(
        task=task["description"][:1000],
        steps="\n\n".join(steps_text),
        code=code[:8000],
    )

    print(f"  [Phase 3] Verification by {verifier_cfg['model']}...", flush=True)
    t0 = time.monotonic()
    status, body = call_llm(
        verifier_cfg["url"],
        [{"role": "system", "content": VERIFY_SYSTEM},
         {"role": "user", "content": user_msg}],
        model=verifier_cfg["model"], timeout=120, max_tokens=1024,
    )
    elapsed = time.monotonic() - t0

    result = extract_json_from_llm_response((status, body))
    print(f"  [Phase 3] Verification done in {elapsed:.0f}s, status={status}", flush=True)
    return {
        "verifier_model": verifier_cfg["model"],
        "body": result.get("body", {}),
        "raw": result.get("raw_content", "")[:500],
        "elapsed_s": elapsed,
    }


# Phase 4: Cross-verification by 32B

def run_cross_verify(task: dict, plan: dict, exec_results: dict,
                     verify_result: dict, code: str,
                     models: dict) -> Optional[dict]:
    """32B cross-verification: compare multi-model output against 32B's analysis."""
    cross_cfg = models.get("qwen-32b")
    if not cross_cfg:
        print("  [Phase 4] 32B not available — skipping cross-verification")
        return {"status": "skipped", "reason": "32B model not loaded"}

    implementation = json.dumps({
        f"step{sid}": r for sid, r in exec_results.items()
    }, indent=2)

    user_msg = CROSS_VERIFY_USER.format(
        task=task["description"][:1000],
        file=task["file"],
        code=code[:4000],
        implementation=implementation[:3000],
        debate_analysis=plan.get("analysis", "")[:1000],
        verify_report=json.dumps(verify_result.get("body", {}), indent=2)[:1000],
    )

    print(f"  [Phase 4] Cross-verification by 32B...", flush=True)
    t0 = time.monotonic()
    status, body = call_llm(
        cross_cfg["url"],
        [{"role": "system", "content": CROSS_VERIFY_SYSTEM},
         {"role": "user", "content": user_msg}],
        model=cross_cfg["model"], timeout=600, max_tokens=2048,
    )
    elapsed = time.monotonic() - t0

    result = extract_json_from_llm_response((status, body))
    print(f"  [Phase 4] Cross-verification done in {elapsed:.0f}s, status={status}", flush=True)
    return {
        "cross_model": cross_cfg["model"],
        "body": result.get("body", {}),
        "raw": result.get("raw_content", "")[:500],
        "elapsed_s": elapsed,
    }


# Pipeline runners: hybrid (original), local-multi, web-multi

def run_hybrid(task: dict) -> dict:
    """Original hybrid pipeline: DeepSeek plan + 32B execution with ASSERT."""
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    print(f"\n{'='*60}")
    print(f"Hybrid Pipeline (DeepSeek + 32B): {task['name']}")
    print(f"File: {task['file']}")
    print(f"{'='*60}")

    plan = generate_web_plan(task["file"], task["description"])
    if not plan:
        return {"error": "Web plan generation failed", "task_id": task["id"]}

    # Detect 32B for execution
    models = detect_models()
    executor_cfg = models.get("qwen-32b", {"url": LLAMA_ENDPOINT, "model": "qwen2.5-coder-32b"})

    exec_results = execute_steps(plan["steps"], executor_cfg, models, use_web_fix=True, target_file=task["file"])

    ok_count = sum(1 for r in exec_results.values() if r["status"] == "ok")
    total = len(exec_results)
    print(f"\n  Execution: {ok_count}/{total} steps OK")

    elapsed = time.monotonic() - t0
    return {
        "task_id": task["id"],
        "task_name": task["name"],
        "file": task["file"],
        "mode": "hybrid",
        "started": started,
        "finished": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": elapsed,
        "plan": {
            "analysis": plan["analysis"],
            "steps_count": len(plan["steps"]),
            "tokens": plan.get("tokens", {}),
            "elapsed_s": plan["elapsed_s"],
        },
        "execution": {
            "steps_total": total,
            "steps_ok": ok_count,
            "step_results": exec_results,
        },
    }


def run_local_multi(task: dict) -> dict:
    """Pure local multi-model pipeline: 3-LLM debate -> 14B exec -> Phi-4 verify -> 32B cross-verify."""
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    print(f"\n{'='*60}")
    print(f"Local Multi-Model Pipeline: {task['name']}")
    print(f"File: {task['file']}")
    print(f"{'='*60}")

    models = detect_models()
    print(f"  Available models: {list(models.keys())}")
    if len(models) < 2:
        return {"error": f"Need >=2 models for multi-model pipeline, found {len(models)}",
                "task_id": task["id"]}

    code = read_file(task["file"])

    # Phase 1: 3-LLM Debate
    plan = run_debate_plan(models, task["file"], task["description"])
    if not plan:
        return {"error": "Debate plan generation failed", "task_id": task["id"]}

    # Phase 2: Execution (prefer Qwen-14B, fallback to Phi-4 14B or any available)
    executor_key = "qwen-14b" if "qwen-14b" in models else \
                   "phi4-14b" if "phi4-14b" in models else \
                   list(models.keys())[0]
    executor_cfg = models[executor_key]
    print(f"\n  Executor: {executor_key} ({executor_cfg['model']})")

    exec_results = execute_steps(plan["steps"], executor_cfg, models, use_web_fix=False, target_file=task["file"])
    ok_count = sum(1 for r in exec_results.values() if r["status"] == "ok")
    print(f"  Execution: {ok_count}/{len(exec_results)} steps OK")

    # Phase 3: Verification (use different model from executor)
    verifier_key = None
    for pref in ["phi4-14b", "qwen3-4b", "qwen-14b"]:
        if pref in models and pref != executor_key:
            verifier_key = pref
            break
    if not verifier_key:
        verifier_key = next((k for k in models if k != executor_key), executor_key)
    verifier_cfg = models[verifier_key]
    print(f"  Verifier: {verifier_key} ({verifier_cfg['model']})")

    verify_result = run_verification(task, plan, exec_results, verifier_cfg, code)

    # Phase 4: Cross-verification by 32B
    cross_result = run_cross_verify(task, plan, exec_results, verify_result, code, models)

    elapsed = time.monotonic() - t0

    pkg = verify_result.get("body", {})
    cross_pkg = cross_result.get("body", {}) if cross_result else {}

    return {
        "task_id": task["id"],
        "task_name": task["name"],
        "file": task["file"],
        "mode": "local-multi",
        "started": started,
        "finished": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": elapsed,
        "models_available": list(models.keys()),
        "executor": executor_key,
        "verifier": verifier_key,
        "plan": {
            "analysis": plan["analysis"],
            "steps_count": len(plan["steps"]),
            "elapsed_s": plan.get("elapsed_s", 0),
        },
        "execution": {
            "steps_total": len(exec_results),
            "steps_ok": ok_count,
            "step_results": {str(k): v for k, v in exec_results.items()},
        },
        "verification": verify_result,
        "cross_verification": cross_result,
        "final_confidence": cross_pkg.get("final_confidence") or pkg.get("confidence"),
        "verdict": cross_pkg.get("verdict") or pkg.get("verdict", "unknown"),
    }


def run_web_multi(task: dict) -> dict:
    """Web-multi pipeline: DeepSeek plan + local multi-model exec + verify + 32B cross-verify."""
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    print(f"\n{'='*60}")
    print(f"Web-Multi Pipeline (DeepSeek + Multi-Model): {task['name']}")
    print(f"File: {task['file']}")
    print(f"{'='*60}")

    models = detect_models()
    print(f"  Available models: {list(models.keys())}")

    code = read_file(task["file"])

    # Phase 1: DeepSeek API plan
    plan = generate_web_plan(task["file"], task["description"])
    if not plan:
        return {"error": "Web plan generation failed", "task_id": task["id"]}

    # Phase 2: Execution (prefer Qwen-14B if available, else 32B)
    executor_key = "qwen-14b" if "qwen-14b" in models else \
                   "qwen-32b" if "qwen-32b" in models else \
                   list(models.keys())[0]
    executor_cfg = models.get(executor_key, {"url": LLAMA_ENDPOINT, "model": "qwen2.5-coder-32b"})
    print(f"\n  Executor: {executor_key} ({executor_cfg.get('model', '?')})")

    exec_results = execute_steps(plan["steps"], executor_cfg, models, use_web_fix=True, target_file=task["file"])
    ok_count = sum(1 for r in exec_results.values() if r["status"] == "ok")
    print(f"  Execution: {ok_count}/{len(exec_results)} steps OK")

    # Phase 3: Verification (different model if available)
    verify_result = {}
    verifier_key = None
    for pref in ["phi4-14b", "qwen3-4b", "qwen-14b"]:
        if pref in models and pref != executor_key:
            verifier_key = pref
            break
    if verifier_key:
        verifier_cfg = models[verifier_key]
        print(f"  Verifier: {verifier_key} ({verifier_cfg['model']})")
        verify_result = run_verification(task, plan, exec_results, verifier_cfg, code)

    # Phase 4: Cross-verification by 32B
    cross_result = run_cross_verify(task, plan, exec_results, verify_result, code, models)

    elapsed = time.monotonic() - t0

    cross_pkg = cross_result.get("body", {}) if cross_result else {}
    verify_pkg = verify_result.get("body", {}) if verify_result else {}

    return {
        "task_id": task["id"],
        "task_name": task["name"],
        "file": task["file"],
        "mode": "web-multi",
        "started": started,
        "finished": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": elapsed,
        "models_available": list(models.keys()),
        "executor": executor_key,
        "verifier": verifier_key,
        "plan": {
            "analysis": plan["analysis"],
            "steps_count": len(plan["steps"]),
            "tokens": plan.get("tokens", {}),
            "elapsed_s": plan["elapsed_s"],
        },
        "execution": {
            "steps_total": len(exec_results),
            "steps_ok": ok_count,
            "step_results": {str(k): v for k, v in exec_results.items()},
        },
        "verification": verify_result,
        "cross_verification": cross_result,
        "final_confidence": cross_pkg.get("final_confidence") or verify_pkg.get("confidence"),
        "verdict": cross_pkg.get("verdict") or verify_pkg.get("verdict", "unknown"),
    }


# Main

def main():
    import argparse
    import yaml

    ap = argparse.ArgumentParser(description="Multi-model code modification pipeline")
    ap.add_argument("--task", type=int, help="Run single task by ID")
    ap.add_argument("--mode", choices=["hybrid", "local-multi", "web-multi"],
                    default="hybrid",
                    help="Pipeline mode: hybrid (DeepSeek+32B), local-multi (all local), "
                         "web-multi (DeepSeek+multi-model)")
    ap.add_argument("--dry-run", action="store_true", help="Generate plan only, don't execute")
    ap.add_argument("--list-models", action="store_true", help="Detect and list available models")
    args = ap.parse_args()

    if args.list_models:
        models = detect_models()
        if models:
            print("Available models:")
            for key, cfg in sorted(models.items()):
                print(f"  {key}: {cfg['model']} @ port {cfg['port']}")
        else:
            print("No local models detected.")
        return

    config = yaml.safe_load(open(TASKS_FILE))
    tasks = config["tasks"]

    if args.task:
        tasks = [t for t in tasks if t["id"] == args.task]
        if not tasks:
            print(f"Task {args.task} not found")
            sys.exit(1)

    for task in tasks:
        if args.dry_run:
            if args.mode == "local-multi":
                models = detect_models()
                plan = run_debate_plan(models, task["file"], task["description"])
            else:
                plan = generate_web_plan(task["file"], task["description"])
            if plan:
                print(f"\nAnalysis: {plan['analysis'][:300]}")
                for sid in sorted(plan["steps"]):
                    print(f"\n--- Step {sid} ---")
                    print(plan["steps"][sid][:400])
        else:
            if args.mode == "local-multi":
                result = run_local_multi(task)
            elif args.mode == "web-multi":
                result = run_web_multi(task)
            else:
                result = run_hybrid(task)

            prefix = {"hybrid": "hybrid", "local-multi": "localmulti", "web-multi": "webmulti"}
            name = save_result(result, prefix[args.mode], task["id"])

            if "execution" in result:
                ok = result["execution"]["steps_ok"]
                total = result["execution"]["steps_total"]
                print(f"\n  Result: {ok}/{total} OK, elapsed={result['elapsed_s']:.0f}s")
            print(f"  Saved: {name}")

            if "verdict" in result:
                print(f"  Verdict: {result['verdict']}, Confidence: {result.get('final_confidence', 'N/A')}")


if __name__ == "__main__":
    main()
