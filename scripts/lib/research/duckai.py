#!/usr/bin/env python3.12
# Status: experimental
# Path: imported by — lib/research/web.py (facade), lib/research/__init__.py
"""Duck.ai (DuckDuckGo AI chat) warm-session client.

[WHY] Duck.ai rejects datacenter IPs (Cloudflare challenge / ERR_CHALLENGE) and
detects headless Chromium (ERR_BN_LIMIT / code 84f2). We therefore run a *headed*
Chromium under Xvfb through the DataImpulse residential proxy, and keep one warm
browser session alive so a question costs ~35 KB over the proxy. The first cold
load costs ~1.3 MB; every one after that costs ~82 KB, because the persistent
profile reuses the disk cache for the scripts duck.ai serves with
`max-age=31536000` (see _user_data_dir).
"""

from __future__ import annotations

import atexit
import base64
import concurrent.futures
import json
import os
import queue
import re
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from playwright.sync_api import (
        BrowserContext,
        CDPSession,
        Page,
        Playwright,
        Response,
    )

from lib.research import proxy

_LOG_PREFIX = "[research.duckai]"

DEFAULT_URL = "https://duck.ai/"

# (model id accepted by ask(), distinctive substring of the collapsed picker button).
# [WHY] the picker shows a shortened label — "GPT-5.6 Luna" renders as "5.6 Luna"
# (measured) — so the button cannot be matched on the id alone, while the open menu
# matches on the full id. Keeping both halves in one row makes this the single
# source of truth: adding a model is one line and the button pattern can no longer
# drift away from the id list.
MODELS: tuple[tuple[str, str], ...] = (
    ("GPT-5.6 Luna", "Luna"),
    ("GPT-5.4 mini", "mini"),
    ("Claude Haiku 4.5", "Haiku"),
    ("Mistral Small 4", "Mistral"),
    ("gpt-oss 120B", "gpt-oss"),
    ("Gemma 4 31B", "Gemma"),
)
DEFAULT_MODEL = MODELS[0][0]
_MODEL_PATTERN = re.compile("|".join(re.escape(token) for _, token in MODELS))

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# [WHY] every byte over the metered residential proxy is billed: analytics beacons
# and visual-only webfonts are pure waste. Blocking the fonts alone cut the cold
# load by 592 KB and 6.3 s with no functional loss (A/B verified: same answer, the
# textarea/submit/model picker all still work).
# Blocked via CDP Network.setBlockedURLs, NOT context.route(): enabling Playwright
# interception makes response.body() fail with "No data found for resource".
_BLOCK_URLS = (
    "https://improving.duckduckgo.com/*",
    "https://*.googletagmanager.com/*",
    "https://*.google-analytics.com/*",
    "https://fonts.googleapis.com/*",
    "https://fonts.gstatic.com/*",
    "*woff2*",
    "*woff*",
    "*ttf*",
    "*otf*",
    "*.eot*",
)

_CHAT_URL = "/duckchat/v1/chat"
_NEW_CHAT_LABEL = "새로운 채팅"
_IDLE_ENV = "DEVFORGE_DUCKAI_IDLE_SEC"
_TIMEOUT_ENV = "DEVFORGE_DUCKAI_TIMEOUT_SEC"
_XVFB_ENV = "DEVFORGE_DUCKAI_XVFB_DISPLAY"
_CACHE_ENV = "DEVFORGE_DUCKAI_CACHE_DIR"

_DEFAULT_IDLE_SEC = 600.0
_DEFAULT_TIMEOUT_SEC = 120.0
_XVFB_DISPLAY = ":99"


class DuckAIError(RuntimeError):
    """Duck.ai request failed (challenge, rate limit, network, or parse)."""


def _log(msg: str) -> None:
    print(f"{_LOG_PREFIX} {msg}", file=sys.stderr, flush=True)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# ── DataImpulse proxy: CONNECT auth injection (env loading lives in proxy.py) ──


def _basic_proxy_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


def _inject_proxy_auth(request_head: bytes, auth_header: str) -> bytes:
    """Replace/append Proxy-Authorization on a CONNECT/HTTP request head.

    [WORKAROUND] DataImpulse opens anonymous CONNECT with 200 (random geo) and only
    honors KR targeting (`user__cr.kr`) via Proxy-Authorization; Chromium sends no
    credentials until a 407 that never comes.
    """
    lines = request_head.split(b"\r\n")
    if not lines:
        return request_head
    out = [lines[0]]
    auth_line = f"Proxy-Authorization: {auth_header}".encode()
    replaced = False
    for ln in lines[1:]:
        if ln.lower().startswith(b"proxy-authorization:"):
            if not replaced:
                out.append(auth_line)
                replaced = True
            continue
        out.append(ln)
    if not replaced:
        out.append(auth_line)
    return b"\r\n".join(out)


