#!/usr/bin/env python3.11
# Status: experimental
# Path: systemd:devforge-fastapi.service
"""DevForge FastAPI — notification hub (Slack + Telegram + Email) + Blob Explorer.

Routes:
  /webhooks/slack   — Slack Events API (JSON POST)
  /slack/actions    — Slack Interactive (form-encoded POST)
  /health           — health check

Background (asyncio tasks via lifespan):
  Telegram polling loop
  Blob Explorer (Azure Blob HTTP file server, ThreadingHTTPServer)

Usage:
  python3 fastapi/app.py                    # default :8002
  python3 fastapi/app.py --port 8003        # custom port
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from lib.notify import Notifier
from mcp_server import mcp
from devforge_fastapi.review_dashboard import router as review_router
from devforge_fastapi.calendar_sync import router as calendar_router
from fastmcp.utilities.lifespan import combine_lifespans

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("devforge-fastapi")

app = FastAPI(title="DevForge FastAPI")

# Mount FastMCP at /mcp using the official http_app pattern.
# path="/" so /mcp/ → sub-app sees / → matches.
mcp_app = mcp.http_app(path="/")
app.mount("/mcp", mcp_app)

# ── Secrets ──────────────────────────────────────────────────

_SECRETS: dict[str, str] = {}
_SF = Path.home() / ".config/devforge/secrets.env"
if _SF.exists():
    for _line in _SF.read_text().split("\n"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _SECRETS[_k.strip()] = _v.strip().strip('"').strip("'")
else:
    # Container runtime: env vars injected via EnvironmentFile=
    _KEYS = [
        "SLACK_SIGNING_SECRET",
        "SLACK_BOT_TOKEN",
        "TELEGRAM_TOKEN",
        "TELEGRAM_CHAT_ID",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
    ]
    for _k in _KEYS:
        _v = os.environ.get(_k, "")
        if _v:
            _SECRETS[_k] = _v

SLACK_SIGNING_SECRET = _SECRETS.get("SLACK_SIGNING_SECRET", "")
SLACK_BOT_TOKEN = _SECRETS.get("SLACK_BOT_TOKEN", "")
_TG_TOKEN = _SECRETS.get("TELEGRAM_TOKEN", "")
if not _TG_TOKEN:
    logger.info("Telegram disabled (no TELEGRAM_TOKEN)")
    TG_BASE = ""
else:
    TG_BASE = f"https://api.telegram.org/bot{_TG_TOKEN}"

# Notifier: Apprise-based Telegram + Email, native Slack
_notifier = Notifier(_SECRETS)


# ── Slack utilities ─────────────────────────────────────────


def _verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    if not SLACK_SIGNING_SECRET:
        logger.warning("No SLACK_SIGNING_SECRET configured, skipping verification")
        return True
    basestring = f"v0:{timestamp}:".encode() + body
    expected = (
        "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature)


def _slack_confirm_fact(fact_id: str) -> bool:
    from lib.db import esc_sql, psql_json, psql_ok

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


def _slack_reject_fact(fact_id: str) -> bool:
    from lib.db import esc_sql, psql_json, psql_ok

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


def _slack_skip_turn(turn_id: str) -> bool:
    from lib.db import esc_sql, psql_json, psql_ok

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


def _slack_admit_turn(turn_id: str) -> bool:
    from lib.db import esc_sql, psql_json, psql_ok

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


def _slack_build_resolved_block(original_section: list, verdict: str, fact_id: str) -> dict:
    resolved_text = (
        f"*{':white_check_mark:' if verdict == 'CONFIRM' else ':x:'} {verdict}* ({fact_id[:12]}...)"
    )
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": resolved_text}]}


# ── Telegram ─────────────────────────────────────────────────


_tg_seen_ids: set[int] = set()
_tg_bot_processor = None


def _get_bot_processor():
    global _tg_bot_processor
    if _tg_bot_processor is None:
        from notice.bot_processor import process as bp

        _tg_bot_processor = bp
    return _tg_bot_processor


async def _telegram_poll_loop():
    if not TG_BASE:
        logger.info("Telegram polling disabled (no TOKEN)")
        return

    _http = httpx.AsyncClient(timeout=35)
    offset = 0
    tg_token = _SECRETS.get("TELEGRAM_TOKEN", "")
    tg_chat = _SECRETS.get("TELEGRAM_CHAT_ID", "")

    # Delete any existing webhook — polling and webhook can't coexist
    try:
        wh_resp = await _http.post(f"{TG_BASE}/deleteWebhook")
        wh_data = wh_resp.json()
        logger.info("Telegram deleteWebhook: %s", wh_data.get("description", wh_data))
    except Exception as wh_e:
        logger.warning("Telegram deleteWebhook failed: %s", wh_e)

    logger.info("Telegram polling started")
    try:
        while True:
            try:
                resp = await _http.post(
                    f"{TG_BASE}/getUpdates",
                    json={"timeout": 30, "offset": offset, "allowed_updates": ["message"]},
                    timeout=35,
                )
                data = resp.json()
                if not data.get("ok"):
                    err = str(data.get("error", ""))
                    if (
                        "conflict" not in err.lower()
                        and "time" not in err.lower()
                        and "read" not in err.lower()
                    ):
                        logger.warning("Telegram getUpdates error: %s", err)
                    await asyncio.sleep(5)
                    continue

                for upd in data.get("result", []):
                    uid = upd.get("update_id", 0)
                    if uid in _tg_seen_ids:
                        offset = uid + 1
                        continue
                    _tg_seen_ids.add(uid)
                    offset = uid + 1

                    msg = upd.get("message", {})
                    cid = str(msg.get("chat", {}).get("id", ""))
                    txt = msg.get("text", "")

                    if cid != tg_chat or not txt:
                        continue

                    logger.info("[tg] %s", txt[:100])
                    processor = _get_bot_processor()
                    resp_text = processor(txt)
                    if resp_text:
                        await _telegram_send(resp_text)
                        logger.info("[tg] replied (%d chars)", len(resp_text))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Telegram poll error: %s", e)
            await asyncio.sleep(3)
    finally:
        await _http.aclose()
        logger.info("Telegram polling stopped")


async def _telegram_send(text: str) -> bool:
    if not TG_BASE:
        return False
    return _notifier.send_telegram(text)


# ── Lifespan ─────────────────────────────────────────────────


async def _blob_server_task():
    """Run Blob Explorer ThreadingHTTPServer in a background thread."""
    from http.server import ThreadingHTTPServer

    from blob_explorer.handler import BlobHandler

    host = os.environ.get("BLOB_EXPLORER_LISTEN", "127.0.0.1:8085").rsplit(":", 1)
    server = ThreadingHTTPServer((host[0], int(host[1])), BlobHandler)
    logger.info("Blob Explorer listening on %s:%s", host[0], host[1])

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, server.serve_forever)


@asynccontextmanager
async def app_lifespan(app_inst: FastAPI):
    tg_task = asyncio.create_task(_telegram_poll_loop())
    blob_task = asyncio.create_task(_blob_server_task())
    try:
        yield
    finally:
        tg_task.cancel()
        blob_task.cancel()
        for t in (tg_task, blob_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


app.router.lifespan_context = combine_lifespans(app_lifespan, mcp_app.lifespan)


# ── Health ────────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok", "server": "devforge-fastapi"}


# ── Review dashboard ─────────────────────────────────────────
app.include_router(review_router)

# ── Calendar sync ──────────────────────────────────────────────
app.include_router(calendar_router)


# ── Slack routes ─────────────────────────────────────────────


@app.post("/webhooks/slack")
async def slack_events(request: Request):
    body = await request.body()
    utc_timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    if not _verify_slack_signature(body, utc_timestamp, sig):
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    if payload.get("type") == "url_verification":
        logger.info("url_verification ok")
        return JSONResponse({"challenge": payload.get("challenge", "")})

    if payload.get("type") == "event_callback":
        event = payload.get("event", {})
        if event.get("type") == "message" and not event.get("bot_id"):
            text = event.get("text", "").strip()
            channel = event.get("channel", "")
            user = event.get("user", "")
            if text:
                logger.info("[slack] from=%s text=%s", user, text[:60])
                processor = _get_bot_processor()
                resp = processor(text)
                if resp:
                    _notifier.send_slack(channel, resp)
                    logger.info("[slack] replied (%d chars)", len(resp))

    return JSONResponse({"ok": True})


@app.post("/slack/actions")
async def slack_actions(request: Request):
    body = await request.body()
    utc_timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    if not _verify_slack_signature(body, utc_timestamp, sig):
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    body_str = body.decode("utf-8")
    params = parse_qs(body_str)
    payload_str = params.get("payload", [None])[0]
    if not payload_str:
        return JSONResponse({"error": "missing payload"}, status_code=400)

    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if payload.get("type") != "block_actions":
        return JSONResponse({"ok": True})

    actions = payload.get("actions", [])
    if not actions:
        return JSONResponse({"ok": True})

    action = actions[0]
    action_id = action.get("action_id", "")
    value = action.get("value", "")
    fact_id = value.split(":", 1)[-1] if ":" in value else value

    channel = payload.get("channel", {}).get("id", "")
    msg_ts = payload.get("message", {}).get("ts", "")
    original_blocks = payload.get("message", {}).get("blocks", [])

    verdict = None
    if action_id == "fact_confirm":
        if _slack_confirm_fact(fact_id):
            verdict = "CONFIRM"
            logger.info("  CONFIRMED %s... via Slack", fact_id[:12])
    elif action_id == "fact_reject":
        if _slack_reject_fact(fact_id):
            verdict = "REJECT"
            logger.info("  REJECTED %s... via Slack", fact_id[:12])
    elif action_id == "noise_skip":
        target = value.split(":", 1)[-1]
        if _slack_skip_turn(target):
            verdict = "SKIP"
            logger.info("  SKIP turn %s... via Slack", target[:12])
    elif action_id == "noise_admit":
        target = value.split(":", 1)[-1]
        if _slack_admit_turn(target):
            verdict = "ADMIT"
            logger.info("  ADMIT turn %s... via Slack", target[:12])

    if verdict and channel and msg_ts:
        updated_blocks = []
        for block in original_blocks:
            if block.get("type") == "actions" and any(
                e.get("value", "").endswith(fact_id) for e in block.get("elements", [])
            ):
                updated_blocks.append(_slack_build_resolved_block(block, verdict, fact_id))
            else:
                updated_blocks.append(block)
        _notifier.slack_update(
            channel,
            msg_ts,
            f"Fact {verdict}: {fact_id[:12]}...",
            updated_blocks,
        )

    return JSONResponse({"ok": True})


# ── Entry point ─────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="DevForge FastAPI Server")
    parser.add_argument("--port", "-p", type=int, default=8002, help="Port (default: 8002)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host (default: 127.0.0.1)")
    args = parser.parse_args()
    logger.info("Starting DevForge FastAPI on %s:%d", args.host, args.port)
    uvicorn.run("fastapi.app:app", host=args.host, port=args.port, log_level="info", reload=True)


if __name__ == "__main__":
    main()
