#!/usr/bin/env python3
# Status: production
# Path: manual — interactive CLI
"""DevForge Orchestrator — LLM-based smart router to specialized pipelines.

Architecture:
  1. Classify user intent using 3B model (lightweight, fast)
  2. Route to the appropriate pipeline or answer directly
  3. Return structured result

Tools (wrapped pipelines):
  - run_p_r_j_pipeline: P→R→J pipeline (proposal → reflection → scoring judgment)
  - run_debate:          30B proposer vs 3B refuter (DART)
  - run_code_review:     3-model code review pipeline
  - execute_code:        Podman-isolated Python sandbox

Usage:
    from orchestrator import orchestrator_run
    r = orchestrator_run("Build a REST API")
    print(r["output"])

CLI:
    python3 orchestrator.py --input "Build a REST API"
    python3 orchestrator.py --list-tools
    python3 orchestrator.py --classify "Hello"          # dry-run classify
"""

import json
import os
import sys
from typing import Any, Dict

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from lib.llm_client import call_llm  # noqa: E402

# ── Constants ──────────────────────────────────────────────────────────────
TIMEOUT_CLASSIFY = 30  # classification is fast (< 5s)
TIMEOUT_DIRECT = 120  # direct answer generation
MAX_TOKENS_CLASSIFY = 32  # classification needs ~1 token
MAX_TOKENS_DIRECT = 1024  # direct answer length
MAX_TOKENS_EXTRACT = 2048  # code extraction from user input

# Category name → tool key mapping (mirrors proxy MODEL_MAP pattern)
CATEGORY_MAP = {
    "direct_answer": None,  # no tool, answer directly
    "p_r_j": "p_r_j",  # P→R→J pipeline
    "debate": "debate",  # DART debate
    "code_review": "code_review",  # 3-model code review
    "sandbox": "sandbox",  # Podman sandbox
}

CLASSIFY_PROMPT = """\
Classify the following user request into EXACTLY ONE category.
Reply with ONLY the category word, nothing else.

Categories:
- direct_answer  — Q&A, simple explanation, quick coding snippet, general chat
- p_r_j          — Complex code review needing P→R→J pipeline
- debate         — Problem needing adversarial multi-model debate
- code_review    — Explicit code review request (requires task ID from task DB)
- sandbox        — User wants to run/test Python code safely

User request: {input}
Category:"""

DIRECT_SYSTEM_PROMPT = "You are a helpful coding assistant. Answer concisely."

CODE_EXTRACT_PROMPT = """\
Extract Python code from the user request.
Output ONLY the code, no explanation."""


# ── Tool abstraction ───────────────────────────────────────────────────────


class OrchestratorTool:
    """Base class for orchestrator tools."""

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}  # JSON Schema for args

    def execute(self, args: Dict[str, Any]) -> str:
        """Execute the tool and return a string summary."""
        raise NotImplementedError


class RunPRJTool(OrchestratorTool):
    """P→R→J pipeline: proposal → reflection → scoring judgment."""

    name = "run_p_r_j_pipeline"
    description = "3-model code review pipeline (proposer + reflector + judge)"
    parameters = {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "The code or task to review",
            },
        },
        "required": ["input"],
    }

    def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        from review_pipeline_3model import run_full_review

        user_input = args.get("input", "")
        if not user_input:
            return {"error": "No input provided"}
        result = run_full_review(user_input, task_label=user_input[:60])
        scoring = result.get("scoring", {})
        return {
            "route": "full_pipeline",
            "findings": len(result.get("report", {}).get("findings", [])),
            "approved": result.get("approved_count", 0),
            "diff_length": len(result.get("diff", "")),
            "confidence": result.get("confidence", 0),
            "action": scoring.get("next_state", "manual_review"),
            "P_score": scoring.get("P_score", 0),
            "R_score": scoring.get("R_score", 0),
            "gap": scoring.get("gap", 0),
            "is_veto": scoring.get("is_veto", False),
        }


class RunDebateTool(OrchestratorTool):
    """DART debate: 30B proposer vs 3B refuter with judge."""

    name = "run_debate"
    description = "Multi-agent DART debate (30B vs 3B) for adversarial problem-solving"
    parameters = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question or problem to debate",
            },
        },
        "required": ["question"],
    }

    def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        from lib.debate.local_debate import LocalDebate  # type: ignore

        question = args.get("question", "")
        if not question:
            return {"error": "No question provided"}
        try:
            session = LocalDebate(question=question, method="drag")
            result = session.run_session()
            if result:
                return {
                    "status": "completed",
                    "conclusion": (result.get("conclusion") or "")[:500],
                    "rounds": result.get("round", 0),
                }
            return {"error": "Debate returned no result"}
        except Exception as e:
            return {"error": str(e)}


