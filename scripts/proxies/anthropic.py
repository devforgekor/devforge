#!/usr/bin/env python3
# Status: production
# Path: systemd:devforge-pod-a (proxied port)
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
from lib.proxy_utils import (
    HOP_BY_HOP_HEADERS,
    _flatten_text,
    _strip_cache_control,
    _strip_system_billing_header,
    _flatten_system_blocks,
    _sanitize_messages,
    _fix_orphan_tool_results,
    _collect_referenced_tool_use_ids,
    _collect_tool_use_ids_present,
    _cleanup,
    _remove_adjacent_orphans,
    _strict_tool_adjacency_fix,
    _message_has_nonempty_content,
    _apply_cache_padding,
    _json_dumps_system_first,
    normalize_proxy_path,
    filter_response_headers,
    _extract_stream_usage,
    log_usage,
)


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
                        request_ms = int(time.time() * 1000)
                        dump_base = f"/tmp/anthropic_debug_{request_ms}"
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
                    request_ms = int(time.time() * 1000)
                    dump_path = f"/tmp/anthropic_4xx_{request_ms}.json"
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