def _pump(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            ready, _, _ = select.select([a, b], [], [], 60)
            if not ready:
                return
            for sock in ready:
                data = sock.recv(65536)
                if not data:
                    return
                (b if sock is a else a).sendall(data)
    except OSError:
        return


def _inject_handle_client(client: socket.socket, token: str, upstream: tuple[str, int]) -> None:
    try:
        client.settimeout(60)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = client.recv(4096)
            if not chunk:
                return
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        head = _inject_proxy_auth(head, token)
        up = socket.create_connection(upstream, timeout=30)
        try:
            up.sendall(head + b"\r\n\r\n" + rest)
            _pump(client, up)
        finally:
            try:
                up.close()
            except OSError:
                pass
    except OSError:
        return
    finally:
        try:
            client.close()
        except OSError:
            pass


def _inject_proxy_loop(server: socket.socket, token: str, upstream: tuple[str, int]) -> None:
    while True:
        try:
            client, _ = server.accept()
        except OSError:
            return
        threading.Thread(
            target=_inject_handle_client, args=(client, token, upstream), daemon=True
        ).start()


class _InjectProxy:
    """Local HTTP proxy that injects DataImpulse auth on CONNECT (singleton)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._server: Optional[socket.socket] = None
        self._port: Optional[int] = None
        self._token: Optional[str] = None
        self._upstream: Optional[tuple[str, int]] = None

    def url(self, username: str, password: str, host: str, port: str) -> str:
        token = _basic_proxy_auth(username, password)
        upstream = (host, int(port))
        with self._lock:
            if (
                self._server is not None
                and self._port
                and self._token == token
                and self._upstream == upstream
            ):
                return f"http://127.0.0.1:{self._port}"

            if self._server is not None:
                try:
                    self._server.close()
                except OSError:
                    pass
                self._server = None
                self._port = None

            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen(64)
            self._server = server
            self._port = server.getsockname()[1]
            self._token = token
            self._upstream = upstream
            threading.Thread(
                target=_inject_proxy_loop, args=(server, token, upstream), daemon=True
            ).start()
            _log(f"inject proxy up: 127.0.0.1:{self._port} -> {host}:{port}")
            return f"http://127.0.0.1:{self._port}"


_inject_proxy = _InjectProxy()


def _build_proxy_config() -> Optional[dict[str, str]]:
    creds = proxy.credentials()
    if not creds:
        return None
    user, password, host, port = creds
    return {"server": _inject_proxy.url(user, password, host, port)}


# ── Xvfb: headed Chromium needs a display ──────────────────────────────

_xvfb_lock = threading.Lock()
_xvfb_proc: Optional[subprocess.Popen[bytes]] = None


def _ensure_display() -> None:
    """Start Xvfb if no DISPLAY is set (headed Chromium must not be headless)."""
    global _xvfb_proc
    if os.environ.get("DISPLAY"):
        return
    with _xvfb_lock:
        if os.environ.get("DISPLAY"):
            return
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            raise DuckAIError("Xvfb not found — required for headed Chromium")
        display = os.environ.get(_XVFB_ENV) or _XVFB_DISPLAY
        proc = subprocess.Popen(
            [xvfb, display, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        sock = f"/tmp/.X11-unix/X{display.lstrip(':')}"
        for _ in range(50):
            if os.path.exists(sock):
                break
            time.sleep(0.1)
        os.environ["DISPLAY"] = display
        _xvfb_proc = proc
        _log(f"Xvfb started on {display}")


def _user_data_dir() -> str:
    """Persistent Chromium profile — the disk HTTP cache lives here.

    [WHY] duck.ai serves every script/stylesheet with `max-age=31536000` + ETag
    (1.18 MB total), but a throwaway context re-downloads all of it on every cold
    load. Reusing one profile measured 1,296,343 B -> 60,877 B (-95%). The directory
    must survive reboots, so it defaults to ~/.cache rather than /tmp.
    """
    path = os.environ.get(_CACHE_ENV) or os.path.join(
        os.path.expanduser("~"), ".cache", "devforge", "duckai"
    )
    os.makedirs(path, exist_ok=True)
    return path


# ── warm-session client (browser owned by a dedicated worker thread) ───


class DuckAIClient:
    """Persistent Duck.ai session. Safe to call from any thread."""

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self._model = model
        self._timeout = _env_float(_TIMEOUT_ENV, _DEFAULT_TIMEOUT_SEC)
        self._idle = _env_float(_IDLE_ENV, _DEFAULT_IDLE_SEC)
        self._queue: queue.Queue[
            Optional[tuple[str, Optional[str], bool, concurrent.futures.Future[str]]]
        ] = queue.Queue()
        self._closing = False
        self._last_used = time.monotonic()

        # Playwright sync objects live only on the worker thread.
        self._pw: Optional[Playwright] = None
        self._ctx: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._cdp: Optional[CDPSession] = None

        self._worker = threading.Thread(target=self._run, name="duckai-worker", daemon=True)
        self._worker.start()
        atexit.register(self.close)

    # -- public API --

    def ask(self, prompt: str, model: Optional[str] = None, fresh: bool = False) -> str:
        if not prompt or not prompt.strip():
            raise DuckAIError("empty prompt")
        if self._closing:
            raise DuckAIError("client is closed")
        fut: concurrent.futures.Future[str] = concurrent.futures.Future()
        self._queue.put((prompt, model, fresh, fut))
        try:
            return fut.result(timeout=self._timeout + 30)
        except concurrent.futures.TimeoutError as exc:
            raise DuckAIError(f"timeout waiting for Duck.ai ({self._timeout + 30}s)") from exc

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._queue.put(None)
        self._worker.join(timeout=10)
        _teardown_xvfb()

    # -- worker loop --

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=30)
            except queue.Empty:
                if self._closing:
                    break
                if self._page is not None and time.monotonic() - self._last_used > self._idle:
                    self._teardown()
                continue
            if item is None:
                break
            prompt, model, fresh, fut = item
            try:
                if self._page is not None and time.monotonic() - self._last_used > self._idle:
                    self._teardown()
                fut.set_result(self._ask(prompt, model, fresh))
                self._last_used = time.monotonic()
            except Exception as exc:  # noqa: BLE001 — surfaced to caller via future
                fut.set_exception(exc)
                self._teardown()
        self._teardown()

    # -- browser lifecycle (worker thread only) --

    def _ensure_started(self) -> None:
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright

        _ensure_display()
        self._pw = sync_playwright().start()
        launch: dict[str, Any] = {
            "headless": False,
            "ignore_default_args": ["--enable-automation"],
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            "user_agent": _USER_AGENT,
            "viewport": {"width": 1920, "height": 1080},
            "locale": "ko-KR",
            "timezone_id": "Asia/Seoul",
        }
        proxy = _build_proxy_config()
        if proxy:
            launch["proxy"] = proxy
        # [WHY] launch_persistent_context hands back the BrowserContext directly (no
        # separate Browser) and keeps the disk HTTP cache across sessions, which is
        # where the -95% cold-load saving comes from — see _user_data_dir.
        self._ctx = self._pw.chromium.launch_persistent_context(_user_data_dir(), **launch)
        self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        self._page = self._ctx.new_page()
        # [WHY] CDP URL blocking avoids Playwright request interception, which
        # would make response.body() unusable (see _BLOCK_URLS).
        self._cdp = self._ctx.new_cdp_session(self._page)
        self._cdp.send("Network.enable")
        self._cdp.send("Network.setBlockedURLs", {"urls": list(_BLOCK_URLS)})
        self._page.goto(DEFAULT_URL, wait_until="domcontentloaded", timeout=60_000)
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                self._page.wait_for_selector("textarea", timeout=30_000)
                break
            except Exception as exc:  # noqa: BLE001 — transient proxy/load hiccup
                last_exc = exc
                _log(f"duck.ai not ready (attempt {attempt + 1}/3), reloading")
                self._page.reload(wait_until="domcontentloaded", timeout=60_000)
        else:
            raise DuckAIError(f"duck.ai UI did not load: {last_exc}")
        self._select_model(self._model)

    def _teardown(self) -> None:
        for obj, meth in (
            (self._ctx, "close"),
            (self._pw, "stop"),
        ):
            if obj is not None:
                try:
                    getattr(obj, meth)()
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    pass
        self._page = None
        self._ctx = None
        self._pw = None
        self._cdp = None

    def _start_new_chat(self, page: Page) -> None:
        """Click duck.ai's new-chat button so the question starts isolated.

        [WHY] a warm session otherwise carries every earlier question, which is
        wrong for unrelated prompts. The label is matched as a substring, so the
        shortcut hint ("새로운 채팅⏎CTRL + ⇧ + O") still matches while the
        sibling "새로운 음성 채팅" does not.
        """
        button = page.locator("button").filter(has_text=_NEW_CHAT_LABEL).first
        try:
            button.click(timeout=5000)
        except Exception:  # noqa: BLE001 — label drift; the fallback also isolates
            # [WHY] a reload drops the in-memory conversation too and the
            # persistent profile keeps it cheap, so isolation is never silently lost.
            _log("new-chat button unavailable — reloading to isolate the question")
            page.reload(wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector("textarea", timeout=30_000)
        else:
            page.wait_for_timeout(400)

    def _select_model(self, model: str) -> None:
        page = self._page
        assert page is not None
        if not page.locator("button").filter(has_text=_MODEL_PATTERN).count():
            # [WHY] sending a message unmounts the picker button — the model name
            # survives only as a read-only <strong> label (measured), so no button
            # matches the pattern and the lookup below would time out. A reload
            # brings the button back; the persistent profile keeps that cheap.
            _log("model picker gone after chat — reloading to restore it")
            page.reload(wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector("textarea", timeout=30_000)
        selector = page.locator("button").filter(has_text=_MODEL_PATTERN).first
        if _normalize_model(model) == _normalize_model(selector.inner_text()):
            return
        selector.click()
        page.wait_for_timeout(800)
        page.locator('[role="menuitemradio"], [role="menuitem"], [role="option"]').filter(
            has_text=model
        ).first.click()
        page.wait_for_timeout(500)

    def _ask(self, prompt: str, model: Optional[str], fresh: bool = False) -> str:
        self._ensure_started()
        page = self._page
        assert page is not None
        if fresh:
            self._start_new_chat(page)
        if model and model != self._model:
            self._select_model(model)
            self._model = model

        captured: dict[str, Response] = {}

        def _on_response(response: Response) -> None:
            if _CHAT_URL in response.url and response.request.method == "POST":
                captured["response"] = response

        page.on("response", _on_response)
        try:
            textarea = page.locator("textarea").first
            textarea.click()
            textarea.fill(prompt)
            page.wait_for_timeout(400)
            page.locator('button[type="submit"]').first.click()

            status = 0
            body = ""
            deadline = time.monotonic() + self._timeout
            while time.monotonic() < deadline:
                page.wait_for_timeout(300)
                response = captured.get("response")
                if response is None:
                    continue
                try:
                    raw = response.body()
                except Exception:  # noqa: BLE001 — body not yet available, keep polling
                    continue
                status = response.status
                body = _decode_body(raw)
                break
            else:
                raise DuckAIError(f"no chat response within {self._timeout:.0f}s")
        finally:
            page.remove_listener("response", _on_response)

        if status != 200:
            raise DuckAIError(_describe_error(status, body))
        return _parse_sse(body)


def _normalize_model(name: str) -> str:
    """Compare 'GPT-5.6 Luna' with the button label '5.6 Luna'."""
    return re.sub(r"[^a-z0-9]+", " ", name.lower().replace("gpt", "")).strip()


def _describe_error(status: int, body: str) -> str:
    try:
        obj = json.loads(body)
        kind = obj.get("type", "?")
        code = obj.get("overrideCode", "?")
    except (json.JSONDecodeError, AttributeError):
        return f"Duck.ai HTTP {status}: {body[:200]}"
    hint = {
        "ERR_BN_LIMIT": "headless/bot detection — headed browser required",
        "ERR_CHALLENGE": "challenge failed — check proxy/IP",
    }.get(kind, "")
    suffix = f" ({hint})" if hint else ""
    return f"Duck.ai error {kind} code {code} HTTP {status}{suffix}"


def _decode_body(raw: bytes) -> str:
    """Decode a Duck.ai SSE body to text.

    [WORKAROUND] DDG sends `text/event-stream` without a charset, so Chromium
    decodes the body as windows-1252 and `response.body()` returns the UTF-8 bytes
    of the mojibake string. Recover the real UTF-8 when the cp1252 round-trip is
    lossless; otherwise the body was already valid UTF-8.
    """
    text = raw.decode("utf-8", errors="replace")
    try:
        return text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _parse_sse(body: str) -> str:
    parts: list[str] = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        message = chunk.get("message")
        if message:
            parts.append(message)
    answer = "".join(parts).strip()
    if not answer:
        raise DuckAIError("Duck.ai returned an empty answer")
    return answer


def _teardown_xvfb() -> None:
    global _xvfb_proc
    with _xvfb_lock:
        if _xvfb_proc is not None:
            try:
                _xvfb_proc.terminate()
            except OSError:
                pass
            _xvfb_proc = None


_client: Optional[DuckAIClient] = None
_client_lock = threading.Lock()


def get_duckai_client() -> DuckAIClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = DuckAIClient()
    return _client


def duckai_ask(prompt: str, model: Optional[str] = None, fresh: bool = False) -> str:
    """Ask Duck.ai a question on a warm session. Raises DuckAIError on failure.

    Set `fresh=True` to start a brand-new chat (no context from earlier questions).
    """
    return get_duckai_client().ask(prompt, model, fresh)
