#!/usr/bin/env python3.11
# Status: experimental
# Path: systemd:openrouter-rr-proxy.service
"""
OpenRouter API Key Round-Robin Proxy.

Distributes requests across 3 OpenRouter accounts. Every model — whether
registered in opencode-rr.json (provider.openrouter.models) or unknown —
rotates over ALL accounts per request (account 1→2→3→1...). A request that
fails on one account is retried on the next, so a single account's daily
limit does not kill a model.

Registered models round-robin from their own cursor (each model spreads its
own traffic); unknown models use the shared global cursor. The per-model
account list is read from opencode-rr.json, so a model can be scoped to a
subset of accounts later without code changes.

Endpoints:
  POST /v1/chat/completions  — OpenAI-compatible chat (stream + non-stream)
  GET  /v1/models            — list models from OpenRouter
  GET  /health               — health check (incl. model→account rotation map)
"""

import json
import logging
import os
import sys
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("OPENROUTER_RR_PROXY_PORT", "8451"))

# Pinned model source: the RR profile opencode reads (daily auto-refresh
# rewrites its provider.openrouter.models — proxy picks the new mapping up via
# mtime check, no restart needed).
OPCODE_CONFIG = os.path.expanduser("~/.config/opencode/opencode-rr.json")

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
CHAT_ENDPOINT = f"{OPENROUTER_API_BASE}/chat/completions"
MODELS_ENDPOINT = f"{OPENROUTER_API_BASE}/models"

YOUR_SITE_URL = os.environ.get("YOUR_SITE_URL", "https://devforge.152-69-229-246.nip.io")
YOUR_APP_NAME = os.environ.get("YOUR_APP_NAME", "DevForge OpenRouter RR Proxy")

# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------


def _load_keys() -> list[str]:
    """Load OpenRouter API keys from environment variables (Azure KV).

    Supports 3-account rotation: MESIDS, MINIPARK4U, HYEONMINPARK4U
    """
    keys: list[str] = []

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
        f"❌ ERROR: Need at least 2 OpenRouter API keys, found {len(KEYS)}. Check env vars.",
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

client: httpx.AsyncClient = None  # type: ignore[assignment]
current_key_index = 0

# Account labels aligned with KEYS order.
ACCOUNT_LABELS = ["MESIDS", "MINIPARK4U", "HYEONMINPARK4U"]

# model_id -> ordered key indices, loaded from opencode-rr.json (mtime-cached).
_model_accounts: dict[str, list[int]] = {}
_model_mtime: float = -1.0
# model_id -> next position within its account list (per-model round-robin).
_model_cursor: dict[str, int] = {}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global client
    client = httpx.AsyncClient(timeout=300.0)
    yield
    await client.aclose()


