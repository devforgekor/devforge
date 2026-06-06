#!/usr/bin/env python3
# Status: production
# Path: manual — interactive CLI
"""DevForge Observer Agent — interactive CLI agent with tool calling.

Routes natural language commands to DevForge pipelines, monitors execution,
and reports errors with root-cause analysis.

Model: Qwen3-Coder-30B-A3B (http://127.0.0.1:8080)

Usage:
    obs                          # Interactive REPL
    obs "check pipeline status"  # One-shot command
    obs --debug                  # Verbose tool traces

Internal commands (in REPL):
    /help     Show available tools
    /tools    List all tools with descriptions
    /clear    Reset conversation history
    /bye      Exit
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ── Configuration ──────────────────────────────────────────────────────────

DEFAULT_MODEL = "Qwen3-Coder-30B-A3B-Instruct-Q4_K_S.gguf"
DEFAULT_API = "http://127.0.0.1:8080"
MODEL = os.environ.get("OBS_MODEL", DEFAULT_MODEL)
API_URL = os.environ.get("OBS_API", DEFAULT_API)

SYSTEM_PROMPT = """\
You are DevForge Observer, an AI agent that monitors and controls the DevForge pipeline.

Your job:
1. Monitor the pipeline (orchestrator, cooperative debate, nightly batch)
2. Report errors with root cause analysis and fix suggestions
3. Execute tasks via available tools when asked
4. Answer in Korean when the user speaks Korean

You have access to tools that let you check system status, query the database,
run the orchestrator pipeline, and analyze errors.

