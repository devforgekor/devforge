#!/usr/bin/env python3
# Status: experimental
# Path: opencode.json — local rate-limiting proxy for OpenRouter free models
"""OpenRouter rate-limiting proxy (15 RPM → safe margin from 20 RPM limit)."""

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

async def proxy(request):
    path = request.match_info.get("path", "")
    path = path.removeprefix("v1/") if path.startswith("v1/") else path
    url = f"{OR_BASE}/{path}" if path else OR_BASE
    body = await request.read()
    is_stream = request.headers.get("accept", "") == "text/event-stream"

    await limiter.wait()

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "transfer-encoding")
    }
    headers["Authorization"] = f"Bearer {await _next_api_key()}"

    async with aiohttp.ClientSession() as sess:
        async with sess.request(
            request.method, url, data=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            if is_stream:
                resp2 = web.StreamResponse(status=resp.status)
                resp2.headers["Content-Type"] = "text/event-stream"
                resp2.headers["Cache-Control"] = "no-cache"
                resp2.headers["X-Accel-Buffering"] = "no"
                await resp2.prepare(request)
                async for chunk in resp.content.iter_any():
                    await resp2.write(chunk)
                await resp2.write_eof()
                return resp2
            data = await resp.read()
            return web.Response(body=data, status=resp.status, content_type=resp.content_type or "application/json")

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
    log.info("OR proxy on 127.0.0.1:%d (%d RPM, %d accounts round-robin)", port, RPM, len(API_KEYS))
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
