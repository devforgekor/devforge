#!/usr/bin/env python3
"""
HTTPS reverse proxy that rotates Gemini API keys per request.

Listens on a local port with a self-signed cert for generativelanguage.googleapis.com.
Each incoming request gets a fresh API key picked via KeyRotator.
"""

import json
import os
import socket
import ssl
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from urllib.request import Request, urlopen

sys.path.insert(0, "/opt/projects/server")
from lib.key_rotator import KeyRotator
from scripts.gemini_rotate import _load_keys, STATE_FILE

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 4430
REAL_HOST = "generativelanguage.googleapis.com"
REAL_PORT = 443

CERT_FILE = os.path.expanduser("~/.local/share/devforge/certs/proxy-cert.pem")
KEY_FILE = os.path.expanduser("~/.local/share/devforge/certs/proxy-key.pem")

_rotator: Optional[KeyRotator] = None
_lock = threading.Lock()


def _get_rotator() -> KeyRotator:
    global _rotator
    if _rotator is not None:
        return _rotator

    keys = _load_keys()
    if not keys:
        raise RuntimeError("No API keys found")

    rotator = KeyRotator(keys)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
            rotator._calls = {int(k): v for k, v in s.get("calls", {}).items()}
            rotator._fails = {int(k): v for k, v in s.get("fails", {}).items()}
            rotator._last_used = {int(k): v for k, v in s.get("last_used", {}).items()}
            rotator._backoff_until = {int(k): v for k, v in s.get("backoff_until", {}).items()}
        except Exception:
            pass

    _rotator = rotator
    return rotator


def _save_state(rotator: KeyRotator):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state = {
        "calls": rotator._calls,
        "fails": rotator._fails,
        "last_used": rotator._last_used,
        "backoff_until": rotator._backoff_until,
    }
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(state, f)
    os.rename(STATE_FILE + ".tmp", STATE_FILE)


REAL_IPS = ["142.250.21.95", "142.250.23.95", "142.251.24.95", "142.251.23.95"]
CURRENT_REAL_IP = REAL_IPS[0]


def _resolve_real_ip() -> str:
    """Return the real Google API IP, bypassing any hosts file redirect."""
    return CURRENT_REAL_IP


class ProxyHandler(BaseHTTPRequestHandler):
    def _pick_key(self):
        with _lock:
            rotator = _get_rotator()
            picked = rotator.pick()
            if picked is None:
                return None, None, None
            idx, name, key = picked
            _save_state(rotator)
            return name, key, idx

    def _forward(self, method):
        name, key, idx = self._pick_key()
        if key is None:
            self.send_error(503, "No API keys available")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else None

        path = self.path
        headers = dict(self.headers)
        headers.pop("Host", None)
        headers["x-goog-api-key"] = key

        real_ip = _resolve_real_ip()

        # Connect directly to IP to avoid hosts file redirect
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        context = ssl.create_default_context()
        wrapped = context.wrap_socket(sock, server_hostname=REAL_HOST)
        wrapped.connect((real_ip, REAL_PORT))

        # Build and send request
        header_lines = [f"{method} {path} HTTP/1.1"]
        header_lines.append(f"Host: {REAL_HOST}")
        for k, v in headers.items():
            if k.lower() not in ("host",):
                header_lines.append(f"{k}: {v}")
        header_lines.append("Connection: close")
        req = "\r\n".join(header_lines) + "\r\n\r\n"
        wrapped.sendall(req.encode())
        if body:
            wrapped.sendall(body)

        # Read response
        wrapped.settimeout(30)
        response_data = b""
        try:
            while True:
                chunk = wrapped.recv(65536)
                if not chunk:
                    break
                response_data += chunk
        except socket.timeout:
            pass
        finally:
            wrapped.close()

        # Parse response status and headers
        header_end = response_data.find(b"\r\n\r\n")
        if header_end == -1:
            self.send_error(502, "Invalid upstream response")
            return
        raw_headers = response_data[:header_end]
        raw_body = response_data[header_end + 4:]

        status_line, *rest = raw_headers.decode(errors="replace").split("\r\n")
        status_code = int(status_line.split(" ")[1])

        # Dechunk if needed
        is_chunked = False
        for line in rest:
            if ":" in line:
                k, v = line.split(":", 1)
                if k.lower() == "transfer-encoding" and "chunked" in v.lower():
                    is_chunked = True

        if is_chunked:
            resp_body = b""
            pos = 0
            while pos < len(raw_body):
                chunk_end = raw_body.find(b"\r\n", pos)
                if chunk_end == -1:
                    break
                try:
                    chunk_size = int(raw_body[pos:chunk_end], 16)
                except ValueError:
                    break
                if chunk_size == 0:
                    break
                pos = chunk_end + 2
                resp_body += raw_body[pos:pos + chunk_size]
                pos += chunk_size + 2
        else:
            resp_body = raw_body

        # send_response must come BEFORE send_header to ensure
        # the HTTP status line is written first in the buffer
        self.send_response(status_code)
        for line in rest:
            if ":" in line:
                k, v = line.split(":", 1)
                if k.lower() not in ("transfer-encoding", "connection"):
                    self.send_header(k.strip(), v.strip())
        self.end_headers()

        if resp_body:
            self.wfile.write(resp_body)

        # Record stats
        with _lock:
            rotator = _get_rotator()
            if status_code in (200, 201):
                rotator.success(idx)
            elif status_code == 429:
                rotator.rate_limited(idx, 60)
            _save_state(rotator)

        print(f"[proxy] {name} → {status_code} {self.path[:60]}", file=sys.stderr)

    def do_POST(self):
        self._forward("POST")

    def do_GET(self):
        self._forward("GET")

    def do_PUT(self):
        self._forward("PUT")

    def do_DELETE(self):
        self._forward("DELETE")

    def do_PATCH(self):
        self._forward("PATCH")

    def log_message(self, format, *args):
        pass  # suppress default logging


def main():
    import signal

    def shutdown(signum, frame):
        print("\n[proxy] shutting down...", file=sys.stderr)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERT_FILE, KEY_FILE)

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    server.socket = context.wrap_socket(server.socket, server_side=True)

    print(f"[proxy] listening on {LISTEN_HOST}:{LISTEN_PORT}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
