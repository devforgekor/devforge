#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/test_research_duckduckgo.py — pytest (unit + gated live)
"""DuckDuckGo HTML search provider tests.

The live test is opt-in: DEVFORGE_DUCKDUCKGO_LIVE=1 (needs the DataImpulse proxy).
"""

from __future__ import annotations

import gzip
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from lib.research import duckduckgo  # noqa: E402

_SERP = b"""
<div class="result results_links web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=z">Example A</a>
  </h2>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Snippet
     <b>one</b> here</a>
</div>
<div class="result results_links web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fb">Example B</a>
  </h2>
  <a class="result__snippet" href="#">Snippet two</a>
</div>
"""

_LITE_SERP = b"""
<tr>
  <td class="result-link"><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Flite.example%2F1">Lite One</a></td>
  <td class="result-snippet">lite snippet</td>
</tr>
"""


# ── _unwrap_url ───────────────────────────────────────────────────────


def test_should_unwrap_uddg_redirect_when_ddg_wrapped():
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&rut=z"
    assert duckduckgo._unwrap_url(href) == "https://example.com/a"


def test_should_prefix_https_when_protocol_relative():
    assert duckduckgo._unwrap_url("//cdn.example.com/x") == "https://cdn.example.com/x"


def test_should_keep_plain_url_when_not_wrapped():
    assert duckduckgo._unwrap_url("https://example.com/plain") == "https://example.com/plain"


# ── parse_serp ────────────────────────────────────────────────────────


def test_should_extract_rows_when_html_serp():
    rows = duckduckgo.parse_serp(_SERP)
    assert [r["url"] for r in rows] == ["https://example.com/a", "https://example.com/b"]
    assert rows[0]["title"] == "Example A"
    assert rows[0]["description"] == "Snippet one here"


def test_should_extract_rows_when_lite_serp():
    rows = duckduckgo.parse_serp(_LITE_SERP)
    assert rows[0]["url"] == "https://lite.example/1"
    assert rows[0]["title"] == "Lite One"
    assert rows[0]["description"] == "lite snippet"


def test_should_return_empty_when_no_results():
    assert duckduckgo.parse_serp(b"<html><body>no results</body></html>") == []


# ── duckduckgo_search ─────────────────────────────────────────────────


def test_should_return_empty_when_query_blank():
    assert duckduckgo.duckduckgo_search("   ") == []


def test_should_return_rows_with_source_when_endpoint_succeeds(monkeypatch):
    monkeypatch.setattr(duckduckgo, "_post", lambda *a, **k: (200, _SERP))
    rows = duckduckgo.duckduckgo_search("anything", max_results=5)
    assert len(rows) == 2
    assert all(r["source"] == "duckduckgo" for r in rows)


def test_should_cap_results_when_more_than_max(monkeypatch):
    monkeypatch.setattr(duckduckgo, "_post", lambda *a, **k: (200, _SERP))
    assert len(duckduckgo.duckduckgo_search("anything", max_results=1)) == 1


def test_should_retry_then_return_empty_when_captcha(monkeypatch):
    calls = []

    def fake_post(endpoint, query, region, proxy_url):
        calls.append(endpoint)
        return 202, b"<html>anomaly captcha</html>"

    monkeypatch.setattr(duckduckgo, "_post", fake_post)
    monkeypatch.setattr(duckduckgo.time, "sleep", lambda _s: None)
    assert duckduckgo.duckduckgo_search("x", retries=2) == []
    # html x3 (1 + 2 retries) then lite x1
    assert len(calls) == 4
    assert calls[-1] == duckduckgo.LITE_ENDPOINT


# ── gzip body handling ────────────────────────────────────────────────


def test_should_advertise_gzip_when_building_headers():
    assert duckduckgo._HEADERS["Accept-Encoding"] == "gzip"


def test_should_inflate_body_when_content_encoding_gzip():
    assert duckduckgo._gunzip(gzip.compress(_SERP), "gzip") == _SERP


def test_should_return_raw_body_when_encoding_absent():
    assert duckduckgo._gunzip(_SERP, "") == _SERP


