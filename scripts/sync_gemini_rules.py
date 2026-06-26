#!/usr/bin/env python3
# Status: experimental
# Path: none — one-off sync, also called by gen_architecture.py
"""Sync shared rules into OpenCode AGENTS.md.

Reads infrastructure.md + llm-common-rule.md + llm-agent-rule.md
and merges them with a header into AGENTS.md.

Usage:
    python3 /opt/projects/server/scripts/sync_gemini_rules.py
"""

import os
import tempfile
from pathlib import Path
from typing import Optional

AGENTS_SRCS = [
    Path("/home/opc/infrastructure.md"),
    Path("/home/opc/llm-common-rule.md"),
    Path("/home/opc/llm-agent-rule.md"),
]
AGENTS_OUTPUT = Path("/home/opc/AGENTS.md")

DEFAULT_AGENTS_HEADER = (
    "# OpenCode Instructions (AGENTS.md)\n\n"
    "> 이 파일은 `sync_gemini_rules.py`에 의해 자동 생성됩니다. 직접 수정하지 마세요.\n"
    "> OpenCode 전용 설정은 `~/.config/opencode/opencode.json`에서 관리하세요.\n\n"
    "---\n\n"
)


def _atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent, encoding="utf-8") as handle:
        handle.write(text)
        temp_name = handle.name
    os.replace(temp_name, path)


def sync_agents_md() -> bool:
    """Merge shared rule files into AGENTS.md. Returns True if written."""
    parts = [DEFAULT_AGENTS_HEADER]
    for src in AGENTS_SRCS:
        p = Path(src)
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))
        else:
            parts.append(f"<!-- {src} not found -->\n")

    new_text = "".join(parts)

    target = AGENTS_OUTPUT
    if target.exists() and target.read_text(encoding="utf-8") == new_text:
        return False

    _atomic_write_text(target, new_text)
    return True


if __name__ == "__main__":
    changed = sync_agents_md()
    if changed:
        print("[sync_agents] AGENTS.md 업데이트 완료")
    else:
        print("[sync_agents] 변경 없음")
