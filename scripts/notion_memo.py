#!/usr/bin/env python3.11
# Status: production
# Path: bash alias: notion-memo, nnmm
"""Notion 메모 전송 CLI — stdin, 파일, 또는 인자로 받은 텍스트를 Notion에 기록.

Usage:
  echo "hello" | notion-memo
  notion-memo "직접 입력한 메모"
  notion-memo --file memo.md
  notion-memo --title "중요 메모" "메모 내용"
  cat report.md | nnmm
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, "/opt/projects/server/scripts")
from lib.notion_client import append_memo  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Send a memo to Notion Devforge-memo page")
    parser.add_argument("text", nargs="*", help="Memo text (direct input)")
    parser.add_argument("--file", "-f", type=Path, help="Read memo from file")
    parser.add_argument("--title", "-t", default="", help="Memo title prefix")
    args = parser.parse_args()

    # Read content: file > stdin > args
    content = ""
    title = args.title or ""

    if args.file:
        if not args.file.exists():
            print(f"File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        content = args.file.read_text(encoding="utf-8")
        if not title:
            title = args.file.name
    elif not sys.stdin.isatty():
        content = sys.stdin.read()
        if not title:
            title = "stdin memo"
    elif args.text:
        content = " ".join(args.text)
        if not title:
            title = content[:60]

    if not content.strip():
        print("No content to send. Pipe text, pass as argument, or use --file.", file=sys.stderr)
        sys.exit(1)

    url = append_memo(content.strip(), title=title)
    print(url)


if __name__ == "__main__":
    main()
