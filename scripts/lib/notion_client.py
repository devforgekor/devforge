#!/usr/bin/env python3.11
# Status: production
# Path: imported by — droplr_upload.py, blob_uploader.py, cli.py
"""Notion 메모 클라이언트 — 서버 문서/분석 결과를 Notion 페이지로 공유.

Devforge-memo 페이지에 블록을 추가하거나, 새 페이지를 생성한다.

Usage:
  from lib.notion_client import append_memo, create_page_from_markdown

  # 메모 추가
  url = append_memo("# 제목\\n\\n본문 내용...")
  print(f"Notion URL: {url}")

  # Blob 업로드 + Notion 메모
  from lib.blob_uploader import upload_raw
  sas_url = upload_raw(content, "pipeline", "session", "file.md")
  memo_url = append_memo(f"## 분석 결과\\n[SAS URL]({sas_url})")
"""

import os
import re
from datetime import datetime, timezone
from typing import Optional

MEMO_PAGE_ID = "3d38c273-6884-8084-8d13-ccee7986a164"

_notion = None


def _get_client():
    global _notion
    if _notion:
        return _notion
    from notion_client import Client

    token = os.environ.get("NOTION_TOKEN_KEY", "")
    if not token:
        raise RuntimeError("NOTION_TOKEN_KEY not found in env (Azure KV)")
    _notion = Client(auth=token)
    return _notion


_VALID_LANGUAGES = frozenset(
    {
        "abap",
        "abc",
        "agda",
        "arduino",
        "ascii art",
        "assembly",
        "bash",
        "basic",
        "bnf",
        "c",
        "c#",
        "c++",
        "clojure",
        "coffeescript",
        "coq",
        "css",
        "dart",
        "dhall",
        "diff",
        "docker",
        "ebnf",
        "elixir",
        "elm",
        "erlang",
        "f#",
        "flow",
        "fortran",
        "gherkin",
        "glsl",
        "go",
        "graphql",
        "groovy",
        "haskell",
        "hcl",
        "html",
        "idris",
        "java",
        "javascript",
        "json",
        "julia",
        "kotlin",
        "latex",
        "less",
        "lisp",
        "livescript",
        "llvm ir",
        "lua",
        "makefile",
        "markdown",
        "markup",
        "matlab",
        "mathematica",
        "mermaid",
        "nix",
        "notion formula",
        "objective-c",
        "ocaml",
        "pascal",
        "perl",
        "php",
        "plain text",
        "powershell",
        "prolog",
        "protobuf",
        "purescript",
        "python",
        "r",
        "racket",
        "reason",
        "ruby",
        "rust",
        "sass",
        "scala",
        "scheme",
        "scss",
        "shell",
        "smalltalk",
        "solidity",
        "sql",
        "swift",
        "toml",
        "typescript",
        "vb.net",
        "verilog",
        "vhdl",
        "visual basic",
        "webassembly",
        "xml",
        "yaml",
        "java/c/c++/c#",
    }
)

_LANG_ALIASES = {
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
    "rb": "ruby",
    "rs": "rust",
    "sh": "shell",
    "zsh": "shell",
    "bash": "shell",
    "http": "plain text",
    "text": "plain text",
    "txt": "plain text",
    "": "plain text",
    "plain": "plain text",
    "console": "shell",
    "terminal": "shell",
    "env": "plain text",
    "dos": "plain text",
    "ps1": "powershell",
    "ps": "powershell",
    "cmd": "plain text",
    "gradle": "groovy",
    "make": "makefile",
    "dockerfile": "docker",
    "yaml": "yaml",
    "yml": "yaml",
    "json5": "json",
    "cjs": "javascript",
    "mjs": "javascript",
    "jsx": "javascript",
    "tsx": "typescript",
    "vue": "html",
    "kt": "kotlin",
    "kts": "kotlin",
    "swift": "swift",
    "m": "objective-c",
    "mm": "objective-c",
    "cmake": "plain text",
    "patch": "diff",
    "nginx": "plain text",
    "apache": "plain text",
    "sqlite": "sql",
    "mysql": "sql",
    "pgsql": "sql",
    "redis": "plain text",
}


def _normalize_language(lang: str) -> str:
    normalized = lang.strip().lower()
    if normalized in _VALID_LANGUAGES:
        return normalized
    if normalized in _LANG_ALIASES:
        return _LANG_ALIASES[normalized]
    return "plain text"


