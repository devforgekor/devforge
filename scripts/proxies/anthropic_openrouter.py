#!/usr/bin/env python3
# Status: production
# Path: systemd:anthropic-openrouter-proxy
"""Anthropic-to-OpenAI format conversion proxy for OpenRouter.

  Claude Code → anthropic-openrouter-proxy (:44778)
    → OpenRouter /v1/chat/completions → DeepSeek V4 Flash (paid, GMICloud pinned)

Supports streaming SSE, tool_use↔tool_calls conversion, and model name mapping.
"""

import hashlib
import http.client
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import urlsplit

# ── Config ─────────────────────────────────────────────────────────────────
DEFAULT_LISTEN = "127.0.0.1:44778"
DEFAULT_UPSTREAM = "https://openrouter.ai/api/v1"

# Pin to a specific OpenRouter provider slug for cache consistency.
# Set to None to use default load balancing (price-weighted + fallback).
PROVIDER_PIN = "gmicloud"

MODEL_MAP = {
    "claude": "deepseek/deepseek-v4-flash",
    "claude-pro": "deepseek/deepseek-v4-pro",
    "claude-sonnet-4-20250514": "deepseek/deepseek-v4-flash",
    "claude-sonnet-4-6": "deepseek/deepseek-v4-flash",
    "claude-opus-4-8": "deepseek/deepseek-v4-pro",
}
# Reverse map for response model field
_OPENAI_TO_ANTHROPIC_MODEL = {v: k for k, v in MODEL_MAP.items()}

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
STRIP_RESP_HEADERS = {
    "host",
    "content-length",
    "date",
    "set-cookie",
    "www-authenticate",
}

ANTHROPIC_API_KEY = os.environ.get("OPENROUTER_MESIDS_API_KEY") or ""

# Ordered list of OpenRouter API keys. Tried in order; on 401/402/403 (auth/credit)
# the proxy retries with the next key. Non-retryable status codes (4xx other than
# 401/402/403, all 2xx/3xx/5xx) are passed through unchanged.
API_KEYS: List[str] = [
    os.environ.get("OPENROUTER_MESIDS_API_KEY", ""),
    os.environ.get("OPENROUTER_MINIPARK4U_API_KEY", ""),
    os.environ.get("OPENROUTER_HYEONMINPARK4U_API_KEY", ""),
    os.environ.get("OPENROUTER_API_KEY", ""),
]
# Filter out empty entries while preserving order.
API_KEYS = [k for k in API_KEYS if k]

# Status codes that indicate the current key is bad (auth failure or no credit).
# Trigger key rotation to the next entry in API_KEYS.
RETRY_KEY_STATUSES = {401, 402, 403}


