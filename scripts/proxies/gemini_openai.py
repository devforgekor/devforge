#!/usr/bin/env python3.11
# Status: experimental
# Path: systemd:gemini-openai-proxy
"""
OpenAI-compatible reverse proxy for Google Gemini API with key rotation.

Translates OpenAI /v1/chat/completions requests to Gemini native format,
rotating API keys via KeyRotator. Independent state from proxies/gemini.py.

Bypasses /etc/hosts by connecting to hardcoded Google IPs (same pattern as
proxies/gemini.py), using asyncio + ssl for proper SNI.
"""

import asyncio
import json
import os
import ssl
import sys
import time
import uuid
from typing import AsyncGenerator, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from lib.auth.key_rotator import KeyRotator
from lib.auth.key_loader import load_api_keys

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("GEMINI_OPENAI_PROXY_PORT", "4431"))
GEMINI_HOST = "generativelanguage.googleapis.com"
GEMINI_PORT = 443
STATE_FILE = os.path.expanduser("~/.cache/devforge/gemini_openai_rotator_state.json")

# Hardcoded IPs bypassing /etc/hosts redirect (same as proxies/gemini.py)
GEMINI_REAL_IPS = [
    "142.250.21.95",
    "142.250.23.95",
    "142.251.24.95",
    "142.251.23.95",
]

SUPPORTED_MODELS = frozenset({
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash-lite",
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
})

_rotator: Optional[KeyRotator] = None
_lock: Optional[asyncio.Lock] = None

app = FastAPI(title="Gemini OpenAI Proxy", version="1.0.0")


# ---------------------------------------------------------------------------
# Rotator
# ---------------------------------------------------------------------------

def _get_rotator() -> KeyRotator:
    global _rotator
    if _rotator is None:
        keys = load_api_keys()
        if not keys:
            raise RuntimeError("No Gemini API keys found")
        _rotator = KeyRotator(keys, state_file=STATE_FILE)
    return _rotator


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


# ---------------------------------------------------------------------------
# Raw async HTTP/1.1 over SSL (bypass /etc/hosts)
# ---------------------------------------------------------------------------

def _pick_ip() -> str:
    """Return a Google IP, cycling through the list."""
    return GEMINI_REAL_IPS[int(time.time()) % len(GEMINI_REAL_IPS)]