def _markdown_to_notion_blocks(md: str) -> list[dict]:
    """Convert markdown text to Notion block objects."""
    blocks = []
    lines = md.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        # Empty line
        if not line.strip():
            i += 1
            continue

        # Code block (```)
        if line.startswith("```"):
            raw_lang = line[3:].strip() or "plain text"
            lang = _normalize_language(raw_lang)
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            blocks.append(
                {
                    "object": "block",
                    "type": "code",
                    "code": {
                        "rich_text": [{"type": "text", "text": {"content": "\n".join(code_lines)}}],
                        "language": lang if lang != "plain text" else "plain text",
                    },
                }
            )
            continue

        # Heading 1
        if line.startswith("# ") and not line.startswith("## "):
            text = line[2:].strip()
            if text:
                blocks.append(
                    {
                        "object": "block",
                        "type": "heading_1",
                        "heading_1": {
                            "rich_text": [{"type": "text", "text": {"content": text}}],
                        },
                    }
                )
            i += 1
            continue

        # Heading 2
        if line.startswith("## ") and not line.startswith("### "):
            text = line[3:].strip()
            if text:
                blocks.append(
                    {
                        "object": "block",
                        "type": "heading_2",
                        "heading_2": {
                            "rich_text": [{"type": "text", "text": {"content": text}}],
                        },
                    }
                )
            i += 1
            continue

        # Heading 3
        if line.startswith("### "):
            text = line[4:].strip()
            if text:
                blocks.append(
                    {
                        "object": "block",
                        "type": "heading_3",
                        "heading_3": {
                            "rich_text": [{"type": "text", "text": {"content": text}}],
                        },
                    }
                )
            i += 1
            continue

        # Bullet list
        if line.startswith("- ") or line.startswith("* "):
            text = line[2:].strip()
            if text:
                blocks.append(
                    {
                        "object": "block",
                        "type": "bulleted_list_item",
                        "bulleted_list_item": {
                            "rich_text": [{"type": "text", "text": {"content": text}}],
                        },
                    }
                )
            i += 1
            continue

        # Numbered list
        if re.match(r"^\d+\. ", line):
            text = re.sub(r"^\d+\.\s*", "", line).strip()
            if text:
                blocks.append(
                    {
                        "object": "block",
                        "type": "numbered_list_item",
                        "numbered_list_item": {
                            "rich_text": [{"type": "text", "text": {"content": text}}],
                        },
                    }
                )
            i += 1
            continue

        # Quote
        if line.startswith("> "):
            text = line[2:].strip()
            if text:
                blocks.append(
                    {
                        "object": "block",
                        "type": "quote",
                        "quote": {
                            "rich_text": [{"type": "text", "text": {"content": text}}],
                        },
                    }
                )
            i += 1
            continue

        # Divider
        if line.strip() == "---":
            blocks.append(
                {
                    "object": "block",
                    "type": "divider",
                    "divider": {},
                }
            )
            i += 1
            continue

        # Callout ( > **Note:** ... )
        if line.strip().startswith("> **"):
            text = re.sub(r"^>\s*\*\*.*?\*\*\s*", "", line.strip()).strip()
            if text:
                blocks.append(
                    {
                        "object": "block",
                        "type": "callout",
                        "callout": {
                            "rich_text": [{"type": "text", "text": {"content": text}}],
                            "icon": {"emoji": "💡"},
                        },
                    }
                )
            i += 1
            continue

        # Paragraph (default) — handle inline links
        text = line.strip()
        if text:
            rich_text = _parse_inline_markdown(text)
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": rich_text,
                    },
                }
            )
        i += 1

    return blocks


def _parse_inline_markdown(text: str) -> list[dict]:
    """Parse inline markdown (bold, links, code) into Notion rich_text array."""
    rich_text = []
    remaining = text

    # Pattern: [text](url) or **text** or `code`
    while remaining:
        link_match = re.search(r"\[([^\]]+)\]\(([^)]+)\)", remaining)
        bold_match = re.search(r"\*\*(.+?)\*\*", remaining)
        code_match = re.search(r"`([^`]+)`", remaining)

        # Find the earliest match
        matches = []
        if link_match:
            matches.append(("link", link_match.start(), link_match))
        if bold_match:
            matches.append(("bold", bold_match.start(), bold_match))
        if code_match:
            matches.append(("code", code_match.start(), code_match))

        if not matches:
            rich_text.append({"type": "text", "text": {"content": remaining}})
            break

        matches.sort(key=lambda x: x[1])
        typ, start, m = matches[0]

        # Text before the match
        if start > 0:
            rich_text.append({"type": "text", "text": {"content": remaining[:start]}})

        if typ == "link":
            rich_text.append(
                {
                    "type": "text",
                    "text": {"content": m.group(1), "link": {"url": m.group(2)}},
                }
            )
            remaining = remaining[m.end() :]

        elif typ == "bold":
            rich_text.append(
                {
                    "type": "text",
                    "text": {"content": m.group(1)},
                    "annotations": {"bold": True},
                }
            )
            remaining = remaining[m.end() :]

        elif typ == "code":
            rich_text.append(
                {
                    "type": "text",
                    "text": {"content": m.group(1)},
                    "annotations": {"code": True},
                }
            )
            remaining = remaining[m.end() :]

    return rich_text