class RunCodeReviewTool(OrchestratorTool):
    """Code review pipeline — trigger review for a task."""

    name = "run_code_review"
    description = "Trigger the 3-model code review pipeline for a task in the DB"
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Task ID from the task DB (e.g., T01)",
            },
        },
        "required": ["task_id"],
    }

    def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        import subprocess

        task_id = args.get("task_id", "")
        if not task_id:
            return {"error": "No task_id provided"}
        try:
            proc = subprocess.run(
                [sys.executable, "review_pipeline_3model.py", "--task-id", task_id],
                capture_output=True,
                timeout=600,
                text=True,
            )
            return {
                "exit_code": proc.returncode,
                "output": (proc.stdout or "")[-1000:],
                "error": (proc.stderr or "")[-500:],
            }
        except subprocess.TimeoutExpired:
            return {"error": "Code review timed out after 600s"}
        except Exception as e:
            return {"error": str(e)}


class ExecuteCodeTool(OrchestratorTool):
    """Execute Python code in isolated Podman sandbox."""

    name = "execute_code"
    description = "Run Python code safely in an isolated sandbox (read-only, no network)"
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python source code to execute",
            },
        },
        "required": ["code"],
    }

    def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        from lib.test_sandbox import run_code_in_sandbox  # type: ignore

        code = args.get("code", "")
        if not code:
            return {"error": "No code provided"}
        result = run_code_in_sandbox(code)
        return {
            "ok": result.get("ok", False),
            "stdout": (result.get("stdout") or "")[:1000],
            "stderr": (result.get("stderr") or "")[:500],
            "exit_code": result.get("exit_code", -1),
        }


# ── Built-in tool registry ─────────────────────────────────────────────────
_BUILTIN_TOOLS: Dict[str, OrchestratorTool] = {
    "p_r_j": RunPRJTool(),
    "debate": RunDebateTool(),
    "code_review": RunCodeReviewTool(),
    "sandbox": ExecuteCodeTool(),
}


# ── Classification ─────────────────────────────────────────────────────────


def _classify(user_input: str, verbose: bool = True) -> str:
    """Use the 3B model to classify a user request into a routing category.

    The classifier uses a short, constrained prompt (``CLASSIFY_PROMPT``) with
    ``max_tokens=32`` so the model returns a single category word.  Non-matching
    responses fall back to fuzzy matching, then to ``direct_answer``.

    Args:
        user_input: Raw user request string.
        verbose: Emit progress messages to stderr.

    Returns:
        One of ``direct_answer``, ``p_r_j``, ``debate``, ``code_review``,
        or ``sandbox``.
    """
    if verbose:
        print("  [Orch] Classifying...", file=sys.stderr)
    prompt = CLASSIFY_PROMPT.format(input=user_input[:500])
    messages = [{"role": "user", "content": prompt}]
    try:
        response = call_llm(messages, model="extractor", max_tokens=MAX_TOKENS_CLASSIFY, timeout=TIMEOUT_CLASSIFY)
    except RuntimeError:
        return "direct_answer"  # safe fallback
    category = response.strip().lower().split("\n")[0].strip()
    category = category.replace(".", "").replace('"', "").strip()
    valid = set(CATEGORY_MAP.keys())
    if category in valid:
        return category
    for v in valid:
        if v in category:
            return v
    return "direct_answer"


# ── Orchestrator ───────────────────────────────────────────────────────────


