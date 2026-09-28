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


# ── keep-alive connection fakes ───────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"", encoding: str = ""):
        self.status = status
        self._body = body
        self._encoding = encoding
        self.closed = False

    def getheader(self, name, default=None):
        if name == "Content-Encoding":
            return self._encoding or default
        return default

    def read(self, *_a):
        if self.status != 200:
            raise AssertionError(f"a {self.status} body must not be downloaded")
        return self._body

    def close(self):
        self.closed = True


class _FakeConn:
    def __init__(self, factory, host=None, port=None, timeout=None, error=None):
        self._factory = factory
        self._error = error
        self.host, self.port, self.timeout = host, port, timeout
        self.tunnel = None
        self.requests = []
        self.closed = False

    def set_tunnel(self, host, port=None, headers=None):
        self.tunnel = (host, port, headers)

    def request(self, method, path, body=None, headers=None):
        if self._error is not None:
            raise self._error
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        return self._factory()

    def close(self):
        self.closed = True


def _patch_https(monkeypatch, specs):
    """Replace http.client.HTTPSConnection with fakes built from `specs`.

    Each spec is {"response": _FakeResponse} or {"error": Exception}; the last
    entry repeats once the created connections run out.
    """
    created = []

    class _Conn(_FakeConn):
        def __init__(self, host=None, port=None, timeout=None):
            spec = specs[len(created)] if len(created) < len(specs) else specs[-1]
            super().__init__(
                lambda: spec.get("response"), host, port, timeout, error=spec.get("error")
            )
            created.append(self)

    monkeypatch.setattr(duckduckgo.http.client, "HTTPSConnection", _Conn)
    return created


@pytest.fixture(autouse=True)
def _clear_conns():
    duckduckgo._CONN.clear()
    duckduckgo._CONN_PROXY.clear()
    yield
    duckduckgo._CONN.clear()
    duckduckgo._CONN_PROXY.clear()


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
    conns = _patch_https(
        monkeypatch, [{"response": _FakeResponse(200, gzip.compress(_SERP), encoding="gzip")}]
    )

    status, body = duckduckgo._post(duckduckgo.HTML_ENDPOINT, "q", "wt-wt", None)

    assert status == 200
    assert body == _SERP
    assert len(duckduckgo.parse_serp(body)) == 2
    method, path, req_body, headers = conns[0].requests[0]
    assert (method, path) == ("POST", "/html/")
    assert req_body == b"q=q&kl=wt-wt"
    assert headers["Accept-Encoding"] == "gzip"


# ── body read policy (only 200 bodies are ever parsed) ────────────────


def test_should_not_read_body_when_status_not_200(monkeypatch):
    conns = _patch_https(monkeypatch, [{"response": _FakeResponse(202)}])

    assert duckduckgo._post(duckduckgo.HTML_ENDPOINT, "q", "wt-wt", None) == (202, b"")
    # an unread body poisons the tunnel, so it must be dropped, not reused
    assert conns[0].closed
    assert duckduckgo._CONN == {}


def test_should_not_read_body_when_status_is_error(monkeypatch):
    conns = _patch_https(monkeypatch, [{"response": _FakeResponse(404)}])

    assert duckduckgo._post(duckduckgo.HTML_ENDPOINT, "q", "wt-wt", None) == (404, b"")
    assert conns[0].closed


# ── keep-alive connection reuse ───────────────────────────────────────


def test_should_reuse_connection_when_called_twice(monkeypatch):
    conns = _patch_https(
        monkeypatch, [{"response": _FakeResponse(200, gzip.compress(_SERP), encoding="gzip")}]
    )

    assert duckduckgo._post(duckduckgo.HTML_ENDPOINT, "a", "wt-wt", None)[0] == 200
    assert duckduckgo._post(duckduckgo.HTML_ENDPOINT, "b", "wt-wt", None)[0] == 200

    assert len(conns) == 1
    assert len(conns[0].requests) == 2
    assert not conns[0].closed


def test_should_tunnel_through_proxy_when_proxy_given(monkeypatch):
    conns = _patch_https(monkeypatch, [{"response": _FakeResponse(200, _SERP)}])

    duckduckgo._post(duckduckgo.HTML_ENDPOINT, "q", "wt-wt", "http://user__cr.kr:pw@host:823")

    host, port, headers = conns[0].tunnel
    assert (host, port) == ("html.duckduckgo.com", 443)
    assert headers["Proxy-Authorization"].startswith("Basic ")


def test_should_reopen_connection_when_proxy_changes(monkeypatch):
    conns = _patch_https(
        monkeypatch,
        [{"response": _FakeResponse(200, _SERP)}, {"response": _FakeResponse(200, _SERP)}],
    )

    duckduckgo._post(duckduckgo.HTML_ENDPOINT, "q", "wt-wt", "http://a:1@host:823")
    first = conns[0]
    duckduckgo._post(duckduckgo.HTML_ENDPOINT, "q", "wt-wt", "http://b:2@host:823")

    assert len(conns) == 2
    assert first.closed


def test_should_reconnect_once_when_tunnel_is_stale(monkeypatch):
    conns = _patch_https(
        monkeypatch,
        [
            {"error": duckduckgo.http.client.RemoteDisconnected("stale")},
            {"response": _FakeResponse(200, _SERP)},
        ],
    )

    status, body = duckduckgo._post(duckduckgo.HTML_ENDPOINT, "q", "wt-wt", None)

    assert status == 200
    assert body == _SERP
    assert len(conns) == 2
    assert conns[0].closed


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
