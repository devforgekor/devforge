#!/usr/bin/env python3
"""Anthropic-compatible reverse proxy for DeepSeek.

Rewrites system-role messages into the top-level system field before forwarding
requests to DeepSeek's Anthropic-compatible endpoint.
"""

import argparse
import hashlib
import http.client
import json
import os
import sys
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit
import time
import socket

# Default context window: DeepSeek V4 Flash supports 1M input tokens
# Override via ANTHROPIC_PROXY_CONTEXT_WINDOW env var for other models
DEEPSEEK_CONTEXT_WINDOW = int(os.environ.get("ANTHROPIC_PROXY_CONTEXT_WINDOW", "1000000"))

DEFAULT_LISTEN = "127.0.0.1:44777"
DEFAULT_UPSTREAM = "https://api.deepseek.com/anthropic"
# Model name mapping: map client model names to supported DeepSeek model names to avoid upstream 400s
# "claude" → deepseek-v4-flash (general coding, $0.14/M input)
# "claude-pro" → deepseek-v4-pro (complex reasoning, $0.435/M input)
MODEL_MAP = {
    "claude": "deepseek-v4-flash",
    "claude-pro": "deepseek-v4-pro",
    "claude-sonnet-4-20250514": "deepseek-v4-flash",  # retired June 15, 2026
    "claude-sonnet-4-6": "deepseek-v4-flash",
    "claude-opus-4-8": "deepseek-v4-pro",
}
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_flatten_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("content"), (str, list, dict)):
            return _flatten_text(value["content"])
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)



_BILLING_HEADER_RE = re.compile(
    r'^x-anthropic-billing-header:\s*.*$',
    re.IGNORECASE | re.MULTILINE,
)


def _strip_billing_header(text: str) -> str:
    """Strip ``x-anthropic-billing-header`` lines from system prompt text.

    Claude Code embeds a line like ``x-anthropic-billing-header: cch=...`` at
    the start of the system prompt.  The ``cch`` field changes on every request
    which defeats Anthropic's server-side prompt caching, so we remove it
    before forwarding.
    """
    return _BILLING_HEADER_RE.sub("", text).strip()