def orchestrator_run(
    user_input: str,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Route a user request to the appropriate pipeline or answer directly.

    Two-phase dispatch:
      1. Classify the request using the 3B model (fast, few-shot).
      2. Route to the matching tool or answer directly via the 3B model.

    Args:
        user_input: The user's request string.
        verbose: Emit progress messages to stderr.

    Returns:
        Dict with keys:
          - status: "ok" or "error"
          - output: Response text (answer, summary, or error message)
          - tool: Name of the tool used, or None for direct answers
    """
    category = _classify(user_input, verbose=verbose)

    if verbose:
        print(f"  [Orch] Category: {category}", file=sys.stderr)

    # ── Direct answer (no tool) ──────────────────────────────────────────
    if category == "direct_answer":
        if verbose:
            print("  [Orch] Answering directly with 3B...", file=sys.stderr)
        messages = [
            {"role": "system", "content": DIRECT_SYSTEM_PROMPT},
            {"role": "user", "content": user_input},
        ]
        try:
            response = call_llm(messages, model="extractor", max_tokens=MAX_TOKENS_DIRECT, timeout=TIMEOUT_DIRECT)
        except RuntimeError as e:
            return {"status": "error", "output": str(e)}
        return {"status": "ok", "output": response, "tool": None}

    # ── Tool routing ─────────────────────────────────────────────────────
    tool_key = CATEGORY_MAP.get(category)
    if not tool_key:
        return {"status": "error", "output": f"Unknown category: {category}"}
    tool = _BUILTIN_TOOLS.get(tool_key)
    if not tool:
        return {"status": "error", "output": f"No tool registered for: {tool_key}"}

    if verbose:
        print(f"  [Orch] → Tool: {tool.name}", file=sys.stderr)

    try:
        if category == "p_r_j":
            result = tool.execute({"input": user_input})
        elif category == "debate":
            result = tool.execute({"question": user_input})
        elif category == "sandbox":
            # Extract code via classifier model, then strip fences
            messages = [
                {"role": "system", "content": CODE_EXTRACT_PROMPT},
                {"role": "user", "content": user_input},
            ]
            try:
                extracted = call_llm(messages, model="extractor", max_tokens=MAX_TOKENS_EXTRACT)
            except RuntimeError as e:
                return {"status": "error", "output": f"Code extraction failed: {e}"}
            cleaned = _strip_code_fences(extracted)
            result = tool.execute({"code": cleaned})
        else:
            result = tool.execute({"input": user_input})
    except Exception as e:
        return {"status": "error", "output": str(e), "tool": tool.name}

    # ── Format result ────────────────────────────────────────────────────
    if isinstance(result, dict) and "error" in result:
        return {"status": "error", "output": result["error"], "tool": tool.name}

    output = _format_result(category, result)
    return {"status": "ok", "output": output, "tool": tool.name}


def _strip_code_fences(text: str) -> str:
    """Strip markdown code fences (`` ```python `` etc.) from extracted code.

    Mirrors the proxy's ``_cleanup`` approach: returns the first non-empty
    code block found, or the original text if no fences are present.

    Args:
        text: Raw model output potentially containing markdown code blocks.

    Returns:
        Clean code string with fences removed.
    """
    cleaned = text.strip()
    if "```" not in cleaned:
        return cleaned
    for block in cleaned.split("```"):
        block = block.strip()
        if not block:
            continue
        block = block.removeprefix("python").strip()
        if block:
            return block
    return cleaned


def _format_result(category: str, result: Dict[str, Any]) -> str:
    """Format a tool result dict into a human-readable string.

    Each category has a dedicated format to extract and present the most
    relevant fields from the tool result.

    Args:
        category: The routing category that produced *result*.
        result: Raw result dict from the tool execution.

    Returns:
        Formatted output string.
    """
    if category == "p_r_j":
        route = result.get("route", "?")
        if route == "full_pipeline":
            return (
                f"Total score: {result.get('total', '?')}/30\n"
                f"Winner: {result.get('winner', '?')}\n"
                f"Iterations: {result.get('iterations', 0)}\n\n"
                f"--- Output ---\n{result.get('output', '')}"
            )
        return result.get("output", result.get("summary", ""))
    elif category == "debate":
        return f"Conclusion: {result.get('conclusion', 'N/A')}\nRounds: {result.get('rounds', 0)}"
    elif category == "sandbox":
        parts = [
            f"Exit code: {result.get('exit_code', -1)}",
            f"OK: {result.get('ok', False)}",
            f"stdout:\n{result.get('stdout', '')}",
        ]
        if result.get("stderr"):
            parts.append(f"stderr:\n{result['stderr']}")
        return "\n".join(parts)
    return json.dumps(result, indent=2)


def list_tools() -> None:
    """Print all registered tools with their names, descriptions, and JSON schemas.

    Mirrors the proxy's self-documentation pattern by printing structured
    tool metadata for human inspection.  Called via ``--list-tools``.
    """
    print("Available Orchestrator Tools:\n")
    for key, tool in _BUILTIN_TOOLS.items():
        print(f"  [{key}] {tool.name}")
        print(f"    Description: {tool.description}")
        print(f"    Schema: {json.dumps(tool.parameters, indent=4)}")
        print()


# ── CLI ────────────────────────────────────────────────────────────────────


def main():
    """CLI entry point.  Parses ``--input``, ``--classify``, ``--list-tools``."""
    import argparse

    parser = argparse.ArgumentParser(description="DevForge Orchestrator — LLM router to pipelines")
    parser.add_argument("--input", "-i", help="User request to process")
    parser.add_argument("--classify", help="Dry-run: classify a request only")
    parser.add_argument(
        "--list-tools", "-l", action="store_true", help="List available tools and exit"
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress verbose output")
    args = parser.parse_args()

    if args.list_tools:
        list_tools()
        return

    if args.classify:
        cat = _classify(args.classify, verbose=True)
        print(f"\nClassification: {cat}")
        return

    if not args.input:
        parser.print_help()
        return

    result = orchestrator_run(args.input, verbose=not args.quiet)

    status = result.get("status", "?")
    output = result.get("output", "")
    tool = result.get("tool", "none")

    print(f"\n{'=' * 60}")
    print(f"Status: {status.upper()}")
    print(f"Tool: {tool}")
    print(f"{'=' * 60}\n")
    print(output)


if __name__ == "__main__":
    main()