Guidelines:
- Use tools proactively when you need information
- Explain what you found in clear terms
- For errors, identify the root cause and suggest fixes
- Keep responses concise but informative\
"""

OBS_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(OBS_SCRIPTS)
sys.path.insert(0, PROJECT_ROOT)


# ── Tool Definitions ──────────────────────────────────────────────────────


TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "check_status",
            "description": "Check server and pipeline health: containers, services, disk, nightly status",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_logs",
            "description": "Check recent journald logs or file logs for errors. Optionally filter by service.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "description": "Service name or log file pattern (e.g. 'nightly', 'orchestrator', 'caddy', 'postgres')",
                    },
                    "lines": {
                        "type": "integer",
                        "description": "Number of recent lines to check (default 20)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_orchestrator",
            "description": "Route a task to the DevForge orchestrator pipeline (classify + execute)",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Natural language task description",
                    },
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_db",
            "description": "Run a read-only SQL query on the devforge_app database",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "SELECT query to run (read-only)",
                    },
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_nightly",
            "description": "Check nightly batch status, last run time, and phase results",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_error",
            "description": "Deep-dive analysis of a recent error: check logs, DB, and containers",
            "parameters": {
                "type": "object",
                "properties": {
                    "context": {
                        "type": "string",
                        "description": "What failed or what to investigate",
                    },
                },
                "required": ["context"],
            },
        },
    },
]


# ── Tool Implementations ─────────────────────────────────────────────────


def tool_check_status() -> str:
    """Gather server health summary."""
    parts = []

    # Containers
    try:
        r = subprocess.run(
            ["podman", "ps", "--format", "{{.Names}}:{{.Status}}"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
            parts.append(f"Containers ({len(lines)}):")
            for l in lines:
                parts.append(f"  {l}")
    except Exception as e:
        parts.append(f"  podman error: {e}")

    # Disk
    for mp in ["/", "/mnt/lv_db", "/mnt/secure_meta", "/opt/projects"]:
        try:
            r = subprocess.run(
                ["df", "-h", mp], capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0:
                lines = r.stdout.strip().splitlines()
                if len(lines) >= 2:
                    parts.append(f"Disk {mp}: {lines[1].split()[3]} free")
        except Exception:
            pass

    # Nightly status file
    sf = "/opt/projects/server/data/nightly_status.yaml"
    if os.path.exists(sf):
        try:
            with open(sf) as f:
                parts.append(f"Nightly status:\n{f.read().strip()}")
        except Exception:
            pass

    # Services
    for svc in ["caddy", "netdata"]:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", svc], capture_output=True, text=True, timeout=5
            )
            parts.append(f"Service {svc}: {r.stdout.strip()}")
        except Exception:
            pass

    return "\n".join(parts) if parts else "No status data available."


def tool_check_logs(service: str = "", lines: int = 20) -> str:
    """Grab recent log lines."""
    if service in ("caddy", "netdata"):
        try:
            r = subprocess.run(
                ["journalctl", "-u", service, "-n", str(lines), "--no-pager"],
                capture_output=True, text=True, timeout=10
            )
            out = r.stdout.strip() if r.returncode == 0 else r.stderr.strip()
            return f"journalctl -u {service} ({lines} lines):\n{out[:2000]}"
        except Exception as e:
            return f"Error reading {service} logs: {e}"

    if service in ("postgres", "qwen", "swap"):
        try:
            r = subprocess.run(
                ["journalctl", "--user", "-u", f"container-devforge-{service}", "-n", str(lines), "--no-pager"],
                capture_output=True, text=True, timeout=10
            )
            out = r.stdout.strip() if r.returncode == 0 else r.stderr.strip()
            return f"journalctl --user -u container-devforge-{service} ({lines} lines):\n{out[:2000]}"
        except Exception as e:
            return f"Error reading container {service} logs: {e}"

    # Fallback: general journalctl
    try:
        r = subprocess.run(
            ["journalctl", "-n", str(lines), "--no-pager"],
            capture_output=True, text=True, timeout=10
        )
        out = r.stdout.strip() if r.returncode == 0 else r.stderr.strip()
        return f"journalctl ({lines} lines):\n{out[:2000]}"
    except Exception as e:
        return f"Error reading logs: {e}"


def tool_run_orchestrator(task: str) -> str:
    """Send task to orchestrator."""
    try:
        r = subprocess.run(
            [sys.executable, f"{OBS_SCRIPTS}/orchestrator.py", "--input", task, "--quiet"],
            capture_output=True, text=True, timeout=600
        )
        out = r.stdout.strip() if r.returncode == 0 else r.stderr.strip()
        if not out:
            out = "(no output from orchestrator)"
        return f"Orchestrator result:\n{out[:2000]}"
    except subprocess.TimeoutExpired:
        return "Error: orchestrator timed out after 600s"
    except Exception as e:
        return f"Error running orchestrator: {e}"


def tool_query_db(sql: str) -> str:
    """Run SQL query."""
    from lib.db import psql
    try:
        result = psql(sql)
        if result:
            return f"Query result:\n{result[:2000]}"
        return "(empty result)"
    except Exception as e:
        return f"DB error: {e}"


def tool_check_nightly() -> str:
    """Nightly batch status."""
    parts = []

    # Status file
    sf = "/opt/projects/server/data/nightly_status.yaml"
    if os.path.exists(sf):
        try:
            with open(sf) as f:
                parts.append("Nightly status file:\n" + f.read().strip())
        except Exception as e:
            parts.append(f"Can't read status file: {e}")
    else:
        parts.append("No nightly_status.yaml (nightly may not have run yet)")

    # Last nightly service run
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", "nightly-batch", "-n", "5", "--no-pager"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0 and r.stdout.strip():
            parts.append("\nLast nightly-batch journal lines:\n" + r.stdout.strip()[-1000:])
    except Exception:
        pass

    # Nightly timer
    try:
        r = subprocess.run(
            ["systemctl", "--user", "list-timers", "--no-pager"],
            capture_output=True, text=True, timeout=10
        )
        for line in r.stdout.splitlines():
            if "nightly" in line.lower():
                parts.append(f"\nNightly timer:\n{line}")
    except Exception:
        pass

    return "\n".join(parts) if parts else "No nightly data available."


def tool_analyze_error(context: str) -> str:
    """Deep-dive error analysis."""
    parts = [f"Error analysis for: {context}"]

    # Check recent journalctl for errors
    try:
        r = subprocess.run(
            ["journalctl", "-n", "50", "--no-pager", "-p", "err"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0 and r.stdout.strip():
            parts.append("\nRecent system errors (journalctl -p err):")
            for line in r.stdout.strip().splitlines()[-20:]:
                parts.append(f"  {line}")
    except Exception:
        pass

    # Check container status
    try:
        r = subprocess.run(
            ["podman", "ps", "-a", "--format", "{{.Names}} {{.Status}}"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            parts.append("\nAll containers:")
            for line in r.stdout.splitlines():
                if "unhealthy" in line.lower() or "exited" in line.lower() or "dead" in line.lower():
                    parts.append(f"  !! {line}")
                else:
                    parts.append(f"  {line}")
    except Exception:
        pass

    # Check relevant DB tables for recent failures
    try:
        from lib.db import psql
        r = psql("SELECT count(*) FROM activity_log WHERE status='error' AND created_at > now() - interval '24 hours'")
        if r:
            parts.append(f"\nActivity errors (24h): {r}")
    except Exception:
        pass

    return "\n".join(parts) if parts else "No error data found."


TOOL_MAP = {
    "check_status": tool_check_status,
    "check_logs": tool_check_logs,
    "run_orchestrator": tool_run_orchestrator,
    "query_db": tool_query_db,
    "check_nightly": tool_check_nightly,
    "analyze_error": tool_analyze_error,
}


# ── LLM Interaction ───────────────────────────────────────────────────────


def call_llm(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    timeout: int = 120,
) -> Dict[str, Any]:
    """Call the Qwen 30B model with optional tool definitions.

    Returns the full message dict (with tool_calls or content).
    """
    body: Dict[str, Any] = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "max_tokens": 4096,
    }
    if tools:
        body["tools"] = tools

    url = f"{API_URL}/v1/chat/completions"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return {"role": "assistant", "content": f"[Error] LLM call failed: {e}"}
    except json.JSONDecodeError as e:
        return {"role": "assistant", "content": f"[Error] Invalid JSON response: {e}"}

    choices = result.get("choices", [])
    if not choices:
        return {"role": "assistant", "content": "[Error] No response from model"}

    return choices[0]["message"]


# ── Main Loop ─────────────────────────────────────────────────────────────


def run_interactive(debug: bool = False) -> None:
    """Interactive REPL with tool calling support."""
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    print(f"\n  DevForge Observer (model: {MODEL})")
    print(f"  Commands: /help  /tools  /clear  /bye")
    print()

    while True:
        try:
            user_input = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue

        # Built-in commands
        if user_input == "/bye":
            break
        if user_input == "/clear":
            messages = [messages[0]]
            print("(history cleared)")
            continue
        if user_input == "/help":
            print("Commands:")
            print("  /help      Show this help")
            print("  /tools     List available tools")
            print("  /clear     Reset conversation")
            print("  /bye       Exit")
            print("\nOr just type your question naturally.")
            continue
        if user_input == "/tools":
            print("Available tools:")
            for t in TOOL_DEFINITIONS:
                fn = t["function"]
                print(f"  {fn['name']}: {fn['description']}")
            continue

        # Add user message
        messages.append({"role": "user", "content": user_input})

        # Tool calling loop (max 5 rounds to prevent infinite loops)
        tool_rounds = 0
        while tool_rounds < 5:
            response = call_llm(messages, tools=TOOL_DEFINITIONS)
            messages.append(response)

            if "tool_calls" not in response:
                break

            tool_rounds += 1
            for tc in response["tool_calls"]:
                fn_name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                if debug:
                    print(f"  [tool] {fn_name}({args})")

                handler = TOOL_MAP.get(fn_name)
                if handler:
                    try:
                        result = handler(**args)
                    except Exception as e:
                        result = f"[Error] Tool {fn_name} failed: {e}"
                else:
                    result = f"[Error] Unknown tool: {fn_name}"

                if debug:
                    print(f"  [result] {result[:200]}...")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

        # Print final response
        final = response.get("content", "")
        if final:
            print(final)
        print()


def run_one_shot(prompt: str, debug: bool = False) -> None:
    """Single prompt execution."""
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    tool_rounds = 0
    while tool_rounds < 5:
        response = call_llm(messages, tools=TOOL_DEFINITIONS)
        messages.append(response)

        if "tool_calls" not in response:
            break

        tool_rounds += 1
        for tc in response["tool_calls"]:
            fn_name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}

            if debug:
                print(f"  [tool] {fn_name}({args})", file=sys.stderr)

            handler = TOOL_MAP.get(fn_name)
            if handler:
                try:
                    result = handler(**args)
                except Exception as e:
                    result = f"[Error] Tool {fn_name} failed: {e}"
            else:
                result = f"[Error] Unknown tool: {fn_name}"

            if debug:
                print(f"  [result] {result[:200]}...", file=sys.stderr)

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

    final = response.get("content", "")
    print(final)


def main() -> None:
    ap = argparse.ArgumentParser(description="DevForge Observer Agent")
    ap.add_argument("prompt", nargs="?", help="One-shot prompt (omit for interactive REPL)")
    ap.add_argument("--debug", "-d", action="store_true", help="Verbose tool traces")
    args = ap.parse_args()

    if args.prompt:
        run_one_shot(args.prompt, debug=args.debug)
    else:
        run_interactive(debug=args.debug)


if __name__ == "__main__":
    main()
