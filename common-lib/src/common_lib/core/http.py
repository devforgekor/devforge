"""
core/http | Async HTTP client with retry and circuit breaker | needs:httpx | http_request(),CircuitBreaker()
"""
import asyncio
import time
from typing import Any

import httpx


class CircuitBreaker:
    """Open after fail_max consecutive failures; auto-reset after reset_timeout seconds."""

    def __init__(self, fail_max: int = 5, reset_timeout: float = 30.0) -> None:
        self.fail_max = fail_max
        self.reset_timeout = reset_timeout
        self.fail_count = 0
        self.open_until: float | None = None

    def allow(self) -> bool:
        if self.open_until is None:
            return True
        if time.monotonic() > self.open_until:
            self.open_until = None
            self.fail_count = 0
            return True
        return False

    def record_failure(self) -> None:
        self.fail_count += 1
        if self.fail_count >= self.fail_max:
            self.open_until = time.monotonic() + self.reset_timeout

    def record_success(self) -> None:
        self.fail_count = 0
        self.open_until = None


async def http_request(
    method: str,
    url: str,
    *,
    retries: int = 3,
    timeout: float = 10.0,
    circuit_breaker: CircuitBreaker | None = None,
    client: httpx.AsyncClient | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Async HTTP request with exponential-backoff retry and optional circuit breaker.

    Args:
        method: HTTP method ("GET", "POST", etc.).
        url: Request URL.
        retries: Maximum attempt count (default 3).
        timeout: Per-request timeout in seconds (default 10).
        circuit_breaker: Optional CircuitBreaker instance.
        client: Optional pre-existing httpx.AsyncClient (used as context manager).
        **kwargs: Passed through to httpx.AsyncClient.request().

    Raises:
        RuntimeError: When the circuit breaker is open.
        httpx.HTTPStatusError: On non-2xx response after all retries.
    """
    cb = circuit_breaker
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        if cb and not cb.allow():
            raise RuntimeError("circuit breaker is open")
        try:
            if client is not None:
                resp = await client.request(method, url, timeout=timeout, **kwargs)
                resp.raise_for_status()
            else:
                async with httpx.AsyncClient() as ac:
                    resp = await ac.request(method, url, timeout=timeout, **kwargs)
                    resp.raise_for_status()
            if cb:
                cb.record_success()
            return resp
        except Exception as e:
            last_exc = e
            if cb:
                cb.record_failure()
            if attempt == retries:
                raise
            await asyncio.sleep(2 ** (attempt - 1))
    raise last_exc  # type: ignore[misc]
