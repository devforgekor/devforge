"""Unified LLM client — single call_llm() for all OpenAI-compatible endpoints.

Used by: code_mod_pipeline.py, review_worker.py (via adapter), hybrid_pipeline.py,
         activity_summarizer.py.
"""
import http.client
import json
import socket
from typing import Tuple


def _enable_keepalive(sock: socket.socket) -> None:
    """Enable aggressive TCP keepalive to prevent idle connection drops by pasta/podman.
    Probes start at 10s idle, every 10s — keeps connection alive during long prompt eval."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 127)
    except (OSError, AttributeError):
        pass


def call_llm(endpoint: str, messages: list, api_key: str = "",
             model: str = "", timeout: int = 1200, max_tokens: int = 2048) -> Tuple[int, dict]:
    """Call any OpenAI-compatible chat completions endpoint. Returns (status, body).

    Uses TCP keepalive to prevent pasta/podman from dropping idle connections
    during long prompt evaluation (32B on ARM CPU: ~1.87 tok/s).
    """
    body = {"messages": messages, "temperature": 0.1, "max_tokens": max_tokens}
    if model:
        body["model"] = model

    data = json.dumps(body).encode()
    u = endpoint
    if u.endswith("/"):
        u = u[:-1]
    path = "/v1/chat/completions"
    base = u
    if u.endswith("/v1/chat/completions"):
        base = u[: -len(path)]
        path = "/v1/chat/completions"

    host = base.replace("https://", "").replace("http://", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        if endpoint.startswith("https://"):
            import ssl
            conn = http.client.HTTPSConnection(
                host, timeout=timeout,
                context=ssl.create_default_context()
            )
        else:
            conn = http.client.HTTPConnection(host, timeout=timeout)

        conn.connect()
        _enable_keepalive(conn.sock)
        conn.request("POST", path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
        if status == 200:
            return status, json.loads(raw)
        return status, {"error": raw.decode()[:500]}
    except Exception as e:
        return 0, {"error": str(e)}
    finally:
        conn.close()