def _flatten_text(content) -> str:
    """Flatten Anthropic content blocks or string to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    inner = block.get("content", "")
                    parts.append(_flatten_text(inner) if isinstance(inner, list) else str(inner))
        return " ".join(p.strip() for p in parts if p.strip())
    return ""


def _resolve_api_key() -> str:
    """Resolve primary OpenRouter API key from env (Azure KV via systemd)."""
    return API_KEYS[0] if API_KEYS else ""


def _anthropic_to_openai(anthropic_body: dict) -> dict:
    """Convert Anthropic Messages API request to OpenAI Chat Completions."""
    system = anthropic_body.get("system", "")
    if isinstance(system, list):
        system = " ".join(
            b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"
        )

    msgs: List[dict] = []
    if system:
        msgs.append({"role": "system", "content": system})

    for msg in anthropic_body.get("messages", []):
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "user":
            if isinstance(content, str):
                msgs.append({"role": "user", "content": content})
            elif isinstance(content, list):
                text_buf: List[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type", "")
                    if bt == "text":
                        text_buf.append(block.get("text", ""))
                    elif bt == "tool_result":
                        if text_buf:
                            msgs.append({"role": "user", "content": " ".join(text_buf)})
                            text_buf = []
                        tc = block.get("content", "")
                        if isinstance(tc, list):
                            tc = " ".join(
                                b.get("text", "")
                                for b in tc
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        msgs.append(
                            {
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id", ""),
                                "content": str(tc),
                            }
                        )
                if text_buf:
                    msgs.append({"role": "user", "content": " ".join(text_buf)})

        elif role == "assistant":
            if isinstance(content, str):
                msgs.append({"role": "assistant", "content": content})
            elif isinstance(content, list):
                text_parts: List[str] = []
                tool_calls: List[dict] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type", "")
                    if bt == "text":
                        text_parts.append(block.get("text", ""))
                    elif bt == "tool_use":
                        tool_calls.append(
                            {
                                "id": block["id"],
                                "type": "function",
                                "function": {
                                    "name": block["name"],
                                    "arguments": json.dumps(
                                        block.get("input", {}), ensure_ascii=False
                                    ),
                                },
                            }
                        )
                msg_obj: dict = {
                    "role": "assistant",
                    "content": " ".join(text_parts) if text_parts else None,
                }
                if tool_calls:
                    msg_obj["tool_calls"] = tool_calls
                msgs.append(msg_obj)

    model = MODEL_MAP.get(anthropic_body.get("model", ""), "deepseek/deepseek-v4-flash")
    stream = anthropic_body.get("stream", False)

    req: dict = {
        "model": model,
        "messages": msgs,
        "max_tokens": anthropic_body.get("max_tokens", 4096),
        "stream": stream,
        "temperature": anthropic_body.get("temperature", 1.0),
    }
    if stream:
        req["stream_options"] = {"include_usage": True}
    top_p = anthropic_body.get("top_p")
    if top_p is not None:
        req["top_p"] = top_p
    stop = anthropic_body.get("stop_sequences")
    if stop:
        req["stop"] = stop if isinstance(stop, list) else [stop]

    # Convert tools (Anthropic format → OpenAI format)
    tools = anthropic_body.get("tools")
    if tools and isinstance(tools, list):
        openai_tools = []
        for t in tools:
            openai_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                }
            )
        req["tools"] = openai_tools

    # Convert tool_choice
    tc = anthropic_body.get("tool_choice")
    if tc and isinstance(tc, dict):
        tc_type = tc.get("type", "auto")
        if tc_type == "any":
            req["tool_choice"] = "required"
        elif tc_type == "none":
            req["tool_choice"] = "none"
        elif tc_type == "tool":
            tc_name = tc.get("name", "")
            if tc_name:
                req["tool_choice"] = {"type": "function", "function": {"name": tc_name}}
        # "auto" is the default in OpenAI, no need to set it explicitly

    # Pin to specific provider for cache consistency
    if PROVIDER_PIN:
        req["provider"] = {"only": [PROVIDER_PIN]}

    return req


def _openai_to_anthropic_nonstream(openai_resp: dict, anthropic_model: str) -> dict:
    """Convert non-streaming OpenAI response to Anthropic format."""
    choice = openai_resp["choices"][0]
    msg = choice.get("message", {})

    content: list = []
    text = msg.get("content")
    if text:
        content.append({"type": "text", "text": text})

    for tc in msg.get("tool_calls", []):
        try:
            inp = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            inp = {}
        content.append(
            {
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["function"]["name"],
                "input": inp,
            }
        )

    finish = choice.get("finish_reason", "stop")
    stop_reason = (
        "end_turn" if finish == "stop" else ("tool_use" if finish == "tool_calls" else finish)
    )

    usage = openai_resp.get("usage", {})

    # Forward cached_tokens info if present
    cached_tokens = 0
    ptd = usage.get("prompt_tokens_details", {})
    if isinstance(ptd, dict):
        cached_tokens = ptd.get("cached_tokens", 0)

    return {
        "id": f"msg_{int(time.time() * 1000)}",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": anthropic_model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": cached_tokens,
        },
    }


# ── Streaming converter ────────────────────────────────────────────────────


class _StreamConverter:
    """State machine: OpenAI SSE chunks → Anthropic SSE events."""

    def __init__(self, anthropic_model: str):
        self.model = anthropic_model
        self.msg_id = f"msg_{int(time.time() * 1000)}_{random_id(6)}"
        self.block_idx = 0  # content block index
        self.text_block_open = False
        self.tool_buffers: Dict[int, dict] = {}  # openai_tc_idx -> {id, name, args_buffer}
        self.open_tool_indices: List[int] = []  # ordered list of openai tc indices
        self.meta_sent = False
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.final_stop_reason = "end_turn"
        self.ended = False

    def _maybe_send_meta(self) -> str:
        if self.meta_sent:
            return ""
        self.meta_sent = True
        meta = {
            "type": "message_start",
            "message": {
                "id": self.msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": self.model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 1},
            },
        }
        return f"event: message_start\ndata: {json.dumps(meta)}\n\n"

    def _emit_text_start(self) -> str:
        if self.text_block_open:
            return ""
        self.text_block_open = True
        ev = {
            "type": "content_block_start",
            "index": self.block_idx,
            "content_block": {"type": "text", "text": ""},
        }
        return f"event: content_block_start\ndata: {json.dumps(ev)}\n\n"

    def _emit_text_delta(self, text: str) -> str:
        if not self.text_block_open:
            return self._emit_text_start() + self._emit_text_delta(text)
        ev = {
            "type": "content_block_delta",
            "index": self.block_idx,
            "delta": {"type": "text_delta", "text": text},
        }
        return f"event: content_block_delta\ndata: {json.dumps(ev)}\n\n"

    def _close_text(self) -> str:
        if not self.text_block_open:
            return ""
        self.text_block_open = False
        ev = {"type": "content_block_stop", "index": self.block_idx}
        self.block_idx += 1
        return f"event: content_block_stop\ndata: {json.dumps(ev)}\n\n"

    def _open_tool(self, tc_idx: int, tc_id: str, name: str) -> str:
        out = self._close_text()
        self.tool_buffers[tc_idx] = {"id": tc_id, "name": name, "args": ""}
        if tc_idx not in self.open_tool_indices:
            self.open_tool_indices.append(tc_idx)
        anthropic_tool_id = _openai_to_anthropic_tool_id(tc_id)
        ev = {
            "type": "content_block_start",
            "index": self.block_idx,
            "content_block": {
                "type": "tool_use",
                "id": anthropic_tool_id,
                "name": name,
                "input": {},
            },
        }
        out += f"event: content_block_start\ndata: {json.dumps(ev)}\n\n"
        return out

    def _tool_delta(self, tc_idx: int, args_chunk: str) -> str:
        buf = self.tool_buffers.get(tc_idx)
        if buf is None:
            return ""
        buf["args"] += args_chunk
        ev = {
            "type": "content_block_delta",
            "index": self.block_idx,
            "delta": {"type": "input_json_delta", "partial_json": args_chunk},
        }
        return f"event: content_block_delta\ndata: {json.dumps(ev)}\n\n"

    def _emit_tool_use_block(self, tc_idx: int, tc_id: str, name: str, args: str) -> str:
        """Emit a complete tool_use block (when tool_calls arrive not in delta form)."""
        out = self._close_text()
        anthropic_tool_id = _openai_to_anthropic_tool_id(tc_id)
        ev = {
            "type": "content_block_start",
            "index": self.block_idx,
            "content_block": {
                "type": "tool_use",
                "id": anthropic_tool_id,
                "name": name,
                "input": {},
            },
        }
        out += f"event: content_block_start\ndata: {json.dumps(ev)}\n\n"
        if args:
            ev2 = {
                "type": "content_block_delta",
                "index": self.block_idx,
                "delta": {"type": "input_json_delta", "partial_json": args},
            }
            out += f"event: content_block_delta\ndata: {json.dumps(ev2)}\n\n"
        ev3 = {"type": "content_block_stop", "index": self.block_idx}
        out += f"event: content_block_stop\ndata: {json.dumps(ev3)}\n\n"
        self.block_idx += 1
        return out

    def _close_tool_blocks(self) -> str:
        out = ""
        for tc_idx in self.open_tool_indices:
            ev = {"type": "content_block_stop", "index": self.block_idx}
            out += f"event: content_block_stop\ndata: {json.dumps(ev)}\n\n"
            self.block_idx += 1
        self.open_tool_indices = []
        self.tool_buffers = {}
        return out

    def _emit_message_delta(self) -> str:
        ev = {
            "type": "message_delta",
            "delta": {
                "stop_reason": self.final_stop_reason,
                "stop_sequence": None,
            },
            "usage": {
                "output_tokens": self.output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": self.cached_tokens,
            },
        }
        return f"event: message_delta\ndata: {json.dumps(ev)}\n\n"

    def _emit_message_stop(self) -> str:
        self.ended = True
        return 'event: message_stop\ndata: {"type":"message_stop"}\n\n'

    def process_chunk(self, chunk: dict) -> Optional[str]:
        """Process one OpenAI SSE chunk. Returns Anthropic event string or None."""
        if self.ended:
            return None

        choices = chunk.get("choices", [])
        if not choices:
            # Could be the final usage chunk (no choices, but has usage)
            usage = chunk.get("usage", {})
            if usage:
                self.input_tokens = usage.get("prompt_tokens", self.input_tokens)
                self.output_tokens = usage.get("completion_tokens", self.output_tokens)
                ptd = usage.get("prompt_tokens_details", {})
                if isinstance(ptd, dict):
                    self.cached_tokens = ptd.get("cached_tokens", self.cached_tokens)
            return None

        choice = choices[0]
        delta = choice.get("delta", {})
        finish = choice.get("finish_reason")
        usage = choice.get("usage") or chunk.get("usage") or {}

        # Track usage from any chunk that has it
        if isinstance(usage, dict):
            self.input_tokens = usage.get("prompt_tokens", self.input_tokens)
            self.output_tokens = usage.get("completion_tokens", self.output_tokens)
            ptd = usage.get("prompt_tokens_details", {})
            if isinstance(ptd, dict):
                self.cached_tokens = ptd.get("cached_tokens", self.cached_tokens)

        # Emit message_start on first chunk
        out = self._maybe_send_meta()

        # OpenAI first chunk has role="assistant" + optional content
        # Anthropic already sent message_start, so just start content blocks

        # Handle text content
        content_str = delta.get("content", "")

        # Handle tool_calls
        tc_deltas = delta.get("tool_calls", [])
        needs_text_close = False

        if tc_deltas:
            for tc in tc_deltas:
                tc_idx = tc.get("index", 0)
                func = tc.get("function", {})

                if tc_idx not in self.tool_buffers:
                    # First chunk for this tool_call — has id + name
                    tc_id = tc.get("id", "")
                    name = func.get("name", "")
                    args_chunk = func.get("arguments", "")
                    if tc_id and name:
                        out += self._open_tool(tc_idx, tc_id, name)
                        if args_chunk:
                            out += self._tool_delta(tc_idx, args_chunk)
                    else:
                        # No id/name yet (some providers send these late) — buffer
                        self.tool_buffers[tc_idx] = {
                            "id": tc.get("id", ""),
                            "name": func.get("name", ""),
                            "args": func.get("arguments", ""),
                        }
                        if tc_idx not in self.open_tool_indices:
                            self.open_tool_indices.append(tc_idx)
                else:
                    args_chunk = func.get("arguments", "")
                    if args_chunk:
                        # Check if this is actually an open block (was opened via _open_tool)
                        if self.open_tool_indices and self.open_tool_indices[-1] == tc_idx:
                            out += self._tool_delta(tc_idx, args_chunk)
                        else:
                            # This tool section wasn't opened yet — need to handle specially
                            buf = self.tool_buffers[tc_idx]
                            buf["args"] += args_chunk

        else:
            # No tool_calls in this chunk — regular text content
            if content_str:
                if self.text_block_open:
                    out += self._emit_text_delta(content_str)
                else:
                    out += self._emit_text_start()
                    out += self._emit_text_delta(content_str)
            elif content_str == "" and not self.text_block_open and not self.open_tool_indices:
                # First chunk with empty content and no tool_calls — start empty text block
                out += self._emit_text_start()

        # Handle finish
        if finish:
            if finish == "stop":
                out += self._close_text()
                self.final_stop_reason = "end_turn"
            elif finish == "tool_calls":
                # Close any remaining text
                out += self._close_text()
                # Emit buffered tool blocks that weren't streamed
                for tc_idx in self.open_tool_indices:
                    buf = self.tool_buffers.get(tc_idx)
                    if buf and not self._is_block_open(tc_idx):
                        out += self._emit_tool_use_block(
                            tc_idx, buf["id"], buf["name"], buf["args"]
                        )
                out += self._close_tool_blocks()
                self.final_stop_reason = "tool_use"

            out += self._emit_message_delta()
            out += self._emit_message_stop()

        return out

    def _is_block_open(self, tc_idx: int) -> bool:
        """Check if a tool call at this index has an open content block."""
        return self.open_tool_indices and tc_idx in self.open_tool_indices


def random_id(length: int = 6) -> str:
    """Generate a short random hex string."""
    return hashlib.sha256(os.urandom(16)).hexdigest()[:length]


def _openai_to_anthropic_tool_id(openai_id: str) -> str:
    """Convert 'call_xxx' to 'tu_xxx' (Anthropic style)."""
    if openai_id.startswith("call_"):
        return "tu_" + openai_id[5:]
    return f"tu_{openai_id}"


# ── Proxy handler ──────────────────────────────────────────────────────────


class OpenRouterProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = urlsplit(DEFAULT_UPSTREAM)

    def _build_connection(self) -> http.client.HTTPConnection:
        if self.upstream.scheme == "https":
            return http.client.HTTPSConnection(
                self.upstream.hostname, self.upstream.port or 443, timeout=300
            )
        return http.client.HTTPConnection(
            self.upstream.hostname, self.upstream.port or 80, timeout=300
        )

    def _resolve_model(self, anthropic_model: str) -> str:
        return MODEL_MAP.get(anthropic_model, "deepseek/deepseek-v4-flash")

    def _reverse_model(self, openai_model: str) -> str:
        return _OPENAI_TO_ANTHROPIC_MODEL.get(openai_model, openai_model)

    # ── /v1/models ──────────────────────────────────────────────────────
    def _handle_models(self) -> None:
        models_data = {
            "data": [
                {
                    "id": k,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "openrouter",
                }
                for k in MODEL_MAP
            ]
        }
        body = json.dumps(models_data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    # ── POST /v1/messages (main endpoint) ────────────────────────────────────

    def _forward_to_deepseek(
        self, anthropic_body: bytes, anthropic_model: str, is_stream: bool
    ) -> None:
        """Fallback: forward the original Anthropic request to local DeepSeek proxy (:44777).

        Called when OpenRouter returns 5xx or connection fails.
        """
        print("[openrouter-proxy] fallback → DeepSeek proxy :44777", file=sys.stderr)
        conn = http.client.HTTPConnection("127.0.0.1", 44777, timeout=120)
        ds_key = os.environ.get("DEEPSEEK_API_KEY", "")
        ds_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ds_key}",
            "User-Agent": "devforge-proxy/1.0",
        }
        if is_stream:
            ds_headers["Accept"] = "text/event-stream"
        try:
            conn.request("POST", "/v1/messages", body=anthropic_body, headers=ds_headers)
            resp = conn.getresponse()
        except Exception as e:
            conn.close()
            print(f"[openrouter-proxy] DeepSeek fallback also failed: {e}", file=sys.stderr)
            err = json.dumps({"error": {"message": f"All upstreams failed: {e}"}}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
            return

        try:
            if is_stream:
                self._stream_response(resp, anthropic_model)
            else:
                self._nonstream_response(resp, anthropic_model)
        finally:
            conn.close()

    def _handle_messages(self, body: bytes) -> None:
        # Parse Anthropic request
        try:
            anthropic_req = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as e:
            err = json.dumps({"error": {"message": f"Malformed JSON: {e}"}}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
            return

        # Convert to OpenAI format
        try:
            openai_req = _anthropic_to_openai(anthropic_req)
        except Exception as e:
            err = json.dumps({"error": {"message": f"Conversion error: {e}"}}).encode("utf-8")
            print(f"[openrouter-proxy] conversion error: {e}", file=sys.stderr)
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
            return

        is_stream = openai_req.get("stream", False)
        anthropic_model = anthropic_req.get("model", "claude")

        # Forward to OpenRouter, trying each API key in order.
        # On 401/402/403 (auth/credit) → next key.
        # On 5xx or connection error → fallback to DeepSeek proxy.
        openai_body = json.dumps(openai_req, ensure_ascii=False).encode("utf-8")
        path = "/api/v1/chat/completions"

        for key_idx, api_key in enumerate(API_KEYS):
            key_label = f"key[{key_idx}]"
            conn = self._build_connection()
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "http://localhost",
                "X-Title": "devforge-proxy",
                "Accept": "text/event-stream" if is_stream else "application/json",
                "User-Agent": "devforge-proxy/1.0",
                "X-OpenRouter-Cache": "true",
            }

            try:
                conn.request("POST", path, body=openai_body, headers=headers)
                resp = conn.getresponse()
            except Exception as e:
                print(
                    f"[openrouter-proxy] {key_label} upstream connection failed: {e}",
                    file=sys.stderr,
                )
                conn.close()
                # Network failure → not a key problem, fall back to DeepSeek.
                self._forward_to_deepseek(body, anthropic_model, is_stream)
                return

            cache_state = resp.getheader("x-openrouter-cache-status", "UNKNOWN")
            cache_age = resp.getheader("x-openrouter-cache-age")
            if cache_age:
                print(f"[openrouter-proxy] {key_label} cache={cache_state} age={cache_age}s", file=sys.stderr)
            else:
                print(f"[openrouter-proxy] {key_label} cache={cache_state}", file=sys.stderr)

            # Key-level failure: rotate to next key if available.
            if resp.status in RETRY_KEY_STATUSES:
                err_body = resp.read()
                conn.close()
                has_next = key_idx + 1 < len(API_KEYS)
                print(
                    f"[openrouter-proxy] {key_label} OpenRouter {resp.status} "
                    f"({err_body[:120]!r}), "
                    + (f"rotating to key[{key_idx + 1}]" if has_next else "no more keys, falling back to DeepSeek"),
                    file=sys.stderr,
                )
                if has_next:
                    continue
                # No more keys → fall back to DeepSeek.
                self._forward_to_deepseek(body, anthropic_model, is_stream)
                return

            # OpenRouter 5xx → fallback to DeepSeek (not key-specific).
            if resp.status >= 500:
                raw = resp.read()
                conn.close()
                print(
                    f"[openrouter-proxy] {key_label} OpenRouter {resp.status}, falling back to DeepSeek",
                    file=sys.stderr,
                )
                self._forward_to_deepseek(body, anthropic_model, is_stream)
                return

            # Success path: stream or non-stream.
            try:
                if (
                    is_stream
                    and resp.getheader("transfer-encoding", "").lower() == "chunked"
                    or is_stream
                ):
                    self._stream_response(resp, anthropic_model)
                else:
                    self._nonstream_response(resp, anthropic_model)
            finally:
                conn.close()
            return

        # All keys exhausted (shouldn't reach here — last iteration falls back to DeepSeek).
        self._forward_to_deepseek(body, anthropic_model, is_stream)

    def _nonstream_response(self, resp: http.client.HTTPResponse, anthropic_model: str) -> None:
        """Handle non-streaming response: read full body, convert to Anthropic format."""
        raw = resp.read()
        if resp.status != 200:
            # Pass through error
            self.send_response(resp.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()
            return

        try:
            openai_resp = json.loads(raw)
            anthropic_resp = _openai_to_anthropic_nonstream(openai_resp, anthropic_model)
        except Exception as e:
            print(f"[openrouter-proxy] response conversion error: {e}", file=sys.stderr)
            err = json.dumps({"error": {"message": "Response conversion failed"}}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
            return

        body = json.dumps(anthropic_resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _stream_response(self, resp: http.client.HTTPResponse, anthropic_model: str) -> None:
        """Stream SSE: read OpenAI chunks, emit Anthropic events."""
        converter = _StreamConverter(anthropic_model)
        buffer = b""

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        while True:
            try:
                chunk = resp.read(4096)
            except Exception:
                break
            if not chunk:
                break

            buffer += chunk
            # Process complete lines
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                if line.startswith(b"data: "):
                    data_str = line[6:].decode("utf-8").strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    try:
                        events = converter.process_chunk(data)
                        if events:
                            ev_bytes = events.encode("utf-8")
                            size_hex = ("%x" % len(ev_bytes)).encode("ascii")
                            self.wfile.write(size_hex + b"\r\n" + ev_bytes + b"\r\n")
                            self.wfile.flush()
                    except Exception as e:
                        print(f"[openrouter-proxy] stream conversion error: {e}", file=sys.stderr)

        # If the converter didn't finish, send termination events
        if not converter.ended:
            term = b""
            if converter.text_block_open:
                term += f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': converter.block_idx})}\n\n".encode()
                converter.block_idx += 1
                converter.text_block_open = False
            # Close any open tool blocks
            if hasattr(converter, "open_tool_indices"):
                for _ in converter.open_tool_indices:
                    term += f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': converter.block_idx})}\n\n".encode()
                    converter.block_idx += 1
            ev = {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {
                    "output_tokens": converter.output_tokens,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": converter.cached_tokens,
                },
            }
            term += f"event: message_delta\ndata: {json.dumps(ev)}\n\n".encode()
            term += b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            if term:
                size_hex = ("%x" % len(term)).encode("ascii")
                self.wfile.write(size_hex + b"\r\n" + term + b"\r\n")

        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    # ── Routing ────────────────────────────────────────────────────────────
    def _route(self) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        if path == "/v1/models" or path == "/models":
            self._handle_models()
            return

        if self.command == "POST" and (path.endswith("/messages") or path.endswith("/v1/messages")):
            length = int(self.headers.get("content-length") or "0")
            body = self.rfile.read(length) if length > 0 else b""
            self._handle_messages(body)
            return

        # Unknown — 404
        err = json.dumps({"error": {"message": f"Not found: {self.command} {self.path}"}}).encode(
            "utf-8"
        )
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(err)))
        self.end_headers()
        self.wfile.write(err)

    def do_GET(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()

    def log_message(self, fmt: str, *args) -> None:
        print(f"[openrouter-proxy] {self.address_string()} - {fmt % args}", file=sys.stderr)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Anthropic→OpenRouter format-conversion proxy")
    parser.add_argument(
        "--listen", default=DEFAULT_LISTEN, help=f"Listen address (default: {DEFAULT_LISTEN})"
    )
    parser.add_argument(
        "--upstream",
        default=DEFAULT_UPSTREAM,
        help=f"OpenRouter endpoint (default: {DEFAULT_UPSTREAM})",
    )
    args = parser.parse_args()

    if not _resolve_api_key():
        print(
            "[openrouter-proxy] WARNING: OPENROUTER_MESIDS_API_KEY not set",
            file=sys.stderr,
        )
        print("[openrouter-proxy] Set keys in env (Azure KV)", file=sys.stderr)

    OpenRouterProxyHandler.upstream = urlsplit(args.upstream)
    host, port_str = args.listen.rsplit(":", 1)
    port = int(port_str)

    server = ThreadingHTTPServer((host, port), OpenRouterProxyHandler)
    print(f"[openrouter-proxy] listening on {host}:{port}", file=sys.stderr)
    print(f"[openrouter-proxy] upstream: {args.upstream}", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[openrouter-proxy] shutting down", file=sys.stderr)
        server.server_close()


if __name__ == "__main__":
    main()
