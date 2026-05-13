"""Tests for src/common_lib/core/http.py"""
import asyncio
import time

import pytest
import httpx

from common_lib.core.http import CircuitBreaker, http_request


class TestCircuitBreaker:
    def test_initially_allows(self):
        cb = CircuitBreaker()
        assert cb.allow() is True

    def test_opens_after_fail_max(self):
        cb = CircuitBreaker(fail_max=3, reset_timeout=60.0)
        for _ in range(3):
            cb.record_failure()
        assert cb.allow() is False

    def test_does_not_open_below_fail_max(self):
        cb = CircuitBreaker(fail_max=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.allow() is True

    def test_resets_after_timeout(self):
        cb = CircuitBreaker(fail_max=1, reset_timeout=0.05)
        cb.record_failure()
        assert cb.allow() is False
        time.sleep(0.1)
        assert cb.allow() is True

    def test_record_success_clears_state(self):
        cb = CircuitBreaker(fail_max=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.fail_count == 0
        assert cb.open_until is None
        assert cb.allow() is True


class TestHttpRequest:
    def test_successful_get(self):
        transport = httpx.MockTransport(handler=lambda req: httpx.Response(200, text="ok"))
        client = httpx.AsyncClient(transport=transport)

        async def run():
            return await http_request("GET", "http://test.example/path", client=client)

        resp = asyncio.run(run())
        assert resp.status_code == 200

    def test_retries_on_failure_then_succeeds(self):
        call_count = 0

        def handler(req):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return httpx.Response(500)
            return httpx.Response(200, text="ok")

        transport = httpx.MockTransport(handler=handler)
        client = httpx.AsyncClient(transport=transport)

        async def run():
            return await http_request("GET", "http://test.example/", retries=3, client=client)

        resp = asyncio.run(run())
        assert resp.status_code == 200
        assert call_count == 3

    def test_raises_after_all_retries_exhausted(self):
        transport = httpx.MockTransport(handler=lambda req: httpx.Response(503))
        client = httpx.AsyncClient(transport=transport)

        async def run():
            await http_request("GET", "http://test.example/", retries=2, client=client)

        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(run())

    def test_circuit_breaker_blocks_when_open(self):
        cb = CircuitBreaker(fail_max=1, reset_timeout=60.0)
        cb.record_failure()  # force open

        async def run():
            await http_request("GET", "http://test.example/", circuit_breaker=cb)

        with pytest.raises(RuntimeError, match="circuit breaker is open"):
            asyncio.run(run())

    def test_circuit_breaker_records_failure(self):
        cb = CircuitBreaker(fail_max=5)
        transport = httpx.MockTransport(handler=lambda req: httpx.Response(500))
        client = httpx.AsyncClient(transport=transport)

        async def run():
            await http_request("GET", "http://test.example/", retries=1, circuit_breaker=cb, client=client)

        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(run())

        assert cb.fail_count >= 1

    def test_shared_client_not_closed_between_calls(self):
        call_count = 0

        def handler(req):
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, text=f"call{call_count}")

        transport = httpx.MockTransport(handler=handler)
        client = httpx.AsyncClient(transport=transport)

        async def run():
            r1 = await http_request("GET", "http://test.example/1", client=client)
            r2 = await http_request("GET", "http://test.example/2", client=client)
            return r1, r2

        r1, r2 = asyncio.run(run())
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert call_count == 2
