#!/usr/bin/env python3.11
# Status: production
# Path: imported by — cli.py, (future) lib consumers
"""Research facade — one entry point over web/exa/context7/fetch providers.

P5a: no cache, no rerank. candidate_k/top_k are separated so a rerank stage can
be added later without changing callers.
"""

from __future__ import annotations

from lib.research import web, exa, context7, fetch  # noqa: F401  (re-export modules)


def _norm_web(items: list[dict]) -> list[dict]:
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""),
         "snippet": r.get("description", ""), "source": "web"}
        for r in items
    ]


def _norm_exa(items: list[dict]) -> list[dict]:
    out = []
    for r in items:
        if r.get("highlights"):
            sn = " | ".join(r["highlights"][:2])
        elif r.get("text"):
            sn = r["text"][:300]
        elif r.get("summary"):
            sn = r["summary"][:300]
        else:
            sn = ""
        out.append({"title": r.get("title", ""), "url": r.get("url", ""),
                    "snippet": sn, "source": "exa", "published": r.get("publishedDate", "")})
    return out


def research(query: str, mode: str = "auto", candidate_k: int = 30, top_k: int = 5,
             rerank: bool = False, use_cache: bool = False) -> dict:
    """Search-facing facade. mode: auto|web|exa. Returns normalized, cited results.

    rerank/use_cache are reserved for P5b; P5a ignores them (no cache, no rerank).
    """
    mode = (mode or "auto").lower()
    if mode == "auto":
        mode = "web"
    if mode == "web":
        items = _norm_web(web.web_search_structured(query, max_results=candidate_k))
        provider = "web"
    elif mode == "exa":
        items = _norm_exa(exa.exa_search(query, num_results=candidate_k))
        provider = "exa"
    else:
        raise ValueError(f"unsupported mode: {mode} (use docs() for context7)")

    meta = {"provider": provider, "candidate_k": candidate_k, "top_k": top_k,
            "reranked": False, "cache_hit": False, "count": len(items)}
    return {"query": query, "mode": mode, "results": items[:top_k], "meta": meta}


def docs(library_name: str, question: str, top_k: int = 5) -> dict:
    """Context7 docs lookup → {library, results:[{title,url,snippet,source}], meta}."""
    matches = context7.resolve_library_id(question, library_name)
    if not matches:
        return {"library": library_name, "results": [], "meta": {"provider": "context7", "count": 0}}
    lib_id = matches[0].get("id", "")
    data = context7.query_docs(lib_id, question)
    text = context7.format_docs_text(lib_id, data)
    return {"library": library_name, "libraryId": lib_id,
            "results": [{"title": f"{library_name} docs", "url": lib_id or "",
                         "snippet": text[:2000], "source": "context7"}],
            "meta": {"provider": "context7", "count": 1, "candidates": len(matches)}}


def fetch_page(url: str, max_chars: int = 50000) -> dict:
    return fetch.fetch_url(url, max_chars=max_chars)


__all__ = ["research", "docs", "fetch_page", "web", "exa", "context7", "fetch"]
