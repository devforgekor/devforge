#!/usr/bin/env python3.11
# Status: experimental
# Path: systemd:openrouter-rr-proxy.service
"""
OpenRouter API Key Round-Robin Proxy.

Rotates through 3 OpenRouter API keys per request (round-robin).
Minimal, stateless proxy — only RPM avoidance, no cooldown/state tracking.

Endpoints:
  POST /v1/chat/completions  — OpenAI-compatible chat (stream + non-stream)
  GET  /v1/models            — list models from OpenRouter
  GET  /health               — health check
"""

import json
import logging
import os
import sys

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("OPENROUTER_RR_PROXY_PORT", "8451"))

SECRETS_FILE = os.path.expanduser("~/.config/devforge/secrets.env")

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
CHAT_ENDPOINT = f"{OPENROUTER_API_BASE}/chat/completions"
MODELS_ENDPOINT = f"{OPENROUTER_API_BASE}/models"

YOUR_SITE_URL = os.environ.get("YOUR_SITE_URL", "https://devforge.152-69-229-246.nip.io")
YOUR_APP_NAME = os.environ.get("YOUR_APP_NAME", "DevForge OpenRouter RR Proxy")

# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------


def _load_keys() -> list[str]:
    """Load OpenRouter API keys from secrets.env."""
    keys: list[str] = []
    if os.path.exists(SECRETS_FILE):
        with open(SECRETS_FILE) as f:
            for line in f:
                line = line.strip()
                if (
                    line.startswith("OPENROUTER_MESIDS_API_KEY=")
                    or line.startswith("OPENROUTER_MINIPARK4U_API_KEY=")
                    or line.startswith("OPENROUTER_HYEONMINPARK4U_API_KEY=")
                ):
                    keys.append(line.split("=", 1)[1].strip().strip("\"'"))
    # Fallback: env vars
    for env_var in (
        "OPENROUTER_MESIDS_API_KEY",
        "OPENROUTER_MINIPARK4U_API_KEY",
        "OPENROUTER_HYEONMINPARK4U_API_KEY",
    ):
        val = os.environ.get(env_var)
        if val and val not in keys:
            keys.append(val)
    return keys


KEYS = _load_keys()
if len(KEYS) < 2:
    print(
        f"❌ ERROR: Need at least 2 OpenRouter API keys, found {len(KEYS)}. Check secrets.env or env vars.",
        file=sys.stderr,
    )
    sys.exit(1)

NUM_KEYS = len(KEYS)
print(f"✅ Loaded {NUM_KEYS} OpenRouter API keys (port {LISTEN_PORT})", flush=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("openrouter-rr-proxy")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="OpenRouter RR Proxy", version="1.0.0")
client = httpx.AsyncClient(timeout=300.0)
current_key_index = 0


@app.on_event("shutdown")
async def _shutdown() -> None:
    await client.aclose()


# ---------------------------------------------------------------------------
# Round-robin
# ---------------------------------------------------------------------------


def _next_key() -> tuple[int, str]:
    """Get next key index and key value (round-robin, no lock needed for single-worker)."""
    global current_key_index
    idx = current_key_index
    current_key_index = (current_key_index + 1) % NUM_KEYS
    return idx, KEYS[idx]


# ---------------------------------------------------------------------------
# Stream helper
# ---------------------------------------------------------------------------


async def _stream_chunks(response: httpx.Response):
    """Stream SSE chunks from OpenRouter response."""
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    except Exception as e:
        logger.error(f"Stream error: {e}")
    finally:
        await response.aclose()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions with round-robin key rotation."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    is_stream = body.get("stream", False)
    model = body.get("model", "unknown")

    # Try each key in round-robin order
    start_idx, _ = _next_key()
    last_error = "All API keys failed."

    for offset in range(NUM_KEYS):
        idx = (start_idx + offset) % NUM_KEYS
        api_key = KEYS[idx]

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": YOUR_SITE_URL,
            "X-Title": YOUR_APP_NAME,
        }

        logger.info("→ key[%d/%d] model=%s stream=%s", idx + 1, NUM_KEYS, model, is_stream)

        try:
            if is_stream:
                req = client.build_request("POST", CHAT_ENDPOINT, json=body, headers=headers)
                resp = await client.send(req, stream=True)

                if resp.status_code == 200:
                    logger.info("✓ key[%d] streaming", idx + 1)
                    return StreamingResponse(
                        _stream_chunks(resp),
                        media_type="text/event-stream",
                        headers={
                            k: v
                            for k, v in resp.headers.items()
                            if k.lower() in ("content-type", "content-encoding", "cache-control")
                        },
                    )
                elif resp.status_code == 429:
                    detail = "429 key[%d]: %s" % (idx + 1, await resp.aread())
                    logger.warning(detail)
                    await resp.aclose()
                    last_error = detail
                    continue
                else:
                    detail = "HTTP %d key[%d]: %s" % (resp.status_code, idx + 1, await resp.aread())
                    logger.error(detail)
                    await resp.aclose()
                    last_error = detail
                    continue

            else:
                resp = await client.post(CHAT_ENDPOINT, json=body, headers=headers)

                if resp.status_code == 200:
                    logger.info("✓ key[%d] done", idx + 1)
                    return JSONResponse(content=resp.json(), status_code=200)
                elif resp.status_code == 429:
                    detail = f"429 key[{idx + 1}]: {resp.text}"
                    logger.warning(detail)
                    last_error = detail
                    continue
                else:
                    detail = f"HTTP {resp.status_code} key[{idx + 1}]: {resp.text}"
                    logger.error(detail)
                    last_error = detail
                    continue

        except httpx.RequestError as e:
            detail = f"RequestError key[{idx + 1}]: {e.__class__.__name__} - {e}"
            logger.error(detail)
            last_error = detail
            continue

    logger.error("✗ All %d keys failed: %s", NUM_KEYS, last_error)
    raise HTTPException(status_code=502, detail=last_error)


@app.get("/v1/models")
async def list_models():
    """List models from OpenRouter (first key)."""
    headers = {
        "Authorization": f"Bearer {KEYS[0]}",
        "HTTP-Referer": YOUR_SITE_URL,
        "X-Title": YOUR_APP_NAME,
    }
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
    return {"status": "ok", "keys": NUM_KEYS}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
