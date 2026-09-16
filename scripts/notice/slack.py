#!/usr/bin/env python3
# Status: production
# Path: systemd:devforge-slack
"""slack.py --- Slack Events API + Interactive actions for DevForge bot.

Single HTTP server handling:
  /webhooks/slack/  — Slack Events API (messages, URL verification)
  /slack/actions/   — Slack interactive block_actions (fact confirm/reject)

Caddy config needed (in /etc/caddy/Caddyfile):
  handle /webhooks/slack/* {
      reverse_proxy 127.0.0.1:8084
  }
  handle /slack/actions/* {
      reverse_proxy 127.0.0.1:8084
  }

Usage:
  python3 slack.py              # listen on 127.0.0.1:8084
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
from urllib.parse import parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from bot_processor import process as bot_process

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.db import esc_sql, psql_json, psql_ok

SECRETS: dict[str, str] = {}
_SF = Path.home() / ".config/devforge/secrets.env"
if _SF.exists():
    for _line in _SF.read_text().split("\n"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            SECRETS[_k.strip()] = _v.strip().strip('"').strip("'")

SLACK_SIGNING_SECRET = SECRETS.get("SLACK_SIGNING_SECRET_KEY", "")
SLACK_BOT_TOKEN = SECRETS.get("SLACK_BOT_TOKEN_KEY", "")
LISTEN_ADDR = os.environ.get("SLACK_LISTEN", "127.0.0.1:8084")


def _verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    """Verify Slack request signature (HMAC-SHA256)."""
    if not SLACK_SIGNING_SECRET:
        print(
            "[slack] WARNING: no SLACK_SIGNING_SECRET_KEY configured, skipping verification",
            file=sys.stderr,
            flush=True,
        )
        return True
    basestring = f"v0:{timestamp}:".encode() + body
    expected = (
        "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature)


def _slack_post(channel: str, text: str) -> bool:
    """Send a message to Slack via chat.postMessage."""
    if not SLACK_BOT_TOKEN:
        print("[slack] no SLACK_BOT_TOKEN_KEY, cannot reply", file=sys.stderr, flush=True)
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
                print(
                    f"[slack] slack API error: {result.get('error', '?')}",
                    file=sys.stderr,
                    flush=True,
                )
                return False
            return True
    except Exception as e:
        print(f"[slack] slack post failed: {e}", file=sys.stderr, flush=True)
        return False


# --- Interactive action handlers (ported from slack_interactive) ---


def _confirm_fact(fact_id: str) -> bool:
    fact = psql_json(f"""SELECT rf.id, rf.evidence,
       CASE rf.fact_type
         WHEN 'user' THEN t.user_turn
         WHEN 'thinking' THEN t.thinking
         WHEN 'text' THEN t.text
       END AS source_text,
       rf.fact_type
    FROM review_facts rf
    JOIN turns t ON t.id = rf.turn_id
    WHERE rf.id = '{esc_sql(fact_id)}'""")
    if not fact:
        return False
    if not psql_ok(
        f"UPDATE review_facts SET user_verdict='CONFIRM', user_verdict_at=NOW() WHERE id='{esc_sql(fact_id)}'"
    ):
        return False
    r = fact[0]
    ev = esc_sql(r.get("evidence", ""))
    src = esc_sql(r.get("source_text", ""))
    ft = esc_sql(r.get("fact_type", ""))
    psql_ok(f"""INSERT INTO feedback_examples (evidence_text, source_text, fact_type, verdict)
       VALUES ('{ev}', '{src}', '{ft}', 'CONFIRM')""")
    return True


def _reject_fact(fact_id: str) -> bool:
    fact = psql_json(f"""SELECT rf.id, rf.evidence,
       CASE rf.fact_type
         WHEN 'user' THEN t.user_turn
         WHEN 'thinking' THEN t.thinking
         WHEN 'text' THEN t.text
       END AS source_text,
       rf.fact_type
    FROM review_facts rf
    JOIN turns t ON t.id = rf.turn_id
    WHERE rf.id = '{esc_sql(fact_id)}'""")
    if not fact:
        return False
    if not psql_ok(
        f"UPDATE review_facts SET user_verdict='REJECT', user_verdict_at=NOW() WHERE id='{esc_sql(fact_id)}'"
    ):
        return False
    r = fact[0]
    ev = esc_sql(r.get("evidence", ""))
    src = esc_sql(r.get("source_text", ""))
    ft = esc_sql(r.get("fact_type", ""))
    psql_ok(f"""INSERT INTO feedback_examples (evidence_text, source_text, fact_type, verdict)
       VALUES ('{ev}', '{src}', '{ft}', 'REJECT')""")
    return True


def _skip_turn(turn_id: str) -> bool:
    turn = psql_json(f"""SELECT id::text,
       COALESCE(user_turn, '') AS user_turn,
       COALESCE(text, '') AS text
    FROM turns WHERE id = '{esc_sql(turn_id)}'::uuid""")
    if not turn:
        return False
    r = turn[0]
    ev = esc_sql((r.get("user_turn") or "").strip()[:200])
    src = esc_sql((r.get("text") or "").strip()[:200])
    psql_ok(f"""INSERT INTO feedback_examples (evidence_text, source_text, fact_type, verdict)
       VALUES ('{ev}', '{src}', 'turn', 'REJECT')
       ON CONFLICT DO NOTHING""")
    return True


def _admit_turn(turn_id: str) -> bool:
    turn = psql_json(f"""SELECT id::text,
       COALESCE(user_turn, '') AS user_turn,
       COALESCE(text, '') AS text
    FROM turns WHERE id = '{esc_sql(turn_id)}'::uuid""")
    if not turn:
        return False
    r = turn[0]
    ev = esc_sql((r.get("user_turn") or "").strip()[:200])
    src = esc_sql((r.get("text") or "").strip()[:200])
    psql_ok(f"""INSERT INTO feedback_examples (evidence_text, source_text, fact_type, verdict)
       VALUES ('{ev}', '{src}', 'turn', 'CONFIRM')
       ON CONFLICT DO NOTHING""")
    psql_ok(f"UPDATE turns SET pipeline_state = 'scanned' WHERE id = '{esc_sql(turn_id)}'::uuid")
    return True


def _build_resolved_block(original_section: list, verdict: str, fact_id: str) -> dict:
    resolved_text = (
        f"*{':white_check_mark:' if verdict == 'CONFIRM' else ':x:'} {verdict}* ({fact_id[:12]}...)"
    )
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": resolved_text}]}


class SlackHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("content-length") or "0")
        body = self.rfile.read(length) if length > 0 else b""
        path = self.path

        # Verify Slack signature (common to all routes)
        utc_timestamp = self.headers.get("X-Slack-Request-Timestamp", "")
        sig = self.headers.get("X-Slack-Signature", "")
        if not _verify_slack_signature(body, utc_timestamp, sig):
            self._respond(401, {"error": "invalid signature"})
            return

        # Interactive actions (block_actions from button clicks, form-encoded)
        if path.startswith("/slack/actions"):
            self._handle_interactive(body)
            return

        # Events API — always JSON body
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            self._respond(400, {"error": "invalid JSON"})
            return

        # URL verification (Slack setup handshake)
        if payload.get("type") == "url_verification":
            self._handle_url_verification(payload)
            return

        # Event callback — messages from users
        if payload.get("type") == "event_callback":
            self._handle_event_callback(payload)
            return

        self._respond(200, {"ok": True})

    def _handle_url_verification(self, payload: dict):
        challenge = payload.get("challenge", "")
        self._respond(200, {"challenge": challenge})
        print(
            f"[slack] url_verification ok (challenge={challenge[:20]}...)",
            file=sys.stderr,
            flush=True,
        )

    def _handle_event_callback(self, payload: dict):
        event = payload.get("event", {})
        event_type = event.get("type", "")

        if event_type == "message" and not event.get("bot_id"):
            text = event.get("text", "").strip()
            channel = event.get("channel", "")
            user = event.get("user", "")

            if text:
                print(f"[slack] from={user} text={text[:60]}", file=sys.stderr, flush=True)
                resp = bot_process(text)
                if resp:
                    _slack_post(channel, resp)
                    print(f"[slack] replied ({len(resp)} chars)", file=sys.stderr, flush=True)

        self._respond(200, {"ok": True})

    def _handle_interactive(self, raw_body: bytes):
        body_str = raw_body.decode("utf-8")
        params = parse_qs(body_str)
        payload_str = params.get("payload", [None])[0]
        if not payload_str:
            self._respond(400, "missing payload")
            return

        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError:
            self._respond(400, "invalid json")
            return

        if payload.get("type") != "block_actions":
            self._respond(200, "ok")
            return

        actions = payload.get("actions", [])
        if not actions:
            self._respond(200, "ok")
            return

        action = actions[0]
        action_id = action.get("action_id", "")
        value = action.get("value", "")
        fact_id = value.split(":", 1)[-1] if ":" in value else value

        channel = payload.get("channel", {}).get("id", "")
        msg_ts = payload.get("message", {}).get("ts", "")
        original_blocks = payload.get("message", {}).get("blocks", [])

        verdict = None
        if action_id == "fact_confirm":
            if _confirm_fact(fact_id):
                verdict = "CONFIRM"
                print(f"  CONFIRMED {fact_id[:12]}... via Slack")
        elif action_id == "fact_reject":
            if _reject_fact(fact_id):
                verdict = "REJECT"
                print(f"  REJECTED {fact_id[:12]}... via Slack")
        elif action_id == "noise_skip":
            target = value.split(":", 1)[-1]
            if _skip_turn(target):
                verdict = "SKIP"
                print(f"  SKIP turn {target[:12]}... via Slack")
        elif action_id == "noise_admit":
            target = value.split(":", 1)[-1]
            if _admit_turn(target):
                verdict = "ADMIT"
                print(f"  ADMIT turn {target[:12]}... via Slack")

        if verdict and channel and msg_ts:
            updated_blocks = []
            for block in original_blocks:
                if block.get("type") == "actions" and any(
                    e.get("value", "").endswith(fact_id) for e in block.get("elements", [])
                ):
                    updated_blocks.append(_build_resolved_block(block, verdict, fact_id))
                else:
                    updated_blocks.append(block)
            _slack_post(
                "chat.update",
                {
                    "channel": channel,
                    "ts": msg_ts,
                    "text": f"Fact {verdict}: {fact_id[:12]}...",
                    "blocks": updated_blocks,
                },
            )

        self._respond(200, "ok")

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
        print(f"[slack] {self.address_string()} - {fmt % args}", file=sys.stderr, flush=True)


def main():
    host, port_text = LISTEN_ADDR.rsplit(":", 1)
    port = int(port_text)
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((host, port), SlackHandler)
    print(
        f"[slack] listening on {host}:{port} (Events API + Interactive)",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
