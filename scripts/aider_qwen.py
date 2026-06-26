#!/usr/bin/env python3
# Status: experimental
# Path: bash — interactive CLI aid for aider Qwen integration
"""Aider wrapper — OpenRouter key (QWEN_480B_API_KEY) + rule injection."""
import os, sys
from pathlib import Path

secrets = Path.home() / ".config/devforge/secrets.env"
key = None
if secrets.exists():
    for line in secrets.read_text().splitlines():
        if line.startswith("QWEN_480B_API_KEY="):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

if not key:
    key = os.environ.get("QWEN_480B_API_KEY") or os.environ.get("OPENROUTER_API_KEY")

if not key:
    print("[aider] No OpenRouter API key found (QWEN_480B_API_KEY)", file=sys.stderr)
    sys.exit(1)

os.environ["OPENROUTER_API_KEY"] = key

# Inject rule files as read-only context
rule_files = [
    "/home/opc/infrastructure.md",
    "/home/opc/llm-common-rule.md",
    "/home/opc/llm-agent-rule.md",
]
read_flags = []
for rf in rule_files:
    if Path(rf).exists():
        read_flags.extend(["--read", rf])

args = sys.argv[1:] or ["--model", "openrouter/qwen/qwen3-coder:free"]
# Only add --model if user didn't specify one
has_model = any(a.startswith("--model") for a in args)
if not has_model:
    args = ["--model", "openrouter/qwen/qwen3-coder:free"] + args

print(f"[aider] Model: {args[args.index('--model')+1]}", flush=True)
os.execvp("aider", ["aider"] + read_flags + args)