app = FastAPI(title="OpenRouter RR Proxy", version="1.0.0", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Round-robin
# ---------------------------------------------------------------------------


def _account_label(idx: int) -> str:
    """Human-readable account name for a key index."""
    return ACCOUNT_LABELS[idx] if idx < len(ACCOUNT_LABELS) else f"key{idx + 1}"


def _next_key() -> tuple[int, str]:
    """Get next key index and key value (round-robin, no lock needed for single-worker)."""
    global current_key_index
    idx = current_key_index
    current_key_index = (current_key_index + 1) % NUM_KEYS
    return idx, KEYS[idx]


def _load_model_accounts() -> dict[str, list[int]]:
    """Map each configured model to its account rotation order.

    Every model in opencode-rr.json provider.openrouter.models rotates over all
    accounts (account 1→2→3...). The daily auto-refresh rewrites opencode-rr.json;
    the mtime check picks the change up without restart.
    """
    mapping: dict[str, list[int]] = {}
    try:
        with open(OPCODE_CONFIG) as f:
            cfg = json.load(f)
        models = cfg.get("provider", {}).get("openrouter", {}).get("models", {})
        for model_id in models:
            mapping[model_id] = list(range(NUM_KEYS))
    except Exception as e:
        logger.error("Failed to load model→account map from %s: %s", OPCODE_CONFIG, e)
    return mapping


def _refresh_model_accounts() -> None:
    """Reload the model→account map when opencode-rr.json changes (mtime)."""
    global _model_accounts, _model_mtime
    try:
        mtime = os.path.getmtime(OPCODE_CONFIG)
    except OSError:
        mtime = -1.0
    if mtime != _model_mtime:
        _model_accounts = _load_model_accounts()
        _model_mtime = mtime
        if _model_accounts:
            logger.info(
                "Model→accounts: %s",
                {m: [_account_label(i) for i in idx] for m, idx in _model_accounts.items()},
            )


def _model_account_order(model: str) -> list[int] | None:
    """Return the account order for a registered model, or None if unregistered.

    The starting account advances per model (1→2→3→1...) so consecutive requests
    for the same model spread across accounts instead of hammering one.
    """
    _refresh_model_accounts()
    accounts = _model_accounts.get(model)
    if not accounts:
        return None
    start = _model_cursor.get(model, 0) % len(accounts)
    _model_cursor[model] = (start + 1) % len(accounts)
    return [accounts[(start + o) % len(accounts)] for o in range(len(accounts))]


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


async def _forward_key(idx: int, body: dict, is_stream: bool, model: str):
    """Send the request via one key. Returns a Response on success or raises
    HTTPException on failure (429/other). Caller decides fallback policy."""
    api_key = KEYS[idx]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": YOUR_SITE_URL,
        "X-Title": YOUR_APP_NAME,
    }

    logger.info(
        "→ key[%d/%d] %s model=%s stream=%s",
        idx + 1,
        NUM_KEYS,
        _account_label(idx),
        model,
        is_stream,
    )

    if client is None:  # lifecycle guard: startup not complete
        raise HTTPException(status_code=503, detail="Proxy not ready")

    try:
        if is_stream:
            req = client.build_request("POST", CHAT_ENDPOINT, json=body, headers=headers)
            resp = await client.send(req, stream=True)

            if resp.status_code == 200:
                logger.info("✓ key[%d] %s streaming", idx + 1, _account_label(idx))
                return StreamingResponse(
                    _stream_chunks(resp),
                    media_type="text/event-stream",
                    headers={
                        k: v
                        for k, v in resp.headers.items()
                        if k.lower() in ("content-type", "content-encoding", "cache-control")
                    },
                )

            # Non-2xx streaming response: read error body then ALWAYS release the
            # connection (even if aread() raises), preventing pool leaks.
            try:
                err_body = (await resp.aread()).decode(errors="replace")
            finally:
                await resp.aclose()
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"key[{idx + 1}] {_account_label(idx)}: {err_body}",
            )

        else:
            resp = await client.post(CHAT_ENDPOINT, json=body, headers=headers)

            if resp.status_code == 200:
                logger.info("✓ key[%d] %s done", idx + 1, _account_label(idx))
                return JSONResponse(content=resp.json(), status_code=200)

            # Non-2xx: release the connection explicitly before retrying.
            err_body = resp.text
            await resp.aclose()
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"key[{idx + 1}] {_account_label(idx)}: {err_body}",
            )

    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"RequestError key[{idx + 1}] {_account_label(idx)}: {e.__class__.__name__} - {e}",
        )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions.

    Every model round-robins over all accounts. Registered models (present in
    opencode-rr.json) start from their own per-model cursor; unknown models use
    the shared global cursor. Requests are retried on the next account when one
    fails, so an account's daily limit (429) does not take a model down.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    is_stream = body.get("stream", False)
    model = body.get("model", "unknown")

    order = _model_account_order(model)
    if order is None:
        # Unknown model: round-robin over all keys (shared global cursor).
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
    """List models from OpenRouter (first key)."""
    if client is None:
        raise HTTPException(status_code=503, detail="Proxy not ready")
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
    _refresh_model_accounts()
    accounts = {m: [_account_label(i) for i in idx] for m, idx in _model_accounts.items()}
    return {"status": "ok", "keys": NUM_KEYS, "accounts": accounts}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
