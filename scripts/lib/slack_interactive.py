#!/usr/bin/env python3
# Status: experimental
# Path: systemd:devforge-slack-interactive.service — Slack interactive button handler
"""Slack Interactive Handler — receives Slack block_actions payload and processes NEUTRAL fact confirm/reject.

Runs as HTTP server on 127.0.0.1:8087. Caddy proxies /slack/actions -> localhost:8087.

Environment (from secrets.env):
  SLACK_SIGNING_SECRET - for request verification
  SLACK_BOT_TOKEN - for chat.update and chat.postMessage
"""

import hashlib
import hmac
import json
import os
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs
from urllib.request import Request, urlopen

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql_ok, esc_sql, psql_json

PORT = 8087

def _load_secrets():
    secrets_path = os.path.expanduser("~/.config/devforge/secrets.env")
    if os.path.exists(secrets_path):
        with open(secrets_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    return (
        os.environ.get("SLACK_SIGNING_SECRET", ""),
        os.environ.get("SLACK_BOT_TOKEN", ""),
        os.environ.get("SLACK_CHANNEL", "U0APJGD8CBW"),
    )

SIGNING_SECRET, BOT_TOKEN, SLACK_CHANNEL = _load_secrets()


def _verify_signature(timestamp: str, body: str, signature: str) -> bool:
    if not SIGNING_SECRET:
        return True
    basestring = f"v0:{timestamp}:{body}".encode()
    sig = "v0=" + hmac.new(SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, signature)


def _slack_post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = Request(
        f"https://slack.com/api/{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {BOT_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  [slack] {path} failed: {e}")
        return {"ok": False}


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
    if not psql_ok(f"UPDATE review_facts SET user_verdict='CONFIRM', user_verdict_at=NOW() WHERE id='{esc_sql(fact_id)}'"):
        return False
    r = fact[0]
    ev = esc_sql(r.get('evidence', ''))
    src = esc_sql(r.get('source_text', ''))
    ft = esc_sql(r.get('fact_type', ''))
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
    if not psql_ok(f"UPDATE review_facts SET user_verdict='REJECT', user_verdict_at=NOW() WHERE id='{esc_sql(fact_id)}'"):
        return False
    r = fact[0]
    ev = esc_sql(r.get('evidence', ''))
    src = esc_sql(r.get('source_text', ''))
    ft = esc_sql(r.get('fact_type', ''))
    psql_ok(f"""INSERT INTO feedback_examples (evidence_text, source_text, fact_type, verdict)
       VALUES ('{ev}', '{src}', '{ft}', 'REJECT')""")
    return True


# ── Extract fail turn: noise skip/admit ───────────────────────────────

def _skip_turn(turn_id: str) -> bool:
    """Mark turn as noise: insert REJECT into feedback_examples."""
    turn = psql_json(f"""SELECT id::text,
       COALESCE(user_turn, '') AS user_turn,
       COALESCE(text, '') AS text
    FROM turns WHERE id = '{esc_sql(turn_id)}'::uuid""")
    if not turn:
        return False
    r = turn[0]
    ev = esc_sql((r.get('user_turn') or '').strip()[:200])
    src = esc_sql((r.get('text') or '').strip()[:200])
    psql_ok(f"""INSERT INTO feedback_examples (evidence_text, source_text, fact_type, verdict)
       VALUES ('{ev}', '{src}', 'turn', 'REJECT')
       ON CONFLICT DO NOTHING""")
    return True


def _admit_turn(turn_id: str) -> bool:
    """Mark turn as valid: insert CONFIRM into feedback_examples, reset to scanned for re-extract."""
    turn = psql_json(f"""SELECT id::text,
       COALESCE(user_turn, '') AS user_turn,
       COALESCE(text, '') AS text
    FROM turns WHERE id = '{esc_sql(turn_id)}'::uuid""")
    if not turn:
        return False
    r = turn[0]
    ev = esc_sql((r.get('user_turn') or '').strip()[:200])
    src = esc_sql((r.get('text') or '').strip()[:200])
    psql_ok(f"""INSERT INTO feedback_examples (evidence_text, source_text, fact_type, verdict)
       VALUES ('{ev}', '{src}', 'turn', 'CONFIRM')
       ON CONFLICT DO NOTHING""")
    # Reset to scanned for re-extraction
    psql_ok(f"UPDATE turns SET pipeline_state = 'scanned' WHERE id = '{esc_sql(turn_id)}'::uuid")
    return True


def _build_resolved_block(original_section: list, verdict: str, fact_id: str) -> dict:
    resolved_text = f"*{':white_check_mark:' if verdict == 'CONFIRM' else ':x:'} {verdict}* ({fact_id[:12]}...)"
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": resolved_text}]}


class SlackActionHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8")

        ts = self.headers.get("X-Slack-Request-Timestamp", "")
        sig = self.headers.get("X-Slack-Signature", "")
        if not _verify_signature(ts, body, sig):
            self._respond(401, "invalid signature")
            return

        params = parse_qs(body)
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
            # Update all fact sections: disable buttons by replacing actions with context
            updated_blocks = []
            for block in original_blocks:
                if block.get("type") == "actions" and any(
                    e.get("value", "").endswith(fact_id)
                    for e in block.get("elements", [])
                ):
                    updated_blocks.append(
                        _build_resolved_block(block, verdict, fact_id)
                    )
                else:
                    updated_blocks.append(block)
            _slack_post("chat.update", {
                "channel": channel, "ts": msg_ts,
                "text": f"Fact {verdict}: {fact_id[:12]}...",
                "blocks": updated_blocks,
            })

        self._respond(200, "ok")

    def _respond(self, code: int, text: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode())


def send_neutral_alert():
    """Query pending NEUTRAL facts and send interactive Slack message."""
    rows = psql_json(
        "SELECT id::text, left(evidence, 120) AS evidence, fact_type, "
        "  to_char(created_at AT TIME ZONE 'Asia/Seoul', 'MM/DD HH24:MI') AS kst "
        "FROM review_facts "
        "WHERE source='extract_pipeline' AND nli_llm='NEUTRAL' AND user_verdict IS NULL "
        "  AND created_at > now() - interval '10 hours' "
        "ORDER BY created_at DESC LIMIT 10",
        timeout=10,
    )
    if not rows:
        print("[slack_interactive] No pending NEUTRAL facts")
        return

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"NEUTRAL Facts: {len(rows)}건 검토 필요", "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "아래 fact들을 검토 후 CONFIRM/REJECT를 선택하세요.\n승인 내용은 enrich few-shot으로 학습됩니다.",
            },
        },
        {"type": "divider"},
    ]

    for r in rows[:5]:
        ev = (r.get("evidence") or "")[:80]
        ft = r.get("fact_type", "?")
        fid = r["id"]
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*[{ft}]* {ev}  `:{fid[:12]}`"},
        })
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "CONFIRM", "emoji": True},
                    "style": "primary",
                    "value": f"c:{fid}",
                    "action_id": "fact_confirm",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "REJECT", "emoji": True},
                    "style": "danger",
                    "value": f"r:{fid}",
                    "action_id": "fact_reject",
                },
            ],
        })

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"CLI: `cli.py fact list --pending` | {len(rows)}건 중 상위 5건 표시"}],
    })

    result = _slack_post("chat.postMessage", {
        "channel": SLACK_CHANNEL,
        "text": f"{len(rows)} NEUTRAL facts pending",
        "blocks": blocks,
    })
    if result.get("ok"):
        print(f"[slack_interactive] Alert sent: {len(rows)} NEUTRAL facts")
    else:
        print(f"[slack_interactive] Alert FAILED: {result.get('error', '?')}")