def test_should_return_raw_body_when_corrupt_gzip():
    assert duckduckgo._gunzip(b"\x1f\x8bnot-a-payload", "gzip") == b"\x1f\x8bnot-a-payload"


def test_should_parse_rows_when_post_returns_gzip(monkeypatch):
    payload = gzip.compress(_SERP)

    class _Resp:
        status = 200
        headers = {"Content-Encoding": "gzip"}

        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(
        duckduckgo.urllib.request,
        "build_opener",
        lambda *_a: type("_Opener", (), {"open": lambda self, req, timeout=None: _Resp()})(),
    )
    status, body = duckduckgo._post(duckduckgo.HTML_ENDPOINT, "q", "wt-wt", None)
    assert status == 200
    assert body == _SERP
    assert len(duckduckgo.parse_serp(body)) == 2


# ── body read policy (only 200 bodies are ever parsed) ────────────────


def test_should_not_read_body_when_status_not_200(monkeypatch):
    class _Resp:
        status = 202
        headers = {"Content-Encoding": "gzip"}

        def read(self, *_a):
            raise AssertionError("a 202 anomaly body (~14 KB) must not be downloaded")

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.close()
            return False

    monkeypatch.setattr(
        duckduckgo.urllib.request,
        "build_opener",
        lambda *_a: type("_O", (), {"open": lambda self, req, timeout=None: _Resp()})(),
    )
    assert duckduckgo._post(duckduckgo.HTML_ENDPOINT, "q", "wt-wt", None) == (202, b"")


def test_should_not_read_body_when_http_error(monkeypatch):
    class _Body:
        def __init__(self):
            self.read_calls = 0
            self.closed = False

        def read(self, *_a):
            self.read_calls += 1
            return b"404 body"

        def close(self):
            self.closed = True

    err_body = _Body()

    def _open(self, req, timeout=None):
        raise duckduckgo.urllib.error.HTTPError("http://x/", 404, "Not Found", None, err_body)

    monkeypatch.setattr(
        duckduckgo.urllib.request,
        "build_opener",
        lambda *_a: type("_O", (), {"open": _open})(),
    )
    assert duckduckgo._post(duckduckgo.HTML_ENDPOINT, "q", "wt-wt", None) == (404, b"")
    assert err_body.read_calls == 0
    assert err_body.closed


# ── web.py rotation integration ───────────────────────────────────────


def test_should_fall_back_to_duckduckgo_when_providers_exhausted(monkeypatch):
    from lib.research import web

    proxy_obj = web.SearchProxy.__new__(web.SearchProxy)
    monkeypatch.setattr(
        web.SearchProxy, "_search_provider", lambda self, p, q, n: [f"error: {p} exhausted"]
    )
    monkeypatch.setattr(
        web,
        "_duckduckgo_fallback",
        lambda q, n: [
            {"title": "T", "url": "https://e.com", "description": "D", "source": "duckduckgo"}
        ],
    )
    out = proxy_obj.search("q", 5)
    assert out[0].startswith("(duckduckgo fallback")
    assert out[1] == "T | https://e.com | D"


def test_should_fall_back_structured_to_duckduckgo_when_no_providers(monkeypatch):
    from lib.research import web

    proxy_obj = web.SearchProxy.__new__(web.SearchProxy)
    proxy_obj._rotators = {}
    monkeypatch.setattr(
        web,
        "_duckduckgo_fallback",
        lambda q, n: [{"title": "T", "url": "https://e.com", "source": "duckduckgo"}],
    )
    out = proxy_obj.search_structured("q", 5)
    assert out == [{"title": "T", "url": "https://e.com", "source": "duckduckgo"}]


# ── live (opt-in) ─────────────────────────────────────────────────────


@pytest.mark.integration
def test_should_return_results_when_live_proxy_available():
    if os.environ.get("DEVFORGE_DUCKDUCKGO_LIVE") != "1":
        pytest.skip("set DEVFORGE_DUCKDUCKGO_LIVE=1 to run the live DDG test")
    rows = duckduckgo.duckduckgo_search("opencode ai coding agent", max_results=5)
    assert rows
    assert all(r["url"].startswith("http") for r in rows)