async def _connect_gemini() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open raw SSL connection to Gemini API, bypassing /etc/hosts."""
    ctx = ssl.create_default_context()
    ip = _pick_ip()
    reader, writer = await asyncio.open_connection(
        ip, GEMINI_PORT,
        ssl=ctx,
        server_hostname=GEMINI_HOST,
    )
    return reader, writer


async def _send_request(
    writer: asyncio.StreamWriter,
    method: str,
    path: str,
    headers: dict[str, str],
    body: Optional[bytes] = None,
):
    """Send raw HTTP/1.1 request."""
    lines = [f"{method} {path} HTTP/1.1"]
    lines.append(f"Host: {GEMINI_HOST}")
    for k, v in headers.items():
        lines.append(f"{k}: {v}")
    lines.append("Connection: close")
    req = "\r\n".join(lines) + "\r\n\r\n"
    writer.write(req.encode())
    if body:
        writer.write(body)
    await writer.drain()


async def _read_response_headers(
    reader: asyncio.StreamReader,
) -> tuple[int, dict[str, str], bool, int]:
    """Read HTTP status line and headers. Returns (status, headers_dict, is_chunked, content_length)."""
    status_line = await reader.readline()
    parts = status_line.decode(errors="replace").strip().split(" ", 2)
    status = int(parts[1]) if len(parts) >= 2 else 502

    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        decoded = line.decode(errors="replace").strip()
        if ":" in decoded:
            k, v = decoded.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    is_chunked = headers.get("transfer-encoding", "") == "chunked"
    content_length = int(headers.get("content-length", 0)) if "content-length" in headers else -1
    return status, headers, is_chunked, content_length


async def _read_body(
    reader: asyncio.StreamReader,
    is_chunked: bool,
    content_length: int,
) -> bytes:
    """Read full response body."""
    body = b""
    if is_chunked:
        while True:
            size_line = await reader.readline()
            if not size_line:
                break
            size_str = size_line.strip()
            if not size_str:
                continue
            try:
                chunk_size = int(size_str, 16)
            except ValueError:
                break
            if chunk_size == 0:
                await reader.readline()  # trailing CRLF
                break
            chunk = await reader.readexactly(chunk_size)
            body += chunk
            await reader.readline()  # trailing CRLF
    elif content_length >= 0:
        body = await reader.readexactly(content_length)
    else:
        # Read until close
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            body += chunk
    return body


async def _gemini_request(
    method: str,
    path: str,
    headers: dict[str, str],
    body: Optional[bytes] = None,
) -> tuple[int, dict[str, str], bytes]:
    """Make a Gemini API request and return (status, headers, body_bytes)."""
    reader, writer = await _connect_gemini()
    try:
        await _send_request(writer, method, path, headers, body)
        status, resp_headers, is_chunked, content_length = await _read_response_headers(reader)
        resp_body = await _read_body(reader, is_chunked, content_length)
        return status, resp_headers, resp_body
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _gemini_stream(
    method: str,
    path: str,
    headers: dict[str, str],
    body: Optional[bytes] = None,
) -> AsyncGenerator[bytes, None]:
    """Stream Gemini API response, yielding raw HTTP chunks (dechunked)."""
    reader, writer = await _connect_gemini()
    try:
        await _send_request(writer, method, path, headers, body)
        status, resp_headers, is_chunked, content_length = await _read_response_headers(reader)

        if status != 200:
            err_body = await _read_body(reader, is_chunked, content_length)
            yield json.dumps({"status": status, "body": err_body.decode(errors="replace")}).encode()
            return

        if is_chunked:
            while True:
                size_line = await reader.readline()
                if not size_line:
                    break
                size_str = size_line.strip()
                if not size_str:
                    continue
                try:
                    chunk_size = int(size_str, 16)
                except ValueError:
                    break
                if chunk_size == 0:
                    await reader.readline()
                    break
                chunk = await reader.readexactly(chunk_size)
                yield chunk
                await reader.readline()
        elif content_length >= 0:
            remaining = content_length
            while remaining > 0:
                chunk = await reader.read(min(65536, remaining))
                if not chunk:
                    break
                yield chunk
                remaining -= len(chunk)
        else:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                yield chunk
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Format conversion: OpenAI → Gemini
# ---------------------------------------------------------------------------

def _convert_messages(messages: list) -> tuple[list, Optional[dict]]:
    """OpenAI messages → Gemini contents + optional systemInstruction."""
    system_parts: list[dict] = []
    contents: list[dict] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            if content:
                system_parts.append({"text": content})
            continue

        gemini_role = "model" if role == "assistant" else "user"
        parts: list[dict] = []

        if content:
            if isinstance(content, str):
                parts.append({"text": content})
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "text":
                        parts.append({"text": block.get("text", "")})
                    elif btype == "image_url":
                        url = (block.get("image_url") or {}).get("url", "")
                        if url.startswith("data:image/"):
                            fmt = url.split(";")[0][5:]
                            data = url.split(",", 1)[-1]
                            parts.append({"inlineData": {"mimeType": fmt, "data": data}})

        contents.append({"role": gemini_role, "parts": parts})

    system_instruction = {"parts": system_parts} if system_parts else None
    return contents, system_instruction


def _clean_model(model: str) -> str:
    """Strip provider prefix like 'openai/gemini-2.5-flash'."""
    if "/" in model:
        model = model.rsplit("/", 1)[-1]
    return model if model in SUPPORTED_MODELS else "gemini-2.5-flash"


def _build_gemini_body(body: dict, contents: list, system_instruction: Optional[dict]) -> dict:
    """Build Gemini API request body."""
    gbody: dict = {"contents": contents}
    if system_instruction:
        gbody["systemInstruction"] = system_instruction

    config: dict = {}
    if max_t := body.get("max_tokens") or body.get("maxTokens"):
        config["maxOutputTokens"] = max_t
    if (t := body.get("temperature")) is not None:
        config["temperature"] = t
    if (p := body.get("top_p")) is not None:
        config["topP"] = p
    if (s := body.get("stop")):
        config["stopSequences"] = s if isinstance(s, list) else [s]
    if config:
        gbody["generationConfig"] = config

    return gbody


def _gemini_chunk_to_openai(chunk: dict, model: str) -> Optional[dict]:
    """Gemini SSE event → OpenAI streaming chunk."""
    candidates = chunk.get("candidates", [])
    if not candidates:
        return None
    c = candidates[0]
    text = "".join(
        p.get("text", "")
        for p in (c.get("content") or {}).get("parts", [])
    )
    fr = c.get("finishReason")
    finish = None
    if fr == "STOP":
        finish = "stop"
    elif fr == "MAX_TOKENS":
        finish = "length"
    elif fr:
        finish = "stop"

    return {
        "choices": [
            {
                "delta": {"content": text} if text else {},
                "index": 0,
                "finish_reason": finish,
            }
        ],
        "created": int(time.time()),
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "model": model,
        "object": "chat.completion.chunk",
    }


# ---------------------------------------------------------------------------
# SSE streaming — parse raw bytes, yield converted OpenAI chunks
# ---------------------------------------------------------------------------

async def _parse_sse_stream(
    byte_stream: AsyncGenerator[bytes, None],
    model: str,
) -> AsyncGenerator[str, None]:
    """Parse Gemini SSE stream and yield OpenAI-format SSE lines."""
    buf = b""
    async for chunk in byte_stream:
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            decoded = line.decode(errors="replace").strip()
            if not decoded.startswith("data: "):
                continue
            raw = decoded[6:].strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            oai = _gemini_chunk_to_openai(data, model)
            if oai:
                yield f"data: {json.dumps(oai, ensure_ascii=False)}\n\n"
                if oai["choices"][0].get("finish_reason"):
                    return
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": int(time.time()), "owned_by": "google"}
            for m in sorted(SUPPORTED_MODELS)
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    messages = body.get("messages", [])
    if not messages:
        return JSONResponse(status_code=400, content={"error": "messages is required"})

    model = _clean_model(body.get("model", "gemini-2.5-flash"))
    stream = body.get("stream", False)

    contents, system_instruction = _convert_messages(messages)
    gemini_body = _build_gemini_body(body, contents, system_instruction)
    payload = json.dumps(gemini_body).encode()

    # ---- Pick API key ----
    lock = _get_lock()
    async with lock:
        try:
            rotator = _get_rotator()
        except RuntimeError as e:
            return JSONResponse(status_code=503, content={"error": str(e)})
        picked = rotator.pick()
        if picked is None:
            return JSONResponse(
                status_code=503,
                content={"error": {"message": "All Gemini API keys exhausted or in backoff.", "type": "rate_limit_error"}},
            )
        key_idx, key_name, api_key = picked

    path = f"/v1beta/models/{model}:generateContent"
    stream_path = f"/v1beta/models/{model}:streamGenerateContent?alt=sse"

    if stream:
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json", "Content-Length": str(len(payload))}
        byte_gen = _gemini_stream("POST", stream_path, headers, payload)

        return StreamingResponse(
            _parse_sse_stream(byte_gen, model),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    # ---- Non-streaming ----
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json", "Content-Length": str(len(payload))}
    status_code, resp_headers, resp_body = await _gemini_request("POST", path, headers, payload)

    async with lock:
        if status_code == 429:
            rotator.rate_limited(key_idx, 60)
            return JSONResponse(
                status_code=429,
                content={"error": {"message": "Rate limited. Key rotated.", "type": "rate_limit_error"}},
            )
        if status_code < 500:
            rotator.success(key_idx)

    if status_code != 200:
        try:
            err = json.loads(resp_body)
        except Exception:
            err = {"message": resp_body.decode(errors="replace")}
        return JSONResponse(status_code=status_code, content=err)

    data = json.loads(resp_body)
    text = ""
    for c in data.get("candidates", []):
        for p in (c.get("content") or {}).get("parts", []):
            text += p.get("text", "")

    finish = "stop"
    if data.get("candidates") and data["candidates"][0].get("finishReason") == "MAX_TOKENS":
        finish = "length"

    usage = data.get("usageMetadata", {})
    openai_usage = {
        "prompt_tokens": usage.get("promptTokenCount", 0),
        "completion_tokens": usage.get("candidatesTokenCount", 0),
        "total_tokens": usage.get("totalTokenCount", 0),
    } if usage else None

    return JSONResponse(content={
        "choices": [
            {
                "finish_reason": finish,
                "index": 0,
                "message": {"content": text, "role": "assistant"},
            }
        ],
        "created": int(time.time()),
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "model": model,
        "object": "chat.completion",
        "usage": openai_usage,
    })


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    uvicorn.run(
        "proxies.gemini_openai:app",
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        log_level=os.environ.get("GEMINI_OPENAI_PROXY_LOG_LEVEL", "info").lower(),
        access_log=True,
        reload=False,
    )


if __name__ == "__main__":
    main()