_EXTRACT_FAIL_REPORT = Path("/var/tmp/extract_fail_report.json")


def send_extract_fail_alert():
    """Read extract fail report and send interactive Slack message with noise classification buttons."""
    if not _EXTRACT_FAIL_REPORT.exists():
        print("[slack_interactive] No extract fail report found")
        return

    report = json.loads(_EXTRACT_FAIL_REPORT.read_text())
    fails = report.get("failures", [])
    noise = report.get("noise", [])
    if not fails and not noise:
        print("[slack_interactive] Extract fail report empty")
        return

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Extract 결과: {len(fails)}건 실패 / {len(noise)}건 noise", "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "실패한 turn들을 검토 후 Skip(noise)/Admit(재추출)을 선택하세요.\nAdmit 선택 시 다음 cycle에서 재추출됩니다.",
            },
        },
        {"type": "divider"},
    ]

    for entry in (fails + noise)[:8]:
        tid = entry.get("turn_id", "?")
        reason = entry.get("reason", "?")
        preview = entry.get("preview", "")[:100]
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*[`{tid[:12]}`]* {reason}\n> {preview}"},
        })
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Skip (noise)", "emoji": True},
                    "style": "danger",
                    "value": f"s:{tid}",
                    "action_id": "noise_skip",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Admit (재추출)", "emoji": True},
                    "style": "primary",
                    "value": f"a:{tid}",
                    "action_id": "noise_admit",
                },
            ],
        })

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"총 {len(fails)+len(noise)}건 중 상위 8개 표시 | 실패 보고서: {_EXTRACT_FAIL_REPORT}"}],
    })

    result = _slack_post("chat.postMessage", {
        "channel": SLACK_CHANNEL,
        "text": f"Extract: {len(fails)} failed, {len(noise)} noise",
        "blocks": blocks,
    })
    if result.get("ok"):
        print(f"[slack_interactive] Extract fail alert sent: {len(fails)} fails, {len(noise)} noise")
    else:
        print(f"[slack_interactive] Extract fail alert FAILED: {result.get('error', '?')}")


def run_server():
    server = HTTPServer(("127.0.0.1", PORT), SlackActionHandler)
    print(f"[slack_interactive] Listening on :{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[slack_interactive] Shutting down")
        server.server_close()


if __name__ == "__main__":
    if "--send-alert" in sys.argv:
        send_neutral_alert()
    elif "--send-extract-fail" in sys.argv:
        send_extract_fail_alert()
    else:
        run_server()