def _strip_system_billing_header(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Strip ``x-anthropic-billing-header`` lines from the top-level ``system`` field.

    Unlike ``_sanitize_messages`` (which only fires when ``system``-role messages
    exist in the ``messages`` array), this function always runs so that the
    ``cch=`` hash — which changes on *every* request — does not break DeepSeek's
    automatic prefix caching.

    Handles both a plain string and a content-block array
    (``[{"type": "text", "text": "…"}]``)  —  the format Claude Code actually sends.
    """
    system = payload.get("system")
    if system is None:
        return payload, False

    changed = False

    # ── Plain string ──────────────────────────────────────────────────
    if isinstance(system, str):
        cleaned = _strip_billing_header(system)
        if cleaned != system:
            payload = dict(payload)
            payload["system"] = cleaned
            print("[anthropic_proxy] stripped billing header from system string", file=sys.stderr)
            return payload, True
        return payload, False

    # ── Content-block array: [{"type": "text", "text": "…"}, …] ────────
    if isinstance(system, list):
        new_system: list = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                cleaned = _strip_billing_header(block["text"])
                if cleaned != block["text"]:
                    block = dict(block)
                    block["text"] = cleaned
                    changed = True
            new_system.append(block)
        if changed:
            payload = dict(payload)
            payload["system"] = new_system
            print("[anthropic_proxy] stripped billing header from system content block", file=sys.stderr)

    return payload, changed


def _flatten_system_blocks(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Convert system content-block array to a flat string for stable prefix caching.

    DeepSeek's automatic prefix caching works on raw bytes. The ``[{"type": "text",
    "text": "..."}, ...]`` JSON array wrapper adds structural bytes that can vary
    between requests (whitespace, block boundaries). Flattening to ``"system": "..."
    eliminates that variability entirely.

    Side effects:
    - Merges adjacent text blocks into one flat string
    - Strips empty blocks
    - Normalizes excessive whitespace
    - Removes the ``type`` → ``text`` key structure.
    """
    system = payload.get("system")
    if system is None or isinstance(system, str):
        return payload, False

    if not isinstance(system, list):
        return payload, False

    parts: list[str] = []
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            text = block["text"]
            normalized = re.sub(r"\n{3,}", "\n\n", text).strip()
            if normalized:
                parts.append(normalized)

    merged = "\n\n".join(parts)
    if not merged:
        return payload, False

    payload = dict(payload)
    payload["system"] = merged
    return payload, True


_CACHE_PADDING = None  # lazy-computed: _get_cache_padding()
_CACHE_PADDING_LOCK = [None]


def _get_cache_padding() -> str:
    """Build a deterministic padding string that extends the cached prefix.

    The padding is appended to the system field so that DeepSeek's automatic
    prefix caching covers more of the early conversation turns.  It is a
    stable, known string that does not affect model behavior.

    The length (in chars) is controlled by ``ANTHROPIC_PROXY_CACHE_PAD_CHARS``
    (default 0, i.e. disabled).  At most 50 000 chars to prevent runaway.
    """
    global _CACHE_PADDING
    if _CACHE_PADDING is not None:
        return _CACHE_PADDING

    _lock = _CACHE_PADDING_LOCK
    if _lock[0] is not None:
        return _lock[0]

    pad_chars = int(os.environ.get("ANTHROPIC_PROXY_CACHE_PAD_CHARS", "0"))
    if pad_chars <= 0:
        _CACHE_PADDING = ""
        return _CACHE_PADDING

    pad_chars = min(pad_chars, 50_000)
    # Use a stable, compact filler that looks like a comment.
    filler = "\n# [pad] " + "x" * 60
    repeats = pad_chars // len(filler) + 1
    result = ("\n[system continuation]" + filler * repeats)[:pad_chars]
    _CACHE_PADDING_LOCK[0] = result
    return result


def _apply_cache_padding(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Append deterministic padding to the system field."""
    system = payload.get("system")
    if not isinstance(system, str):
        return payload, False
    pad = _get_cache_padding()
    if not pad:
        return payload, False
    payload = dict(payload)
    payload["system"] = system + "\n" + pad
    return payload, True


def _json_dumps_system_first(payload: dict, **kwargs: Any) -> str:
    """Serialize payload to JSON with ``system`` as the first key.

    DeepSeek's automatic prefix caching keys on the *raw byte prefix* of the
    JSON body.  With ``sort_keys=True`` (the default), ``system`` ends up
    *after* ``messages`` — the most variable field — which pushes it past the
    cached prefix window for long conversations.  This function forces
    ``system`` to position 1 so it is **always** in the cache hit region.

    The remaining keys are serialised with whatever *kwargs* are passed
    (usually ``sort_keys=True`` plus ``separators=…``).
    """
    if "system" not in payload:
        return json.dumps(payload, **kwargs)
    p = dict(payload)
    sys_val = p.pop("system")
    rest = json.dumps(p, **kwargs)
    sys_json = json.dumps(sys_val, **kwargs)
    return '{"system":' + sys_json + "," + rest[1:]


def _strip_cache_control(obj: Any) -> bool:
    """Recursively remove all cache_control fields. Returns True if anything was removed."""
    if isinstance(obj, dict):
        changed = False
        if "cache_control" in obj:
            del obj["cache_control"]
            changed = True
        for value in obj.values():
            if _strip_cache_control(value):
                changed = True
        return changed
    if isinstance(obj, list):
        changed = False
        for item in obj:
            if _strip_cache_control(item):
                changed = True
        return changed
    return False


def _fix_orphan_tool_results(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Remove tool_result blocks that lack a matching tool_use in the previous assistant message.

    Claude CLI auto-compaction ("Crunched") can strip tool_use blocks from assistant
    messages while leaving tool_result references in user messages, causing a 400 from
    the upstream API. This function runs unconditionally to repair such payloads.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) < 1:
        return payload, False

    def _content_has_tool_use(msg: Any, tid: str) -> bool:
        # None means no previous message exists — all tool_results are orphans
        if msg is None:
            return False
        content = msg.get("content", []) if isinstance(msg, dict) else []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") == tid:
                    return True
        return False

    def _strip_orphans(content: Any, prev_msg: Any) -> Tuple[Any, bool]:
        if not isinstance(content, list):
            return content, False
        new_content = []
        changed = False
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                if tid and not _content_has_tool_use(prev_msg, tid):
                    print(
                        f"[anthropic_proxy] dropping orphan tool_result id={tid} "
                        f"(no matching tool_use in previous assistant message)",
                        file=sys.stderr,
                    )
                    changed = True
                    continue
            new_content.append(block)
        return new_content, changed

    changed = False
    new_messages: List[Dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            # prev_msg is None when user message is the first message (system moved to top-level field)
            prev_msg = messages[i - 1] if i > 0 else None
            new_content, c = _strip_orphans(msg.get("content", []), prev_msg)
            if c:
                changed = True
                msg = dict(msg)
                msg["content"] = new_content
                if not new_content:
                    # entire user turn was only orphan tool_results — drop it
                    print(
                        f"[anthropic_proxy] dropping empty user message at index {i} after orphan tool_result removal",
                        file=sys.stderr,
                    )
                    continue
        new_messages.append(msg)

    if not changed:
        return payload, False

    updated = dict(payload)
    updated["messages"] = new_messages
    return updated, True


def _sanitize_messages(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload, False

    system_parts: List[str] = []
    cleaned_messages: List[Dict[str, Any]] = []

    for message in messages:
        if not isinstance(message, dict):
            cleaned_messages.append(message)
            continue
        if message.get("role") == "system":
            text = _flatten_text(message.get("content"))
            if text:
                system_parts.append(text)
            continue
        cleaned_messages.append(message)

    if not system_parts:
        return payload, False

    updated = dict(payload)
    updated["messages"] = cleaned_messages
    existing_system = _flatten_text(updated.get("system"))
    merged_system = "\n\n".join(part for part in [existing_system, "\n\n".join(system_parts)] if part)
    cleaned_system = _strip_billing_header(merged_system)
    if cleaned_system != merged_system:
        print("[anthropic_proxy] stripped billing header from system prompt", file=sys.stderr)
    if cleaned_system:
        updated["system"] = cleaned_system
    elif "system" in updated:
        updated.pop("system", None)
    return updated, True


# ---------------------------------------------------------------------------
# Module-level helper functions extracted from ProxyHandler._forward
# ---------------------------------------------------------------------------


def _collect_referenced_tool_use_ids(obj: Any) -> set:
    """Recursively collect tool_use ids referenced by tool_result blocks.

    Scans dicts and lists for ``{"type": "tool_result", "tool_use_id": ...}``
    entries and returns the set of all referenced ids.
    """
    ids = set()
    if isinstance(obj, dict):
        if obj.get("type") == "tool_result" and "tool_use_id" in obj:
            ids.add(obj["tool_use_id"])
        for v in obj.values():
            ids |= _collect_referenced_tool_use_ids(v)
    elif isinstance(obj, list):
        for it in obj:
            ids |= _collect_referenced_tool_use_ids(it)
    return ids


def _collect_tool_use_ids_present(obj: Any) -> set:
    """Recursively collect tool_use ids present in tool_use blocks.

    Scans for ``{"type": "tool_use", "id": ...}`` entries and returns
    the set of all ids found.
    """
    ids = set()
    if isinstance(obj, dict):
        if obj.get("type") == "tool_use" and "id" in obj:
            ids.add(obj["id"])
        for v in obj.values():
            ids |= _collect_tool_use_ids_present(v)
    elif isinstance(obj, list):
        for it in obj:
            ids |= _collect_tool_use_ids_present(it)
    return ids


def _cleanup(obj: Any, kept_tool_use_ids: set) -> Any:
    """Remove content referencing tool_use ids not in *kept_tool_use_ids*.

    Any dict whose ``type == "tool_result"`` and whose ``tool_use_id`` is
    not in the kept set is dropped (returned as ``None``).  All other
    structure is preserved recursively.
    """
    if isinstance(obj, dict):
        if obj.get("type") == "tool_result" and "tool_use_id" in obj:
            if obj["tool_use_id"] not in kept_tool_use_ids:
                return None
            return obj
        changed = False
        newd = {}
        for k, v in obj.items():
            cv = _cleanup(v, kept_tool_use_ids)
            if cv is None:
                continue
            newd[k] = cv
            if cv is not v:
                changed = True
        return newd if changed else obj
    if isinstance(obj, list):
        newlist = []
        changed = False
        for it in obj:
            cv = _cleanup(it, kept_tool_use_ids)
            if cv is None:
                changed = True
                continue
            newlist.append(cv)
            if cv is not it:
                changed = True
        return newlist if changed else obj
    return obj


def _prev_has_tool_use(prev_msg: Any, tid: str) -> bool:
    """Return True if *prev_msg* contains a ``tool_use`` block with id *tid*."""
    if prev_msg is None:
        return False
    return tid in _collect_tool_use_ids_present(prev_msg)


def _remove_adjacent_orphans(msgs: list) -> list:
    """Remove ``tool_result`` blocks whose referenced ``tool_use`` is not present
    in the *immediately preceding* message.

    This catches a specific edge-case where a tool_result is left over after
    its corresponding tool_use was truncated away in an earlier *adjacent*
    message.
    """
    if not msgs:
        return msgs

    def _rem(o, _prev, _idx):
        if isinstance(o, dict):
            if o.get("type") == "tool_result" and "tool_use_id" in o:
                if not _prev_has_tool_use(_prev, o["tool_use_id"]):
                    print(
                        f"[anthropic_proxy] dropping orphan tool_result "
                        f"{o['tool_use_id']} at message index {_idx}",
                        file=sys.stderr,
                    )
                    return None
                return o
            newd = {}
            for k, v in o.items():
                cv = _rem(v, _prev, _idx)
                if cv is None:
                    continue
                newd[k] = cv
            return newd
        if isinstance(o, list):
            nl = []
            for it in o:
                cv = _rem(it, _prev, _idx)
                if cv is None:
                    continue
                nl.append(cv)
            return nl
        return o

    out = []
    for i, m in enumerate(msgs):
        prev = msgs[i - 1] if i > 0 else None
        cleaned = _rem(m, prev, i)
        if cleaned is not None:
            out.append(cleaned)
    return out


def _strict_tool_adjacency_fix(msgs: list) -> list:
    """Strictly enforce DeepSeek's tool_use/tool_result adjacency requirement.

    DeepSeek requires every ``tool_result`` block to have a corresponding
    ``tool_use`` block in the *immediately preceding* message, which must be
    an ``assistant`` role message.  This function:

    1. Removes any ``tool_result`` whose ``tool_use`` is not found in the
       immediately preceding assistant message.
    2. Drops messages that become empty after removal.
    3. Pre-fills the first non-system message with ``tool_result`` leading
       with a text placeholder.
    """
    if not msgs:
        return msgs

    changed = False
    out: list = []

    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            out.append(m)
            continue
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue

        prev = msgs[i - 1] if i > 0 else None
        prev_has_tool_use = set()
        if isinstance(prev, dict) and prev.get("role") == "assistant":
            prev_has_tool_use = _collect_tool_use_ids_present(prev)

        new_content = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                if tid and tid not in prev_has_tool_use:
                    # Log the exact scenario for diagnostics
                    _why = "no preceding message" if prev is None else \
                           f"preceding msg role={prev.get('role')} (expected assistant)" if not isinstance(prev, dict) or prev.get("role") != "assistant" else \
                           f"preceding assistant lacks tool_use id={tid}"
                    print(
                        f"[anthropic_proxy] strict: removing tool_result {tid} at msg[{i}] "
                        f"({_why})",
                        file=sys.stderr,
                    )
                    changed = True
                    continue
            new_content.append(block)

        if changed and not new_content:
            if m.get("role") in ("user", "assistant"):
                # Add a synthetic text so the message isn't empty
                new_content = [{"type": "text", "text": "[tool results removed during context repair]"}]
                changed = True
                print(f"[anthropic_proxy] strict: replaced empty {m.get('role')} msg[{i}] with placeholder", file=sys.stderr)
            else:
                # Non-user/assistant messages with empty content — drop
                changed = True
                print(f"[anthropic_proxy] strict: dropping empty {m.get('role')} msg[{i}]", file=sys.stderr)
                continue

        if changed:
            m = dict(m)
            m["content"] = new_content
        out.append(m)

    if not changed:
        return msgs

    # First non-system message leading with tool_result → prepend filler text
    for i, m in enumerate(out):
        if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
            c = m.get("content", [])
            if isinstance(c, list) and c and isinstance(c[0], dict) and c[0].get("type") == "tool_result":
                out[i] = dict(m)
                out[i]["content"] = [{"type": "text", "text": "[Context restored after truncation]"}] + c
                print(f"[anthropic_proxy] strict: prefixed tool_result-first msg[{i}] with filler", file=sys.stderr)
            break

    return out


def _message_has_nonempty_content(m: Any) -> bool:
    """Check whether a message dict has non-trivial content.

    Returns ``True`` when *m* contains text, ``tool_use``, or
    ``tool_result`` blocks (i.e. the message isn't a placeholder).
    """
    if not isinstance(m, dict):
        return False
    c = m.get("content")
    if isinstance(c, str):
        return bool(c.strip())
    if isinstance(c, list):
        for el in c:
            if isinstance(el, str) and el.strip():
                return True
            if isinstance(el, dict):
                if (
                    el.get("type") == "text"
                    and isinstance(el.get("text"), str)
                    and el["text"].strip()
                ):
                    return True
                if el.get("type") in ("tool_use", "tool_result"):
                    return True
                if isinstance(el.get("content"), str) and el["content"].strip():
                    return True
                if isinstance(el.get("content"), list) and el["content"]:
                    return True
        return False
    return False


# ---------------------------------------------------------------------------
# Module-level helper functions (path normalization, header filtering, usage logging)
# ---------------------------------------------------------------------------


def normalize_proxy_path(path: str, base_path: str) -> str:
    """Normalize and map incoming path, collapsing prefixes and preserving query string."""
    parsed = urlsplit(path)
    path_only = parsed.path
    query = parsed.query

    # Collapse duplicate /anthropic/anthropic prefixes
    while path_only.startswith("/anthropic/anthropic"):
        path_only = path_only.replace("/anthropic/anthropic", "/anthropic", 1)

    # If client used /anthropic/v1/*, remove the leading /anthropic to map to upstream /v1/*
    if path_only.startswith("/anthropic/v1"):
        path_only = path_only.replace("/anthropic", "", 1)

    # Ensure path starts with /v1 for upstream
    if not path_only.startswith("/v1") and path_only.startswith("/anthropic/v1"):
        path_only = path_only.replace("/anthropic", "", 1)

    # Reattach query if present and ensure upstream base path is preserved
    if base_path.endswith("/"):
        base_path = base_path[:-1]
    # Avoid double-prefixing if client already included base_path
    if base_path and not path_only.startswith(base_path):
        prefixed = base_path + path_only
    else:
        prefixed = path_only
    return prefixed + ("?" + query if query else "")


def filter_response_headers(resp_headers: list) -> tuple[list, bool]:
    """Filter hop-by-hop headers from response headers, detect chunked transfer encoding.

    Returns (filtered_headers, is_chunked).
    """
    filtered: list = []
    is_chunked = False
    for key, value in resp_headers:
        lk = key.lower()
        if lk in HOP_BY_HOP_HEADERS and lk != "transfer-encoding":
            continue
        if lk == "content-length":
            continue
        if lk == "transfer-encoding":
            if "chunked" in value.lower():
                is_chunked = True
            continue
        filtered.append((key, value))
    return filtered, is_chunked


def _extract_stream_usage(tail_bytes: bytes) -> bytes:
    """Scan the tail of a streaming SSE response for a 'data:' line containing usage.

    DeepSeek (and some OpenAI-compatible endpoints) append the usage object to the
    penultimate SSE frame. This function scans the buffered tail in reverse to find
    the last data line that carries a 'usage' key, then returns it as a minimal
    JSON dict so callers can parse it the same way as a non-streaming response.

    Returns the JSON bytes ``b'{"usage": {...}}'`` on success, or ``b''`` if no
    usage frame is found.
    """
    try:
        text = tail_bytes.decode("utf-8", errors="replace")
        for line in reversed(text.split("\n")):
            line = line.strip()
            if line.startswith("data: ") and "[DONE]" not in line:
                try:
                    payload = json.loads(line[6:])
                    if "usage" in payload:
                        return json.dumps({"usage": payload["usage"]}).encode("utf-8")
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return b""


# ---------------------------------------------------------------------------
# Balance / context-window helpers
# ---------------------------------------------------------------------------

_balance_cache: Dict[str, Any] = {"cny": None, "time": 0.0}
_BALANCE_CACHE_TTL = 120  # seconds between live balance fetches


def _fetch_deepseek_balance() -> Optional[str]:
    """Fetch topped_up_balance in CNY from DeepSeek API, cached for 120 s.

    Returns the raw string from the API (e.g. ``"17.82"``) or ``None`` when
    the upstream is not DeepSeek / the API key is missing / the call fails.
    """
    global _balance_cache
    now = time.time()
    if _balance_cache["cny"] is not None and now - _balance_cache["time"] < _BALANCE_CACHE_TTL:
        return _balance_cache["cny"]

    api_key: Optional[str] = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not api_key:
        return None

    try:
        conn = http.client.HTTPSConnection("api.deepseek.com", timeout=5)
        conn.request("GET", "/user/balance", headers={"Authorization": f"Bearer {api_key}"})
        resp = conn.getresponse()
        if resp.status == 200:
            data: Dict[str, Any] = json.loads(resp.read().decode())
            for bi in data.get("balance_infos", []):
                if bi.get("currency") == "CNY":
                    bal: str = bi.get("topped_up_balance", "0.00")
                    _balance_cache["cny"] = bal
                    _balance_cache["time"] = now
                    return bal
    except Exception:
        pass
    return None


def _format_context_bar(tokens: int, max_tokens: int) -> str:
    """Render a 10-segment █/▌/░ progress bar (20 half-steps, 5 % each) with colour.

    Each cell is split in half: █=full, ▌=half, ░=empty → 20 steps total.

    - Green (default)   -> < 70 %
    - Yellow/Amber      -> 70–89 %
    - Red               -> >= 90 %
    """
    pct = min(tokens / max_tokens * 100, 100.0) if max_tokens > 0 else 0
    total_half = round(pct / 5)  # 0 .. 20, each = 5 %
    chars = []
    for i in range(10):
        remain = total_half - i * 2
        if remain <= 0:
            chars.append("░")
        elif remain == 1:
            chars.append("▌")
        else:
            chars.append("█")
    bar = "".join(chars)
    if pct >= 90.0:
        bar = f"\033[31m{bar}\033[0m"
    elif pct >= 70.0:
        bar = f"\033[33m{bar}\033[0m"
    return f"{bar} {pct:.0f}%"


def log_usage(body: Optional[bytes], data: Optional[bytes], resp_status: int) -> None:
    """Log token usage from API response for cache monitoring.

    Supports three response formats via layered fallbacks:

    * **Anthropic** — ``input_tokens``, ``output_tokens``, ``cache_read_input_tokens``,
      ``cache_creation_input_tokens``.
    * **DeepSeek V4 Flash** (native) — ``prompt_tokens``, ``completion_tokens``,
      ``prompt_cache_hit_tokens``, ``prompt_cache_miss_tokens`` (all at top-level
      ``usage``, **not** nested under ``prompt_tokens_details``).
    * **DeepSeek V2 / V3** (legacy) — ``prompt_tokens_details.cached_tokens``.

    Streaming responses pass the tail-buffer usage through
    :func:`_extract_stream_usage` before this function is called.
    """
    if body and resp_status == 200 and data:
        try:
            r = json.loads(data.decode("utf-8"))
            u = r.get("usage", {})

            # ── input tokens ──────────────────────────────────────────────
            input_tokens = u.get(
                "input_tokens",
                u.get("prompt_tokens", 0),  # DeepSeek native fallback
            )

            # ── output tokens ─────────────────────────────────────────────
            output_tokens = u.get(
                "output_tokens",
                u.get("completion_tokens", 0),  # DeepSeek native fallback
            )

            # ── cache read (tokens served from cache) ─────────────────────
            # Lookup priority (per deep-research on DeepSeek V4 Flash):
            #   1. prompt_cache_hit_tokens     (DeepSeek V4 Flash, top-level)
            #   2. cache_read_input_tokens     (Anthropic standard)
            #   3. prompt_tokens_details.cached_tokens (DeepSeek V2/V3 legacy)
            cache_read = u.get("prompt_cache_hit_tokens", 0)
            if not cache_read:
                cache_read = u.get("cache_read_input_tokens", 0)
            if not cache_read:
                ptd = u.get("prompt_tokens_details")
                if isinstance(ptd, dict):
                    cache_read = ptd.get("cached_tokens", 0)

            # ── cache miss (tokens NOT in cache, freshly processed) ───────
            cache_miss = u.get("prompt_cache_miss_tokens", 0)
            if not cache_miss:
                # Anthropic: cache_creation_input_tokens = tokens *written* to cache,
                # not the same as miss, but we use it as a rough proxy for display.
                cache_miss = u.get("cache_creation_input_tokens", 0)

            body_kb = len(body) / 1024

            # ── hit rate calculation ──────────────────────────────────────
            # DeepSeek V4: prompt_tokens (input) is new tokens sent this
            # round, NOT inclusive of cached tokens. Total useful work by
            # the server = new input + cached prefix tokens.
            denom = input_tokens + cache_read
            if cache_miss > 0:
                denom = max(denom, cache_read + cache_miss)

            pct = min((cache_read / denom * 100), 100.0) if denom > 0 else 0

            # bar: body bytes vs actual auto-compact trigger (128K tok × 85% × ~4 chars/tok ≈ 435K)
            body_chars = len(body)  # bytes ≈ chars for ASCII/English
            COMPACT_BODY_LIMIT = int(os.environ.get(
                "ANTHROPIC_PROXY_BAR_LIMIT", "435200",
            ))
            context_bar = _format_context_bar(body_chars, COMPACT_BODY_LIMIT)
            balance = _fetch_deepseek_balance()
            balance_str = f" ¥{balance}" if balance else ""
            print(f"[anthropic_proxy] usage: input={input_tokens} cache_read={cache_read} "
                  f"cache_miss={cache_miss} output={output_tokens} "
                  f"hit_rate={pct:.0f}% body={body_kb:.0f}KB {context_bar}{balance_str}",
                  file=sys.stderr)
        except Exception:
            pass


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = urlsplit(DEFAULT_UPSTREAM)

    def _build_upstream_connection(self) -> http.client.HTTPConnection:
        """Build HTTP/HTTPS connection to the upstream proxy target."""
        if self.upstream.scheme == "https":
            conn = http.client.HTTPSConnection(
                self.upstream.hostname,
                self.upstream.port or 443,
                timeout=300,
            )
        else:
            conn = http.client.HTTPConnection(
                self.upstream.hostname,
                self.upstream.port or 80,
                timeout=300,
            )
        return conn

    def _stream_response(self, conn: http.client.HTTPConnection, resp: http.client.HTTPResponse) -> bytes:
        """Stream chunked response to client with idle timeout handling.

        Captures the last ~2.5 kB of the raw stream and, after the connection
        closes, hands it to :func:`_extract_stream_usage` so that usage metrics
        embedded in the final SSE frame (DeepSeek / OpenAI style) are preserved
        for ``log_usage``.  Returns the extracted usage JSON bytes on success,
        or ``b""`` when there is no usage frame.
        """
        chunk_size = int(os.environ.get("ANTHROPIC_PROXY_STREAM_CHUNK", "4096"))
        idle_timeout = float(os.environ.get("ANTHROPIC_PROXY_STREAM_TIMEOUT", "60"))
        try:
            if hasattr(conn, "sock") and conn.sock:
                conn.sock.settimeout(idle_timeout)
        except Exception:
            pass
        last_read = time.time()
        tail_buffer = b""  # rolling buffer of the last ~2.5 kB
        try:
            while True:
                try:
                    chunk = resp.read(chunk_size)
                except socket.timeout:
                    if time.time() - last_read > idle_timeout:
                        print(f"[anthropic_proxy] stream idle timeout after {idle_timeout}s", file=sys.stderr)
                        break
                    else:
                        continue
                if not chunk:
                    break
                last_read = time.time()
                tail_buffer = (tail_buffer + chunk)[-2500:]
                try:
                    size_hex = ("%x" % len(chunk)).encode("ascii")
                    self.wfile.write(size_hex + b"\r\n" + chunk + b"\r\n")
                    self.wfile.flush()
                except BrokenPipeError:
                    break
        except Exception:
            pass
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass
        return _extract_stream_usage(tail_buffer)

    def _nonstream_response(self, data: bytes) -> None:
        """Send non-streaming response body to client."""
        try:
            self.send_header("Content-Length", str(len(data)))
        except Exception:
            pass
        self.end_headers()
        if data:
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except BrokenPipeError:
                pass

    def _inject_auth_header(self, headers: Dict[str, str]) -> None:
        """Inject Authorization header from env if none is set."""
        try:
            if not any(k.lower() == 'authorization' for k in headers):
                key = os.environ.get('DEEPSEEK_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN')
                if key:
                    headers['Authorization'] = f'Bearer {key}'
                    if os.environ.get('ANTHROPIC_PROXY_DEBUG','0') == '1':
                        print('[anthropic_proxy] injected Authorization header from env', file=sys.stderr)
        except Exception:
            pass

    def _forward(self) -> None:
        body = b""
        if self.command in {"POST", "PUT", "PATCH"}:
            length = int(self.headers.get("content-length") or "0")
            body = self.rfile.read(length) if length > 0 else b""

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
            and key.lower() not in {"host", "content-length", "date", "user-agent", "x-request-id", "x-trace-id"}
        }
        # enforce a fixed User-Agent to improve prefix caching stability
        headers['User-Agent'] = 'devforge-proxy/1.0'

        if body:
            content_type = self.headers.get("content-type", "")
            if "application/json" in content_type:
                # Strict JSON validation: return 400 for malformed JSON or unexpected structure
                try:
                    payload = json.loads(body.decode("utf-8"))
                except Exception as e:
                    err = json.dumps({"error": {"message": "Malformed JSON in request", "detail": str(e)}}).encode("utf-8")
                    try:
                        self.send_response(400, "Bad Request")
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(err)))
                        self.end_headers()
                        self.wfile.write(err)
                        self.wfile.flush()
                    except Exception:
                        pass
                    return

                if not isinstance(payload, dict):
                    err = json.dumps({"error": {"message": "Expected JSON object in request body"}}).encode("utf-8")
                    try:
                        self.send_response(400, "Bad Request")
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(err)))
                        self.end_headers()
                        self.wfile.write(err)
                        self.wfile.flush()
                    except Exception:
                        pass
                    return

                cache_stripped = _strip_cache_control(payload)
                payload, cch_stripped = _strip_system_billing_header(payload)
                if cch_stripped:
                    print("[anthropic_proxy] stripped x-anthropic-billing-header from top-level system field",
                          file=sys.stderr)
                payload, _ = _flatten_system_blocks(payload)
                payload, system_rewritten = _sanitize_messages(payload)
                payload, orphans_fixed = _fix_orphan_tool_results(payload)

                # Simple truncation: approximate token budget by characters and collapse older messages
                try:
                    APPROX_CHARS_PER_TOKEN = 4
                    try:
                        APPROX_CHARS_PER_TOKEN = int(os.environ.get("ANTHROPIC_PROXY_CHARS_PER_TOKEN", "4"))
                    except Exception:
                        pass
                    # char_budget based on model context window (1M for both Flash & Pro),
                    # NOT MAX_TOKENS — so truncation aligns with what the model can actually see.
                    # Override via ANTHROPIC_PROXY_CHAR_BUDGET env var if needed.
                    char_budget = DEEPSEEK_CONTEXT_WINDOW * APPROX_CHARS_PER_TOKEN
                    try:
                        char_budget = int(os.environ.get("ANTHROPIC_PROXY_CHAR_BUDGET", str(char_budget)))
                    except Exception:
                        pass
                    if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
                        msgs = payload["messages"]
                        # compute approximate characters in each message using _flatten_text on content
                        sizes = [len(_flatten_text(m.get("content"))) if isinstance(m, dict) else len(str(m)) for m in msgs]
                        total_chars = sum(sizes)
                        if total_chars > int(char_budget * 0.85):
                            # keep most recent messages up to a lower threshold, collapse older ones
                            # Work with indices so we can preserve required dependencies (tool_use/tool_result pairs)
                            entries = list(enumerate(msgs))
                            kept_idxs = []
                            accum = 0
                            for idx, m in reversed(entries):
                                sz = sizes[idx]
                                if accum + sz > int(char_budget * 0.7):
                                    break
                                kept_idxs.append(idx)
                                accum += sz
                            # if nothing was kept because a single recent message exceeds the threshold,
                            # preserve the most recent message to avoid sending an empty conversation.
                            if not kept_idxs and msgs:
                                kept_idxs = [len(msgs) - 1]
                                accum = sizes[-1]

                            # map tool_use id -> message index using nested scanning
                            tool_use_index = {}
                            for i, m in enumerate(msgs):
                                ids = _collect_tool_use_ids_present(m)
                                for tid in ids:
                                    # prefer the earliest (lowest) index if multiple occurrences
                                    if tid not in tool_use_index:
                                        tool_use_index[tid] = i

                            # ensure any tool_result kept also keeps its corresponding tool_use
                            needed_idxs = set(kept_idxs)
                            for idx in list(kept_idxs):
                                m = msgs[idx]
                                refs = _collect_referenced_tool_use_ids(m)
                                for rid in refs:
                                    if rid in tool_use_index:
                                        needed_idxs.add(tool_use_index[rid])

                            # build final kept list in original order (but we may need to reorder tool_use messages)
                            kept = [msgs[i] for i in sorted(needed_idxs)]

                            # Compute which tool_use ids are actually included in kept messages
                            kept_tool_use_ids = set()
                            for m in kept:
                                kept_tool_use_ids |= _collect_tool_use_ids_present(m)

                            for i, m in enumerate(kept):
                                kept[i] = _cleanup(m, kept_tool_use_ids)
                            # drop any top-level None entries (shouldn't normally happen)
                            kept = [m for m in kept if m is not None]

                            # After pruning orphan results, ensure that any tool_result message is immediately
                            # preceded by its corresponding tool_use message as required by upstream API.
                            final_kept = []
                            appended_idxs = set()
                            for idx in sorted(needed_idxs):
                                # if this original message contains tool_result refs, ensure tool_use message(s)
                                # are inserted immediately before it in the output sequence
                                m = msgs[idx]
                                refs = _collect_referenced_tool_use_ids(m)
                                for rid in refs:
                                    tu_idx = tool_use_index.get(rid)
                                    if tu_idx is not None and tu_idx not in appended_idxs:
                                        final_kept.append(msgs[tu_idx])
                                        appended_idxs.add(tu_idx)
                                if idx not in appended_idxs:
                                    final_kept.append(m)
                                    appended_idxs.add(idx)

                            notice = {"role": "system", "content": "Conversation truncated by proxy: older messages removed to fit model context."}
                            payload["messages"] = [notice] + final_kept

                            # Enforce upstream requirement: any tool_result block must have its corresponding
                            # tool_use block in the immediately previous message. If not, remove those tool_result nodes.
                            payload["messages"] = _remove_adjacent_orphans(payload["messages"])

                            # Remove messages that have empty content (upstream requires non-empty message content)
                            cleaned_msgs = []
                            for m in payload.get('messages', []):
                                if _message_has_nonempty_content(m):
                                    cleaned_msgs.append(m)
                                else:
                                    # allow system notice messages even if content is empty? skip them to satisfy upstream
                                    print(f"[anthropic_proxy] dropping empty-message role={m.get('role')}", file=sys.stderr)

                            # If cleaning removed all non-system messages, try to preserve most recent original non-empty message
                            if len([x for x in cleaned_msgs if x.get('role') != 'system']) == 0:
                                # try to find last non-empty message from original msgs
                                for orig in reversed(msgs):
                                    if isinstance(orig, dict) and _message_has_nonempty_content(orig) and orig.get('role') != 'system':
                                        cleaned_msgs.append(orig)
                                        print('[anthropic_proxy] restored last non-system message to avoid empty payload', file=sys.stderr)
                                        break

                            payload["messages"] = cleaned_msgs

                            headers['X-Proxy-Conversation-Truncated'] = '1'
                            print(f"[anthropic_proxy] truncated conversation: was {total_chars} chars, kept {accum} chars", file=sys.stderr)
                except Exception as e:
                    print(f"[anthropic_proxy] truncation check failed: {e}", file=sys.stderr)

                # Strip [1m] suffix from model name (Claude Code uses it to detect 1M context models)
                # Claude Code should strip it before sending, but we do it here as a safety net
                if isinstance(payload, dict):
                    m = payload.get("model")
                    if isinstance(m, str) and m.endswith("[1m]"):
                        m = m[:-4]
                        payload["model"] = m
                        print(f"[anthropic_proxy] stripped [1m] suffix -> {m}", file=sys.stderr)

                # Remap requested DeepSeek model names to local llama model names if configured
                if isinstance(payload, dict):
                    m = payload.get("model")
                    if not isinstance(m, str) or not m:
                        err = json.dumps({"error": {"message": "Missing or invalid 'model' field in request"}}).encode("utf-8")
                        try:
                            self.send_response(400, "Bad Request")
                            self.send_header("Content-Type", "application/json; charset=utf-8")
                            self.send_header("Content-Length", str(len(err)))
                            self.end_headers()
                            self.wfile.write(err)
                            self.wfile.flush()
                        except Exception:
                            pass
                        return

                    # Exact match mapping
                    if isinstance(m, str) and m in MODEL_MAP:
                        new_m = MODEL_MAP[m]
                        payload["model"] = new_m
                        print(f"[anthropic_proxy] remapped model {m} -> {new_m}", file=sys.stderr)
                    else:
                        # Prefix/fuzzy mapping: map variants like 'qwen3-30b-a3b-local' to 'qwen3-30b-a3b'
                        for k, v in MODEL_MAP.items():
                            if isinstance(m, str) and m.startswith(k):
                                payload["model"] = v
                                print(f"[anthropic_proxy] remapped model {m} -> {v} (prefix match)", file=sys.stderr)
                                break
                # Always re-serialize for consistent JSON formatting (compact, sorted keys)
                # to ensure DeepSeek's automatic prefix caching sees identical byte prefixes.
                # Serialization settings controllable via environment for A/B testing
                _sort_keys = os.environ.get("ANTHROPIC_PROXY_SORT_KEYS", "1") == "1"
                _compact = os.environ.get("ANTHROPIC_PROXY_COMPACT_JSON", "1") == "1"
                _seps = (",", ":") if _compact else (", ", ": ")

                try:
                    msgs_before = payload.get('messages', [])
                    filtered = [m for m in msgs_before if _message_has_nonempty_content(m) or (isinstance(m, dict) and m.get('role') == 'system' and isinstance(m.get('content'), str) and m.get('content').strip())]
                    # If no non-system messages remain, try to restore last non-system from original msgs
                    _orig_msgs = payload.get("messages", [])
                    if len([x for x in filtered if isinstance(x, dict) and x.get('role') != 'system']) == 0:
                        for orig in reversed(_orig_msgs):
                            if isinstance(orig, dict) and orig.get('role') != 'system' and _message_has_nonempty_content(orig):
                                filtered.append(orig)
                                print('[anthropic_proxy] restored last non-system message during final validation', file=sys.stderr)
                                break
                    payload['messages'] = filtered
                except Exception:
                    # fail safe: leave payload as-is
                    pass

                # ── Strict tool_use/tool_result adjacency fix ──────────────
                # Enforce DeepSeek's strict requirement: every tool_result must
                # have its matching tool_use in the immediately preceding
                # assistant message. This replaces the final orphan fix, the
                # _remove_adjacent_orphans check, and the tool_result-first
                # guard with a single comprehensive pass.
                try:
                    payload['messages'] = _strict_tool_adjacency_fix(payload.get('messages', []))
                except Exception as _e:
                    print(f"[anthropic_proxy] strict adjacency fix failed: {_e}", file=sys.stderr)

                # If all user/assistant messages were removed (only system notice remains),
                # add a synthetic user prompt so the API call doesn't fail with "no user message".
                if payload.get('messages') and not any(
                    m.get('role') in ('user', 'assistant') for m in payload['messages']
                ):
                    payload['messages'].append({
                        "role": "user",
                        "content": "Please continue — my previous message was lost during context truncation."
                    })
                    print("[anthropic_proxy] added synthetic user message after full orphan cleanup", file=sys.stderr)

                # Ensure there's at least one message (upstream requires messages non-empty)
                if not payload.get('messages'):
                    payload['messages'] = [{"role":"system","content":"Conversation truncated by proxy: no messages available after cleaning."}]

                # ── Cache padding (extends DeepSeek's automatic prefix cache) ──
                payload, _ = _apply_cache_padding(payload)

                body = _json_dumps_system_first(payload, ensure_ascii=False, sort_keys=_sort_keys, separators=_seps)
                body = body.encode("utf-8")

                # ── Insurance truncation (proxy safety net) ────────────────
                # Removes oldest non-system messages when body exceeds 90% of
                # Claude's auto-compact trigger (612K chars).  Fires only when
                # Claude's own auto-compact hasn't reduced the body in time.
                _INSURANCE_TRIGGER = int(os.environ.get(
                    "ANTHROPIC_PROXY_INSURANCE_TRIGGER",
                    str(int(680_000 * 0.9)),  # default: 90% of 680K = 612K
                ))
                if _INSURANCE_TRIGGER > 0 and len(body) > _INSURANCE_TRIGGER:
                    try:
                        msgs = payload.get("messages", [])
                        # Split into system and non-system messages
                        sys_msgs = [m for m in msgs if isinstance(m, dict) and m.get("role") == "system"]
                        non_sys_msgs = [m for m in msgs if isinstance(m, dict) and m.get("role") != "system"]
                        before = len(msgs)
                        while len(non_sys_msgs) > 1 and len(body) > _INSURANCE_TRIGGER:
                            non_sys_msgs.pop(0)  # remove oldest
                            payload["messages"] = sys_msgs + non_sys_msgs
                            body = _json_dumps_system_first(payload, ensure_ascii=False,
                                              sort_keys=_sort_keys, separators=_seps)
                            body = body.encode("utf-8")
                        if len(msgs) < before:
                            print(f"[anthropic_proxy] insurance: removed {before - len(msgs)} msgs, "
                                  f"body={len(body)} chars", file=sys.stderr)
                            headers['X-Proxy-Insurance-Truncated'] = '1'
                            # Strict adjacency fix after insurance truncation
                            try:
                                payload['messages'] = _strict_tool_adjacency_fix(payload.get('messages', []))
                            except Exception:
                                pass
                            if not payload.get('messages') or not any(
                                m.get('role') in ('user', 'assistant') for m in payload['messages']
                            ):
                                payload['messages'] = payload.get('messages', []) + [
                                    {"role": "user", "content": "Please continue — context was truncated."}
                                ]
                            # Re-serialize body after guard/synthetic message modification
                            body = _json_dumps_system_first(payload, ensure_ascii=False,
                                              sort_keys=_sort_keys, separators=_seps)
                            body = body.encode("utf-8")
                    except Exception as e:
                        print(f"[anthropic_proxy] insurance truncation failed: {e}", file=sys.stderr)

                # attach SHA256 of the serialized body to headers (also write to debug file when enabled)
                try:
                    sha = hashlib.sha256(body).hexdigest()
                    headers['X-Proxy-Body-SHA256'] = sha
                    if os.environ.get('ANTHROPIC_PROXY_DEBUG','0') == '1':
                        ts = int(time.time() * 1000)
                        dump_base = f"/tmp/anthropic_debug_{ts}"
                        with open(dump_base + '_forward.json','wb') as fwd:
                            fwd.write(body)
                        with open(dump_base + '_sha256.txt','w', encoding='utf-8') as sf:
                            sf.write(sha)
                        with open(dump_base + '_headers.json','w', encoding='utf-8') as hf:
                            json.dump(headers, hf, ensure_ascii=False, indent=2)
                        print(f"[anthropic_proxy] wrote debug dumps {dump_base}_*.json", file=sys.stderr)
                except Exception:
                    pass
                if cache_stripped:
                    print(f"[anthropic_proxy] stripped cache_control from request", file=sys.stderr)
                if system_rewritten:
                    print(f"[anthropic_proxy] moved system-role messages to top-level system field", file=sys.stderr)
                if orphans_fixed:
                    print(f"[anthropic_proxy] fixed orphan tool_result blocks (tool_use/tool_result mismatch after compaction)", file=sys.stderr)

        self._inject_auth_header(headers)

        conn = self._build_upstream_connection()
        path = normalize_proxy_path(self.path, self.upstream.path or "")
        data: Optional[bytes] = None

        try:
            conn.request(self.command, path, body=body if body else None, headers=headers)
            resp = conn.getresponse()
        except Exception as e:
            print(f"[anthropic_proxy] upstream request failed: {e}", file=sys.stderr)
            conn.close()
            err = json.dumps({"error": {"message": f"Upstream connection failed: {e}", "type": "upstream_error"}}).encode("utf-8")
            try:
                self.send_response(502, "Bad Gateway")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                self.wfile.flush()
            except Exception:
                pass
            return

        try:
            self.send_response(resp.status, resp.reason)

            resp_headers = resp.getheaders()
            filtered_headers, is_chunked = filter_response_headers(resp_headers)

            for key, value in filtered_headers:
                self.send_header(key, value)

            # handle streaming vs non-streaming responses
            if is_chunked:
                try:
                    self.send_header("Transfer-Encoding", "chunked")
                except Exception:
                    pass
                self.end_headers()
                data = self._stream_response(conn, resp)
            else:
                data = resp.read()
                self._nonstream_response(data)

            log_usage(body, data, resp.status)

            # ── 400 debug dump ────────────────────────────────────────────
            # dump request body to /tmp when upstream returns 400 so we can
            # inspect the exact payload that triggered the rejection.
            if resp.status >= 400 and body:
                try:
                    ts = int(time.time() * 1000)
                    dump_path = f"/tmp/anthropic_4xx_{ts}.json"
                    with open(dump_path, "wb") as _df:
                        _df.write(body)
                    # keep only the last 5 dumps
                    _existing = sorted(p for p in os.listdir("/tmp") if p.startswith("anthropic_4xx_"))
                    for _stale in _existing[:-5]:
                        try: os.remove(os.path.join("/tmp", _stale))
                        except Exception: pass
                    print(f"[anthropic_proxy] upstream returned {resp.status}, dumped body to {dump_path}",
                          file=sys.stderr)
                except Exception as _de:
                    print(f"[anthropic_proxy] debug dump failed: {_de}", file=sys.stderr)

        finally:
            conn.close()

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_PUT(self) -> None:
        self._forward()

    def do_PATCH(self) -> None:
        self._forward()

    def do_DELETE(self) -> None:
        self._forward()

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[anthropic_proxy] {self.address_string()} - {fmt % args}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default=os.environ.get("ANTHROPIC_PROXY_LISTEN", DEFAULT_LISTEN))
    parser.add_argument(
        "--upstream",
        default=os.environ.get("ANTHROPIC_PROXY_UPSTREAM", DEFAULT_UPSTREAM),
    )
    args = parser.parse_args()

    host, port_text = args.listen.rsplit(":", 1)
    port = int(port_text)
    ProxyHandler.upstream = urlsplit(args.upstream)
    # allow quick restart / rebind after previous socket in TIME_WAIT
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((host, port), ProxyHandler)
    print(f"[anthropic_proxy] listening on {host}:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
