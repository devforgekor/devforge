#!/usr/bin/env python3
"""Anthropic-compatible reverse proxy for DeepSeek.

Rewrites system-role messages into the top-level system field before forwarding
requests to DeepSeek's Anthropic-compatible endpoint.
"""

import argparse
import http.client
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple
from urllib.parse import urlsplit
import time
import socket

DEFAULT_LISTEN = "127.0.0.1:44777"
DEFAULT_UPSTREAM = "https://api.deepseek.com/anthropic"
# Model name mapping: map Anthropic/DeepSeek model names to local llama model names
MODEL_MAP = {
    "deepseek-chat": "qwen3-30b-a3b",
    "deepseek-v4-pro": "qwen3-30b-a3b",
    "deepseek-v4-flash": "qwen3-30b-a3b",
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
    if merged_system:
        updated["system"] = merged_system
    elif "system" in updated:
        updated.pop("system", None)
    return updated, True


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = urlsplit(DEFAULT_UPSTREAM)

    def _forward(self) -> None:
        body = b""
        if self.command in {"POST", "PUT", "PATCH"}:
            length = int(self.headers.get("content-length") or "0")
            body = self.rfile.read(length) if length > 0 else b""

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
            and key.lower() not in {"host", "content-length"}
        }

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
                payload, system_rewritten = _sanitize_messages(payload)
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

                    if isinstance(m, str) and m in MODEL_MAP:
                        new_m = MODEL_MAP[m]
                        payload["model"] = new_m
                        print(f"[anthropic_proxy] remapped model {m} -> {new_m}", file=sys.stderr)
                # Always re-serialize for consistent JSON formatting (compact, no spaces)
                # to ensure DeepSeek's automatic prefix caching sees identical byte prefixes.
                body = json.dumps(payload, ensure_ascii=False, separators=(",",":"))
                body = body.encode("utf-8")
                if cache_stripped:
                    print(f"[anthropic_proxy] stripped cache_control from request", file=sys.stderr)
                if system_rewritten:
                    print(f"[anthropic_proxy] moved system-role messages to top-level system field", file=sys.stderr)
                # DEBUG dump when enabled via ANTHROPIC_PROXY_DEBUG=1
                if os.environ.get("ANTHROPIC_PROXY_DEBUG", "0") == "1":
                    try:
                        ts = int(time.time() * 1000)
                        dump_base = f"/tmp/anthropic_debug_{ts}"
                        # write the forwarded body
                        with open(dump_base + "_forward.json", "wb") as fwd:
                            if isinstance(body, (bytes, bytearray)):
                                fwd.write(body)
                            else:
                                fwd.write(json.dumps(body, ensure_ascii=False).encode("utf-8"))
                        # header dump
                        try:
                            with open(dump_base + "_headers.json", "w", encoding="utf-8") as hf:
                                json.dump(headers, hf, ensure_ascii=False, indent=2)
                        except Exception:
                            pass
                        print(f"[anthropic_proxy] wrote debug dumps {dump_base}_*.json", file=sys.stderr)
                    except Exception as e:
                        print(f"[anthropic_proxy] debug dump failed: {e}", file=sys.stderr)

        # Choose HTTP vs HTTPS connection based on upstream scheme
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
        # Normalize and map incoming path (preserve querystring)
        # Handle cases like: /anthropic/v1/messages, /anthropic/v1/chat/completions,
        # /v1/chat/completions, and clients that accidentally prefix twice (/anthropic/anthropic/...)
        parsed = urlsplit(self.path)
        path_only = parsed.path
        query = parsed.query

        # Collapse duplicate /anthropic/anthropic prefixes
        while path_only.startswith("/anthropic/anthropic"):
            path_only = path_only.replace("/anthropic/anthropic", "/anthropic", 1)

        # If client used /anthropic/v1/*, remove the leading /anthropic to map to upstream /v1/*
        if path_only.startswith("/anthropic/v1"):
            path_only = path_only.replace("/anthropic", "", 1)

        # Normalize legacy 'messages' endpoint to '/chat/completions'
        if path_only.endswith("/messages"):
            path_only = path_only[: -len("/messages")] + "/chat/completions"

        # Ensure path starts with /v1 for upstream
        if not path_only.startswith("/v1") and path_only.startswith("/anthropic/v1"):
            # fallback safety: strip /anthropic prefix
            path_only = path_only.replace("/anthropic", "", 1)

        # Reattach query if present
        path = path_only + ("?" + query if query else "")

        try:
            conn.request(self.command, path, body=body if body else None, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status, resp.reason)

            # Forward headers; allow Transfer-Encoding to pass through for streaming
            resp_headers = resp.getheaders()
            is_chunked = False
            for key, value in resp_headers:
                lk = key.lower()
                if lk in HOP_BY_HOP_HEADERS and lk != "transfer-encoding":
                    continue
                if lk == "content-length":
                    # we'll set Content-Length only for non-streaming responses
                    continue
                if lk == "transfer-encoding":
                    if "chunked" in value.lower():
                        is_chunked = True
                    # forward transfer-encoding header so client can handle streaming
                    self.send_header(key, value)
                    continue
                self.send_header(key, value)

            self.end_headers()

            if is_chunked:
                # Stream response body in chunks as they arrive with idle timeout handling
                chunk_size = int(os.environ.get("ANTHROPIC_PROXY_STREAM_CHUNK", "4096"))
                idle_timeout = float(os.environ.get("ANTHROPIC_PROXY_STREAM_TIMEOUT", "10"))
                # Try to set underlying socket timeout so reads don't block forever
                try:
                    if hasattr(conn, "sock") and conn.sock:
                        conn.sock.settimeout(idle_timeout)
                except Exception:
                    pass
                last_read = time.time()
                try:
                    while True:
                        try:
                            chunk = resp.read(chunk_size)
                        except socket.timeout:
                            # if we've been idle longer than idle_timeout, stop streaming
                            if time.time() - last_read > idle_timeout:
                                print(f"[anthropic_proxy] stream idle timeout after {idle_timeout}s", file=sys.stderr)
                                break
                            else:
                                continue
                        if not chunk:
                            break
                        last_read = time.time()
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except BrokenPipeError:
                            break
                except Exception:
                    pass
                # No Content-Length for chunked responses
                data = b""
            else:
                data = resp.read()
                try:
                    self.send_header("Content-Length", str(len(data)))
                except Exception:
                    pass
                if data:
                    try:
                        self.wfile.write(data)
                        self.wfile.flush()
                    except BrokenPipeError:
                        pass

            # Log usage for cache monitoring (only when body contains JSON)
            if body and resp.status == 200 and data:
                try:
                    r = json.loads(data.decode("utf-8"))
                    u = r.get("usage", {})
                    input_tokens = u.get("input_tokens", 0)
                    cache_read = u.get("cache_read_input_tokens", 0)
                    cache_create = u.get("cache_creation_input_tokens", 0)
                    output_tokens = u.get("output_tokens", 0)
                    body_kb = len(body) / 1024
                    denom = input_tokens + cache_read
                    pct = min((cache_read / denom * 100), 100.0) if denom > 0 else 0
                    print(f"[anthropic_proxy] usage: input={input_tokens} cache_read={cache_read} "
                          f"cache_create={cache_create} output={output_tokens} "
                          f"hit_rate={pct:.0f}% body={body_kb:.0f}KB", file=sys.stderr)
                except Exception:
                    pass
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
    server = ThreadingHTTPServer((host, port), ProxyHandler)
    print(f"[anthropic_proxy] listening on {host}:{port}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
