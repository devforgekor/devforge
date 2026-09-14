#!/usr/bin/env python3.11
# Status: production
# Path: alias gemini (via ~/.bashrc), standalone CLI
"""gemini — Agentic Gemini CLI with function calling + tool execution.

Direct Gemini API calls via local proxy (127.0.0.1:4430).
Core logic in gemini_core.py. This file: banner, REPL, CLI entry point.
"""

import argparse, json, subprocess, sys, time

from gemini_core import (
    TOOLS, DEFAULT_MODEL, call_gemini, execute_tool, load_keys, pick_key,
)


def _show_banner(keys):
    try:
        logo = subprocess.run(
            ["oh-my-logo", "GEMINI", "sunset", "--filled"],
            capture_output=True, text=True, timeout=10
        ).stdout
        logo = logo.rstrip() + "\n"
    except Exception:
        logo = ""
    n_keys = len(keys)
    sep = "─" * 52
    print(f"\n{logo}")
    print(f"  {sep}")
    print(f"  \033[33mDevForge Server CLI\033[0m  \033[90m| {n_keys} keys | {DEFAULT_MODEL}\033[0m")
    print(f"  \033[90mTools: web_search, fetch_url | MCP: 9 servers\033[0m")
    print(f"  {sep}\n")


def _repl(keys):
    pick_key(keys)
    try:
        import readline
        readline.parse_and_bind('set input-meta on')
        readline.parse_and_bind('set convert-meta off')
        readline.parse_and_bind('set output-meta on')
    except ImportError:
        pass
    contents = []
    tools_arg = TOOLS
    print("  \033[90mType /help for commands, /quit to exit\033[0m\n")

    while True:
        try:
            user_input = input("\033[36m>>> \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input == "/quit":
            break
        if user_input == "/help":
            print("  \033[90m/quit  — exit\033[0m")
            print("  \033[90m/clear — clear conversation history\033[0m")
            print("  \033[90m/notools  — disable function calling\033[0m")
            print("  \033[90m/tools    — enable function calling\033[0m")
            continue
        if user_input == "/clear":
            contents.clear()
            print("  \033[90mConversation cleared.\033[0m")
            continue
        if user_input == "/notools":
            tools_arg = None
            print("  \033[90mFunction calling disabled.\033[0m")
            continue
        if user_input == "/tools":
            tools_arg = TOOLS
            print("  \033[90mFunction calling enabled.\033[0m")
            continue

        contents.append({"role": "user", "parts": [{"text": user_input}]})

        for turn in range(8):
            result = call_gemini(contents, tools=tools_arg)
            if result is None:
                print("\033[31m[API: no response (timeout)]\033[0m", file=sys.stderr)
                break
            if "error" in result:
                print(f"\033[31m[API: {result['error'][:200]}]\033[0m", file=sys.stderr)
                break
            candidate = result.get("candidates", [{}])[0]
            content = candidate.get("content", {})

            if not content.get("parts"):
                reason = candidate.get("finishReason", "unknown")
                print(f"\033[31m[Blocked: {reason}]\033[0m", file=sys.stderr)
                break

            part = content["parts"][0]

            if "functionCall" in part:
                fc = part["functionCall"]
                name = fc.get("name")
                fc_args = fc.get("args", {})
                tool_result = execute_tool(name, fc_args)
                if tool_result is None:
                    tool_result = "(no output)"
                contents.append({"role": "model", "parts": [{"functionCall": fc}]})
                contents.append({
                    "role": "function",
                    "parts": [{"functionResponse": {"name": name, "response": {"result": tool_result}}}]
                })
                continue

            text = part.get("text", "")
            if text:
                print()
                print(text)
                print()
            break


def main():
    keys = load_keys()
    if not keys:
        print("[gemini] error: no API keys found in secrets.env", file=sys.stderr)
        sys.exit(1)

    _show_banner(keys)

    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--prompt", help="One-shot prompt")
    parser.add_argument("-f", "--file", help="Read prompt from file")
    parser.add_argument("-n", "--no-tools", action="store_true", help="Disable function calling (plain Q&A)")
    parser.add_argument("args", nargs="*", help="Prompt text")
    args = parser.parse_args()

    prompt = None
    if args.file:
        with open(args.file) as f:
            prompt = f.read().strip()
    elif args.prompt:
        prompt = args.prompt
    elif args.args:
        prompt = " ".join(args.args)

    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()

    if not prompt:
        if sys.stdin.isatty():
            _repl(keys)
            return
        print("Usage: gemini <prompt> | gemini -p <prompt> | gemini -f <file>", file=sys.stderr)
        sys.exit(1)

    pick_key(keys)
    contents = [{"role": "user", "parts": [{"text": prompt}]}]
    tools_arg = None if args.no_tools else TOOLS

    for turn in range(8):
        result = call_gemini(contents, tools=tools_arg)
        if result is None:
            print("[gemini] API: no response (timeout)", file=sys.stderr)
            sys.exit(1)
        if "error" in result:
            print(f"[gemini] API: {result['error'][:200]}", file=sys.stderr)
            sys.exit(1)
        candidate = result.get("candidates", [{}])[0]
        content = candidate.get("content", {})

        if not content.get("parts"):
            reason = candidate.get("finishReason", "unknown")
            print(f"[gemini] Blocked (finishReason={reason})", file=sys.stderr)
            block_reason = candidate.get("safetyRatings", [])
            if block_reason:
                print(json.dumps(block_reason, indent=2), file=sys.stderr)
            sys.exit(1)

        part = content["parts"][0]

        if "functionCall" in part:
            fc = part["functionCall"]
            name = fc.get("name")
            fc_args = fc.get("args", {})
            tool_result = execute_tool(name, fc_args)
            if tool_result is None:
                tool_result = "(no output)"
            contents.append({"role": "model", "parts": [{"functionCall": fc}]})
            contents.append({
                "role": "function",
                "parts": [{"functionResponse": {"name": name, "response": {"result": tool_result}}}]
            })
            continue

        text = part.get("text", "")
        if text:
            print(text)
        else:
            print("[gemini] Empty response", file=sys.stderr)
            print(json.dumps(content, indent=2)[:500], file=sys.stderr)
        return

    print("\n[gemini] Max turns reached. Response may be incomplete.", file=sys.stderr)


if __name__ == "__main__":
    main()
