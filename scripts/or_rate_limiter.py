#!/usr/bin/env python3
# Status: experimental
# Path: opencode.json — local rate-limiting proxy for OpenRouter free models
"""OpenRouter rate-limiting proxy (15 RPM, multi-key round-robin + auto-retry on 429/5xx)."""

import os, time, asyncio, json, logging
import aiohttp
from aiohttp import web

OR_BASE = "https://openrouter.ai/api/v1"

def _load_openrouter_keys() -> list:
    """Load all available OpenRouter API keys from env (round-robin pool)."""
    keys = []
    for env_var in (
        "OPENROUTER_API_KEY",
        "OPENROUTER_MESIDS_API_KEY",
        "OPENROUTER_MINIPARK4U_API_KEY",
    ):
        k = os.environ.get(env_var, "").strip()
        if k and k not in keys:
            keys.append(k)
    return keys or []

API_KEYS = _load_openrouter_keys()
if not API_KEYS:
    raise RuntimeError(
        "No OpenRouter API key found. Set OPENROUTER_API_KEY / "
        "OPENROUTER_MESIDS_API_KEY / OPENROUTER_MINIPARK4U_API_KEY"
    )

RPM = 15
INTERVAL = 60.0 / RPM

_key_idx = 0
_key_lock = asyncio.Lock()

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
MAX_KEY_ATTEMPTS = 3

log = logging.getLogger("or-proxy")

class RateLimiter:
    def __init__(self):
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            wait = INTERVAL - (now - self._last)
            if wait > 0:
                log.debug("throttle %.1fs", wait)
                await asyncio.sleep(wait)
            self._last = time.monotonic()

limiter = RateLimiter()

async def _next_api_key() -> str:
    """Round-robin across API key accounts to bypass per-key RPM limits."""
    global _key_idx
    async with _key_lock:
        key = API_KEYS[_key_idx % len(API_KEYS)]
        _key_idx += 1
    return key

def _is_retryable(status: int) -> bool:
    return status in RETRYABLE_STATUS

async def _do_request(sess, method, url, body, headers, is_stream):
    """One upstream request. Returns (status, content_type, body_bytes) or stream iterator."""
    return await sess.request(
        method, url, data=body, headers=headers,
        timeout=aiohttp.ClientTimeout(total=120),
    )

async def proxy(request):
    path = request.match_info.get("path", "")
    path = path.removeprefix("v1/") if path.startswith("v1/") else path
    url = f"{OR_BASE}/{path}" if path else OR_BASE
    body = await request.read()
    is_stream = request.headers.get("accept", "") == "text/event-stream"

    base_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "transfer-encoding", "authorization")
    }

    last_status = 0
    last_body = b""
    last_ct = "application/json"

    async with aiohttp.ClientSession() as sess:
        for attempt in range(MAX_KEY_ATTEMPTS):
            await limiter.wait()
            api_key = await _next_api_key()
            headers = dict(base_headers)
            headers["Authorization"] = f"Bearer {api_key}"

            try:
                resp = await _do_request(sess, request.method, url, body, headers, is_stream)
            except Exception as e:
                log.warning("upstream conn error (attempt %d/%d, key %d): %s",
                           attempt + 1, MAX_KEY_ATTEMPTS,
                           (_key_idx - 1) % len(API_KEYS), e)
                if attempt < MAX_KEY_ATTEMPTS - 1:
                    continue
                err = json.dumps({"error": {"message": f"Upstream failed after {MAX_KEY_ATTEMPTS} attempts: {e}"}}).encode("utf-8")
                return web.Response(body=err, status=502, content_type="application/json")

            if not _is_retryable(resp.status):
                # Non-retryable — pass through immediately
                if is_stream:
                    return await _stream_response(request, resp)
                data = await resp.read()
                return web.Response(body=data, status=resp.status, content_type=resp.content_type or "application/json")

            # Retryable — drain body, close, retry with next key
            last_status = resp.status
            try:
                last_body = await resp.read()
                last_ct = resp.content_type or "application/json"
            except Exception:
                pass
            try:
                resp.release()
            except Exception:
                pass

            key_id = (_key_idx - 1) % len(API_KEYS)
            log.warning("retryable status %d from key %d (attempt %d/%d) — switching key",
                       resp.status, key_id, attempt + 1, MAX_KEY_ATTEMPTS)
            if attempt < MAX_KEY_ATTEMPTS - 1:
                continue

    # All attempts exhausted — return last error body
    return web.Response(body=last_body, status=last_status, content_type=last_ct)

async def _stream_response(request, resp):
    """Forward streaming response chunks to client."""
    resp2 = web.StreamResponse(status=resp.status)
    resp2.headers["Content-Type"] = "text/event-stream"
    resp2.headers["Cache-Control"] = "no-cache"
    resp2.headers["X-Accel-Buffering"] = "no"
    await resp2.prepare(request)
    async for chunk in resp.content.iter_any():
        await resp2.write(chunk)
    await resp2.write_eof()
    return resp2

async def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(levelname)s %(message)s")
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", proxy)
    port = int(os.environ.get("OR_PROXY_PORT", 4311))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    log.info("OR proxy on 127.0.0.1:%d (%d RPM, %d keys round-robin, max %d retries)",
            port, RPM, len(API_KEYS), MAX_KEY_ATTEMPTS)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
