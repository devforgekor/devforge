#!/usr/bin/env python3
# Status: production
# Path: MCP stdio (opencode.json: search) — thin wrapper over lib/research/*
"""Unified search MCP — Brave/Tavily/you.com + Exa + Context7 in one server.

Domain consolidation (MCP best practice: one domain → one server, few narrow tools).
Core logic and key rotation live in lib/research/{web,exa,context7}.py (unchanged);
this server only exposes 3 narrow tools with minimal schemas:

  search(query, mode)      — web (Brave→Tavily→you.com) or semantic (Exa)
  fetch_contents(urls)     — URL body fetch (Exa)
  query_docs(library, q)   — library docs (Context7)

Replaces the former search-proxy / exa-search / context7 servers.
"""

import json
import os
import sys

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from lib.research import context7, exa  # noqa: E402
from lib.research.rerank import rerank_records  # noqa: E402
from lib.research.web import get_proxy  # noqa: E402

TOOLS = [
    {
        "name": "search",
        "description": (
            "Search the web. mode=auto|web (Brave/Tavily/you.com, key-rotated) "
            "or semantic (Exa). Use 'semantic' for research papers, deep/exhaustive "
            "or when keyword search is weak. Returns titles/URLs/snippets."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "mode": {
                    "type": "string",
                    "enum": ["auto", "web", "semantic"],
                    "default": "auto",
                    "description": "auto=web first then semantic; web=keyword; semantic=Exa",
                },
                "max_results": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_contents",
        "description": "Fetch full text of URLs (max 10) via Exa. Use after search to read pages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
            },
            "required": ["urls"],
        },
    },
    {
        "name": "query_docs",
        "description": (
            "Up-to-date library/website docs (Context7). 'library' may be a name (auto-resolved) "
            "or an ID: /owner/repo, /owner/repo/version, /websites/<site>, /packages/<name>. "
            "Keep each query to ONE topic; for multi-concept questions call this once per concept "
            "(in parallel when independent). Prefer official sources."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "library": {"type": "string", "description": "Library name (e.g. FastAPI) or ID (/org/project[/version], /websites/<site>)"},
                "query": {"type": "string", "description": "Question about ONE topic in the library"},
            },
            "required": ["library", "query"],
        },
    },
]


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _text(content: str) -> dict:
    return {"content": [{"type": "text", "text": content}]}


def _do_search(query: str, mode: str, max_results: int) -> str:
    query = (query or "").strip()
    if not query:
        raise ValueError("query is required")
    if mode == "semantic":
        records = _exa_records(query, max_results)
        records = rerank_records(query, records, text_key="summary") if records else records
        return _format_records(query, records, "semantic") if records else exa.exa_search_text(query, num_results=max_results)
    if mode == "web":
        proxy = get_proxy()
        if not proxy.available:
            raise RuntimeError("no web search keys configured")
        records = proxy.search_structured(query, max_results)
        if not records:
            return "(no results)"
        records = rerank_records(query, records, text_key="description")
        return _format_records(query, records, "web")
    # auto: web(키 있으면) → 부족/실패 시 semantic
    proxy = get_proxy()
    if proxy.available:
        try:
            records = proxy.search_structured(query, max_results)
            if records:
                records = rerank_records(query, records, text_key="description")
                return _format_records(query, records, "web")
        except Exception:  # noqa: BLE001 — web 실패 시 semantic 폴백
            pass
    records = _exa_records(query, max_results)
    if records:
        records = rerank_records(query, records, text_key="summary")
        return _format_records(query, records, "semantic")
    return exa.exa_search_text(query, num_results=max_results)


def _exa_records(query: str, max_results: int) -> list:
    """Exa 결과를 구조화 dict로(리랭킹 대상). 실패 시 빈 리스트."""
    try:
        return exa.exa_search(query, num_results=max_results, highlights=True)
    except Exception:  # noqa: BLE001
        return []


def _format_records(query: str, records: list, mode: str) -> str:
    """구조화 결과를 텍스트로(제목/URL/스니펫). 리랭킹된 순서를 유지한다."""
    lines = [f"Search results for: {query} (mode={mode}, reranked)"]
    for i, r in enumerate(records, 1):
        title = r.get("title") or "Untitled"
        url = r.get("url") or ""
        desc = r.get("description") or r.get("summary") or ""
        if isinstance(desc, dict):
            desc = desc.get("text") or ""
        if not desc and r.get("highlights"):
            desc = " | ".join(r["highlights"][:2])
        lines.append(f"{i}. {title}\n   {url}\n   {str(desc)[:300]}")
    return "\n".join(lines)


def _do_fetch(urls: list) -> str:
    if not urls:
        raise ValueError("urls is required")
    return exa.exa_contents_text(urls[:10])


def _do_docs(library: str, query: str) -> str:
    library = (library or "").strip()
    if not library:
        raise ValueError("library is required")
    if library.startswith("/"):
        return context7.query_docs_text(library, query)
    resolved = context7.resolve_library_id_text(query, library)
    ids = context7.resolve_library_id(query, library)
    if not ids:
        return f"(no library found for {library})\n{resolved}"
    return context7.query_docs_text(ids[0]["id"], query)


def _handle_tool(name: str, args: dict) -> dict:
    if name == "search":
        return _text(_do_search(args.get("query", ""), args.get("mode", "auto"), int(args.get("max_results", 5))))
    if name == "fetch_contents":
        return _text(_do_fetch(args.get("urls", [])))
    if name == "query_docs":
        return _text(_do_docs(args.get("library", ""), args.get("query", "")))
    raise KeyError(name)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        req_id = request.get("id")
        method = request.get("method", "")

        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "search", "version": "2.0.0"}}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = request.get("params", {})
            try:
                result = _handle_tool(params.get("name", ""), params.get("arguments", {}) or {})
                _send({"jsonrpc": "2.0", "id": req_id, "result": result})
            except KeyError as e:
                _send({"jsonrpc": "2.0", "id": req_id,
                       "error": {"code": -32601, "message": f"Unknown tool: {e}"}})
            except Exception as e:  # noqa: BLE001 — 툴 오류는 모델이 복구할 수 있게 반환
                _send({"jsonrpc": "2.0", "id": req_id,
                       "error": {"code": -32000, "message": f"{type(e).__name__}: {e}"}})
        elif method == "notifications/initialized":
            pass
        else:
            _send({"jsonrpc": "2.0", "id": req_id,
                   "error": {"code": -32601, "message": f"Unknown method: {method}"}})


if __name__ == "__main__":
    main()
