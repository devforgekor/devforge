#!/usr/bin/env python3
# Status: production
# Path: systemd:devforge-webhook
"""webhook_server.py --- Slack Events API receiver for DevForge bot.

Receives Slack Events via Caddy (TLS termination), processes messages
through bot_processor (Qwen2.5-Coder-3B), and sends responses via Slack API.

Caddy config needed (in /etc/caddy/Caddyfile):
  handle /webhooks/slack/* {
      reverse_proxy 127.0.0.1:8084
  }

Usage:
  python3 webhook_server.py              # listen on 127.0.0.1:8084
  python3 webhook_server.py --daemon     # same (default)
"""

import hashlib
import hmac
import json
import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.bot_processor import process as bot_process


SECRETS: dict[str, str] = {}
_SF = Path.home() / ".config/devforge/secrets.env"
if _SF.exists():
    for _line in _SF.read_text().split("\n"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            SECRETS[_k.strip()] = _v.strip().strip('"').strip("'")

SLACK_SIGNING_SECRET = SECRETS.get("SLACK_SIGNING_SECRET", "")
SLACK_BOT_TOKEN = SECRETS.get("SLACK_BOT_TOKEN", "")
LISTEN_ADDR = os.environ.get("WEBHOOK_LISTEN", "127.0.0.1:8084")


def _verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    """Verify Slack request signature (HMAC-SHA256)."""
    if not SLACK_SIGNING_SECRET:
        print("[webhook] WARNING: no SLACK_SIGNING_SECRET configured, skipping verification", file=sys.stderr, flush=True)
        return True
    basestring = f"v0:{timestamp}:".encode() + body
    expected = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _slack_post(channel: str, text: str) -> bool:
    """Send a message to Slack via chat.postMessage."""
    if not SLACK_BOT_TOKEN:
        print("[webhook] no SLACK_BOT_TOKEN, cannot reply", file=sys.stderr, flush=True)
        return False
    payload = json.dumps({"channel": channel, "text": text, "mrkdwn": True}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                print(f"[webhook] slack API error: {result.get('error', '?')}", file=sys.stderr, flush=True)
                return False
            return True
    except Exception as e:
        print(f"[webhook] slack post failed: {e}", file=sys.stderr, flush=True)
        return False


class SlackHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("content-length") or "0")
        body = self.rfile.read(length) if length > 0 else b""

        # Verify Slack signature
        ts = self.headers.get("X-Slack-Request-Timestamp", "")
        sig = self.headers.get("X-Slack-Signature", "")
        if not _verify_slack_signature(body, ts, sig):
            self._respond(401, {"error": "invalid signature"})
            return

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            self._respond(400, {"error": "invalid JSON"})
            return

        # URL verification (Slack setup handshake)
        if payload.get("type") == "url_verification":
            challenge = payload.get("challenge", "")
            self._respond(200, {"challenge": challenge})
            print(f"[webhook] url_verification ok (challenge={challenge[:20]}...)", file=sys.stderr, flush=True)
            return

        # Event callback
        if payload.get("type") == "event_callback":
            event = payload.get("event", {})
            event_type = event.get("type", "")

            # Only process message events (not bot's own messages)
            if event_type == "message" and not event.get("bot_id"):
                text = event.get("text", "").strip()
                channel = event.get("channel", "")
                user = event.get("user", "")

                if text:
                    print(f"[webhook] from={user} text={text[:60]}", file=sys.stderr, flush=True)
                    resp = bot_process(text)
                    if resp:
                        _slack_post(channel, resp)
                        print(f"[webhook] replied ({len(resp)} chars)", file=sys.stderr, flush=True)

            self._respond(200, {"ok": True})
            return

        # Fallback
        self._respond(200, {"ok": True})

    def _respond(self, status: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except Exception:
            pass

    def log_message(self, fmt: str, *args: Any):
        print(f"[webhook] {self.address_string()} - {fmt % args}", file=sys.stderr, flush=True)


def main():
    host, port_text = LISTEN_ADDR.rsplit(":", 1)
    port = int(port_text)
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((host, port), SlackHandler)
    print(f"[webhook] listening on {host}:{port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
