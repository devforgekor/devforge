#!/usr/bin/env python3.12
# Status: experimental
# Path: systemd:gemini-rr-proxy
"""Gemini OpenAI-compatible key round-robin proxy.

Forwards OpenAI-format /v1/chat/completions to Google's official
OpenAI-compatible endpoint, rotating API keys on 429.

Endpoints:
  POST /v1/chat/completions  — OpenAI-compatible chat (stream + non-stream)
  GET  /v1/models            — list models from Google
  GET  /health               — health check
"""

import logging
import os
import sys
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

sys.path.insert(0, "/opt/projects/server/scripts")
from lib.auth.key_loader import load_api_keys

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("GEMINI_RR_PROXY_PORT", "4431"))

GOOGLE_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
CHAT_ENDPOINT = f"{GOOGLE_OPENAI_BASE}/chat/completions"
MODELS_ENDPOINT = f"{GOOGLE_OPENAI_BASE}/models"


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
logger = logging.getLogger("gemini-rr-proxy")

client: httpx.AsyncClient = None  # type: ignore[assignment]
current_key_index = 0


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global client
    client = httpx.AsyncClient(timeout=300.0)
    yield
    await client.aclose()


app = FastAPI(title="Gemini RR Proxy", version="1.0.0", lifespan=_lifespan)


def _next_key() -> tuple[int, str]:
    global current_key_index
    idx = current_key_index
    current_key_index = (current_key_index + 1) % NUM_KEYS
    return idx, KEYS[idx]


async def _forward_key(idx: int, body: dict, is_stream: bool, model: str):
    api_key = KEYS[idx]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    label = ACCOUNT_LABELS[idx] if idx < len(ACCOUNT_LABELS) else f"key{idx + 1}"
    logger.info("→ key[%d/%d] %s model=%s stream=%s", idx + 1, NUM_KEYS, label, model, is_stream)

    if client is None:
        raise HTTPException(status_code=503, detail="Proxy not ready")

    try:
        if is_stream:
            req = client.build_request("POST", CHAT_ENDPOINT, json=body, headers=headers)
            resp = await client.send(req, stream=True)
            if resp.status_code == 200:
                logger.info("✓ key[%d] %s streaming", idx + 1, label)
                return StreamingResponse(
                    _stream_chunks(resp),
                    media_type="text/event-stream",
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
        else:
            resp = await client.post(CHAT_ENDPOINT, json=body, headers=headers)
            if resp.status_code == 200:
                logger.info("✓ key[%d] %s done", idx + 1, label)
                return JSONResponse(content=resp.json(), status_code=200)
            err_body = resp.text
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


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    is_stream = body.get("stream", False)
    model = body.get("model", "unknown")

    start_idx, _ = _next_key()
    order = [(start_idx + offset) % NUM_KEYS for offset in range(NUM_KEYS)]

    last_error = "All API keys failed."
    for idx in order:
        try:
            return await _forward_key(idx, body, is_stream, model)
        except HTTPException as e:
            last_error = e.detail
            continue

    logger.error("✗ All %d keys failed for %s: %s", len(order), model, last_error)
    raise HTTPException(status_code=502, detail=last_error)


@app.get("/v1/models")
async def list_models():
    if client is None:
        raise HTTPException(status_code=503, detail="Proxy not ready")
    headers = {"Authorization": f"Bearer {KEYS[0]}"}
    try:
        resp = await client.get(MODELS_ENDPOINT, headers=headers)
        resp.raise_for_status()
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/health")
async def health():
    return {"status": "ok", "keys": NUM_KEYS, "accounts": ACCOUNT_LABELS}


if __name__ == "__main__":
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
