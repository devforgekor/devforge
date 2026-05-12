#!/usr/bin/env python3
"""Lightweight orchestrator: routes tasks to aider (code) or qwen (chat)."""
from typing import List, Optional, Tuple
import subprocess as sp
import sys
import os
import json
import urllib.request
import time

QWEN_URL = "http://127.0.0.1:8080/v1/chat/completions"
QWEN_MODEL = "qwen2.5-coder-7b-instruct-q8_0.gguf"
AIDER = "/opt/projects/aider-env/bin/aider"
AIDER_ENV = {
    **os.environ,
    "OPENAI_API_BASE": "http://127.0.0.1:8080/v1",
    "OPENAI_API_KEY": "unused",
}

def ask_qwen(prompt: str, system: str = "", max_tokens: int = 256) -> Tuple[float, str, dict]:
    """Direct qwen API call - fastest path."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body = json.dumps({
        "model": QWEN_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }).encode()

    start = time.time()
    req = urllib.request.Request(QWEN_URL, data=body)
    req.add_header("Content-Type", "application/json")
    resp = urllib.request.urlopen(req, timeout=120)
    data = json.loads(resp.read())
    elapsed = time.time() - start

    content = data["choices"][0]["message"]["content"]
    tokens = data.get("usage", {})
    return elapsed, content, tokens

def run_aider(message: str, files: Optional[List[str]] = None, cwd: str = ".") -> Tuple[float, str]:
    """Run aider for code editing tasks."""
    args = [
        AIDER,
        "--model", f"openai/{QWEN_MODEL}",
        "--no-check-update", "--yes", "--no-gitignore",
        "--map-tokens", "0",
        "--no-auto-commits",
        "--message", message,
    ]
    if files:
        args.extend(files)

    start = time.time()
    result = sp.run(args, capture_output=True, text=True, timeout=300, cwd=cwd, env=AIDER_ENV)
    elapsed = time.time() - start
    return elapsed, result.stdout

def classify(message: str) -> str:
    """Quick keyword-based classification. Avoids LLM call overhead."""
    msg = message.lower()
    code_keywords = ["add", "fix", "implement", "create", "modify", "change", "update",
                     "함수", "추가", "수정", "구현", "만들어", "고쳐", "변경",
                     "function", "class", "refactor", "rewrite", "코드"]
    return "code" if any(kw in msg for kw in code_keywords) else "chat"

def main():
    if len(sys.argv) < 2:
        print("Usage: orch <message> [--files f1.py f2.py] [--cwd /path]")
        sys.exit(1)

    message = sys.argv[1]
    files = []
    cwd = "."

    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--files" and i+1 < len(args):
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                files.append(args[i])
                i += 1
        elif args[i] == "--cwd" and i+1 < len(args):
            cwd = args[i+1]
            i += 2
        else:
            i += 1

    task_type = classify(message)
    print(f"[orch] routing to: {task_type}")

    if task_type == "code" or files:
        elapsed, out = run_aider(message, files, cwd)
        print(f"[aider] {elapsed:.0f}s")
        # Show relevant output lines
        for line in out.split("\n"):
            if any(kw in line for kw in ["Tokens:", "Applied", "diff", "error", "Error"]):
                print(f"  {line.strip()}")
    else:
        elapsed, content, tokens = ask_qwen(message)
        print(f"[qwen] {elapsed:.1f}s | prompt={tokens.get('prompt_tokens',0)} completion={tokens.get('completion_tokens',0)}")
        print(content)

if __name__ == "__main__":
    main()