# ═══════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════


def append_memo(markdown: str, title: Optional[str] = None) -> str:
    """Append markdown content as blocks to the Devforge-memo page.

    Args:
        markdown: Markdown text to add as new blocks.
        title: Optional title prefix (default: timestamp).

    Returns:
        Page URL of the Devforge-memo page.
    """
    notion = _get_client()
    blocks = _markdown_to_notion_blocks(markdown)

    if not blocks:
        return f"https://notion.so/{MEMO_PAGE_ID.replace('-', '')}"

    # Add a divider + title block at the beginning
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prefix = title or f"📝 {now}"

    # Append title as heading_3 and content blocks
    all_blocks = [
        {
            "object": "block",
            "type": "divider",
            "divider": {},
        },
        {
            "object": "block",
            "type": "heading_3",
            "heading_3": {
                "rich_text": [{"type": "text", "text": {"content": f"📝 {title or now}"}}],
            },
        },
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {"type": "text", "text": {"content": f"Generated: {now}", "link": None}}
                ],
            },
        },
    ] + blocks

    # Notion API limits: max 100 blocks per request
    CHUNK_SIZE = 100
    for i in range(0, len(all_blocks), CHUNK_SIZE):
        chunk = all_blocks[i : i + CHUNK_SIZE]
        notion.blocks.children.append(block_id=MEMO_PAGE_ID, children=chunk)

    return f"https://notion.so/{MEMO_PAGE_ID.replace('-', '')}"


def create_page(title: str, markdown: str, parent_page_id: Optional[str] = None) -> str:
    """Create a new Notion page with markdown content.

    Args:
        title: Page title.
        markdown: Content in markdown format.
        parent_page_id: Parent page ID (default: Devforge-memo).

    Returns:
        Page URL of the created page.
    """
    notion = _get_client()
    parent_id = parent_page_id or MEMO_PAGE_ID

    blocks = _markdown_to_notion_blocks(markdown)

    page = notion.pages.create(
        parent={"type": "page_id", "page_id": parent_id},
        properties={
            "title": {
                "title": [{"type": "text", "text": {"content": title}}],
            },
        },
        children=blocks if blocks else None,
    )

    page_id = page["id"].replace("-", "")
    return f"https://notion.so/{page_id}"


def append_memo_with_blob(
    markdown: str,
    blob_url: str,
    title: Optional[str] = None,
    filename: Optional[str] = None,
) -> str:
    """Append a memo to Notion with a reference to a Blob file.

    Args:
        markdown: Description text (markdown).
        blob_url: Azure Blob SAS URL or Droplr short URL.
        title: Optional title prefix.
        filename: Optional filename to display.

    Returns:
        Page URL of the Devforge-memo page.
    """
    link_text = filename or "📎 첨부 파일"
    full_md = f"{markdown}\n\n> [{link_text}]({blob_url})"
    return append_memo(full_md, title=title)


def get_memo_url() -> str:
    """Get the Devforge-memo page URL."""
    return f"https://notion.so/{MEMO_PAGE_ID.replace('-', '')}"


def list_recent_memos(limit: int = 10) -> list[dict]:
    """List recent blocks from the Devforge-memo page.

    Args:
        limit: Number of blocks to retrieve.

    Returns:
        List of block dicts with type and text content.
    """
    notion = _get_client()
    children = notion.blocks.children.list(block_id=MEMO_PAGE_ID, page_size=limit)
    memo_list = []
    for c in children.get("results", []):
        block_type = c.get("type", "?")
        block_data = c.get(block_type, {})
        rich_text = block_data.get("rich_text", [])
        text = "".join(
            t.get("text", {}).get("content", "") for t in rich_text if t.get("type") == "text"
        )
        memo_list.append(
            {
                "id": c["id"],
                "type": block_type,
                "text": text[:200],
            }
        )
    return memo_list
