"""
Sync shared rules into Gemini CLI instruction file.

Pattern adapted from common-lib core/copilot_rules.py.
Reads common-main.md + common-rule.md, prepends Gemini header, writes GEMINI.md.

Usage:
    python3 /opt/projects/server/scripts/sync_gemini_rules.py
"""

import os
import tempfile
from pathlib import Path
from typing import Optional, List

RULE_SRCS = [
    Path("/home/opc/common-main.md"),
    Path("/home/opc/common-rule.md"),
]
GEMINI_INSTRUCTIONS = Path("/home/opc/GEMINI.md")
GEMINI_HEADER = Path("/home/opc/.gemini/header.md")

DEFAULT_HEADER = (
    "# Gemini CLI Instructions\n\n"
    "> 이 파일은 `sync_gemini_rules.py`에 의해 자동 생성됩니다. 직접 수정하지 마세요.\n"
    "> Gemini 전용 설정은 `~/.gemini/header.md`에서 관리하세요.\n\n"
    "---\n\n"
)


def _atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent, encoding="utf-8") as handle:
        handle.write(text)
        temp_name = handle.name
    os.replace(temp_name, path)


def sync_gemini_instructions(
    rule_srcs: Optional[List[Path]] = None,
    gemini_instructions: Optional[Path] = None,
    header_src: Optional[Path] = None,
) -> bool:
    """Sync shared rule files into GEMINI.md. Returns True if written."""
    rule_srcs = rule_srcs or RULE_SRCS
    gemini_instructions = gemini_instructions or GEMINI_INSTRUCTIONS
    header_src = header_src or GEMINI_HEADER

    # Read header (Gemini-specific, excluded from sync)
    if header_src.exists():
        header = header_src.read_text(encoding="utf-8")
    else:
        header = DEFAULT_HEADER

    # Concatenate all rule sources
    parts = [header]
    for src in rule_srcs:
        p = Path(src)
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))
        else:
            parts.append(f"<!-- {src} not found -->\n")

    new_text = "".join(parts)

    # Skip write if unchanged
    target = Path(gemini_instructions)
    if target.exists() and target.read_text(encoding="utf-8") == new_text:
        return False

    _atomic_write_text(target, new_text)
    return True


if __name__ == "__main__":
    changed = sync_gemini_instructions()
    if changed:
        print("[sync_gemini] GEMINI.md 업데이트 완료")
    else:
        print("[sync_gemini] 변경 없음")
