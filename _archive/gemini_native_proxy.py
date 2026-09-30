#!/usr/bin/env python3.12
# Status: experimental
# Path: systemd:gemini-native-proxy
"""Gemini native API key round-robin proxy.

Pure pass-through: forwards all /v1beta/* requests to Google's native
Gemini API, rotating the x-goog-api-key header on 429.

Endpoints:
  ANY /v1beta/*  — native Gemini API (generateContent, streamGenerateContent, etc.)
  GET /health     — health check
"""

import logging
import os
import sys
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

sys.path.insert(0, "/opt/projects/server/scripts")
from lib.auth.key_loader import load_api_keys

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("GEMINI_NATIVE_PROXY_PORT", "4432"))

GOOGLE_NATIVE_BASE = "https://generativelanguage.googleapis.com"


def _load_keys() -> list[str]:
    return [k for _, k in load_api_keys("GEMINI")]


def _load_labels() -> list[str]:
    return [name for name, _ in load_api_keys("GEMINI")]


KEYS = _load_keys()
if len(KEYS) < 2:
    print(f"ERROR: Need at least 2 Gemini API keys, found {len(KEYS)}", flush=True)
    raise SystemExit(1)

NUM_KEYS = len(KEYS)
ACCOUNT_LABELS = _load_labels()[:NUM_KEYS]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("gemini-native-proxy")

client: httpx.AsyncClient = None  # type: ignore[assignment]
current_key_index = 0


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global client
    client = httpx.AsyncClient(timeout=300.0)
    yield
    await client.aclose()


app = FastAPI(title="Gemini Native Proxy", version="1.0.0", lifespan=_lifespan)


def _next_key() -> tuple[int, str]:
    global current_key_index
    idx = current_key_index
    current_key_index = (current_key_index + 1) % NUM_KEYS
    return idx, KEYS[idx]


async def _forward_key(idx: int, method: str, path: str, query: str, body: bytes, headers: dict):
    api_key = KEYS[idx]
    label = ACCOUNT_LABELS[idx] if idx < len(ACCOUNT_LABELS) else f"key{idx + 1}"

    fwd_headers = {k: v for k, v in headers.items() if k.lower() not in ("host", "x-goog-api-key", "content-length")}
    fwd_headers["x-goog-api-key"] = api_key

    url = f"{GOOGLE_NATIVE_BASE}{path}"
    if query:
        url = f"{url}?{query}"

    logger.info("→ key[%d/%d] %s %s %s", idx + 1, NUM_KEYS, label, method, path)

    if client is None:
        raise HTTPException(status_code=503, detail="Proxy not ready")

    try:
        req = client.build_request(method, url, content=body, headers=fwd_headers)
        resp = await client.send(req, stream=True)

        if resp.status_code == 200:
            logger.info("✓ key[%d] %s %s done", idx + 1, label, path)
            return StreamingResponse(
                _stream_chunks(resp),
                media_type=resp.headers.get("content-type", "application/json"),
                headers={
                    k: v for k, v in resp.headers.items()
                    if k.lower() in ("content-type", "content-encoding", "cache-control")
                },
            )

        try:
            err_body = (await resp.aread()).decode(errors="replace")
        finally:
            await resp.aclose()
        raise HTTPException(status_code=resp.status_code, detail=f"key[{idx + 1}] {label}: {err_body}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"RequestError key[{idx + 1}] {label}: {e.__class__.__name__}")


async def _stream_chunks(response: httpx.Response):
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    except Exception as e:
        logger.error("Stream error: %s", e)
    finally:
        await response.aclose()


@app.api_route("/v1beta/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_native(path: str, request: Request):
    body = await request.body()
    query = str(request.url.query)

    start_idx, _ = _next_key()
    order = [(start_idx + offset) % NUM_KEYS for offset in range(NUM_KEYS)]

    last_error = "All API keys failed."
    for idx in order:
        try:
            return await _forward_key(idx, request.method, f"/v1beta/{path}", query, body, dict(request.headers))
        except HTTPException as e:
            last_error = e.detail
            continue

    logger.error("✗ All %d keys failed for /v1beta/%s: %s", len(order), path, last_error)
    raise HTTPException(status_code=502, detail=last_error)


@app.get("/health")
async def health():
    return {"status": "ok", "keys": NUM_KEYS, "accounts": ACCOUNT_LABELS}


if __name__ == "__main__":
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
