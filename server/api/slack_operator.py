"""slack_operator.py — Slack Slash Command interactive operator for DevForge.

State machine: MENU -> TEST_SUBMENU -> TEST_START_MODEL -> WAITING_RESULT
                       -> LOG_LINES
"""
import glob
import hashlib
import hmac
import json
import logging
import os
import signal

import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_API = "https://slack.com/api"

PID_DIR = Path("/var/tmp")
LOG_GLOB = "/var/tmp/batch_*.log"
RESULTS_GLOB = "/opt/projects/server/batch_results/*.json"
# ── state machine ────────────────────────────────────────────────────
_interaction_state: Dict[str, dict] = {}
STATE_TTL = 1800  # 30 min


def _state_key(user_id: str, channel_id: str) -> str:
    return f"{user_id}:{channel_id}"


def _get_state(user_id: str, channel_id: str) -> dict:
    _prune_expired()
    key = _state_key(user_id, channel_id)
    if key not in _interaction_state:
        _interaction_state[key] = {"state": "MENU", "ts": time.time(), "context": {}}
    return _interaction_state[key]


def _prune_expired():
    now = time.time()
    expired = [k for k, v in _interaction_state.items() if now - v["ts"] > STATE_TTL]
    for k in expired:
        del _interaction_state[k]


# ── signature verification (adapted from seedling slack.py:36-44) ────
def verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    if not SLACK_SIGNING_SECRET:
        return True
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), base, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── slack messaging ──────────────────────────────────────────────────
def _post_to_response_url(response_url: str, payload: dict):
    """Immediate response via Slack response_url (no auth needed)."""
    if not response_url:
        return
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        response_url, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logger.error("response_url failed: %s", e)


def _post_to_slack(channel: str, text: str):
    """Send a message to a Slack channel via chat.postMessage (Bot Token)."""
    if not SLACK_BOT_TOKEN:
        logger.warning("SLACK_BOT_TOKEN not configured")
        return
    payload = json.dumps({"channel": channel, "text": text, "mrkdwn": True}).encode()
    req = urllib.request.Request(
        f"{SLACK_API}/chat.postMessage", data=payload,
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.error("chat.postMessage failed: %s", e)


def _update_slack_message(channel: str, ts: str, blocks: list, text: str = ""):
    """Update an existing message via chat.update."""
    if not SLACK_BOT_TOKEN:
        return
    payload = json.dumps({
        "channel": channel, "ts": ts,
        "blocks": blocks, "text": text,
    }).encode()
    req = urllib.request.Request(
        f"{SLACK_API}/chat.update", data=payload,
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.error("chat.update failed: %s", e)


# ── block builders ───────────────────────────────────────────────────
def _truncate(text: str, max_chars: int = 2800) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... (truncated)"


def _build_menu_blocks() -> dict:
    return {
        "response_type": "ephemeral",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*DevForge*"}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Status"},
                 "value": "status", "action_id": "df_status"},
                {"type": "button", "text": {"type": "plain_text", "text": "Test"},
                 "value": "test_menu", "action_id": "df_test_menu"},
                {"type": "button", "text": {"type": "plain_text", "text": "Log"},
                 "value": "log_menu", "action_id": "df_log_menu"},
            ]},
        ],
    }


def _build_test_menu_blocks() -> dict:
    return {
        "replace_original": True,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Test*"}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Start"},
                 "value": "test_start", "action_id": "df_test_start"},
                {"type": "button", "text": {"type": "plain_text", "text": "Result"},
                 "value": "test_result", "action_id": "df_test_result"},
                {"type": "button", "text": {"type": "plain_text", "text": "Stop"},
                 "value": "test_stop", "action_id": "df_test_stop"},
                {"type": "button", "text": {"type": "plain_text", "text": "Back"},
                 "value": "menu", "action_id": "df_back"},
            ]},
        ],
    }


def _build_model_select_blocks() -> dict:
    return {
        "replace_original": True,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Model*"}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "1. StarCoder2 15B"},
                 "value": "starcoder2_15b", "action_id": "df_model_select"},
                {"type": "button", "text": {"type": "plain_text", "text": "2. DeepSeek Coder V2 Lite"},
                 "value": "deepseek_coder_v2_lite", "action_id": "df_model_select"},
                {"type": "button", "text": {"type": "plain_text", "text": "3. Qwen Coder 14B"},
                 "value": "qwen_coder_14b", "action_id": "df_model_select"},
                {"type": "button", "text": {"type": "plain_text", "text": "4. Qwen Coder 7B"},
                 "value": "qwen_coder_7b", "action_id": "df_model_select"},
                {"type": "button", "text": {"type": "plain_text", "text": "Back"},
                 "value": "test_menu", "action_id": "df_back"},
            ]},
        ],
    }


def _build_log_lines_blocks() -> dict:
    return {
        "replace_original": True,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Log lines*"}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "10"},
                 "value": "log_10", "action_id": "df_log_lines"},
                {"type": "button", "text": {"type": "plain_text", "text": "20"},
                 "value": "log_20", "action_id": "df_log_lines"},
                {"type": "button", "text": {"type": "plain_text", "text": "50"},
                 "value": "log_50", "action_id": "df_log_lines"},
                {"type": "button", "text": {"type": "plain_text", "text": "100"},
                 "value": "log_100", "action_id": "df_log_lines"},
                {"type": "button", "text": {"type": "plain_text", "text": "Back"},
                 "value": "menu", "action_id": "df_back"},
            ]},
        ],
    }


# ── command execution ────────────────────────────────────────────────
def _latest_log_file() -> Optional[str]:
    files = glob.glob(LOG_GLOB)
    return max(files, key=os.path.getmtime) if files else None


def _latest_result_file() -> Optional[str]:
    files = glob.glob(RESULTS_GLOB)
    return max(files, key=os.path.getmtime) if files else None


def _pid_file(user_id: str) -> Path:
    ts = str(int(time.time()))
    return PID_DIR / f"batch_{user_id}_{ts}.pid"


def _find_pid_file() -> Optional[Path]:
    files = glob.glob(str(PID_DIR / "batch_*.pid"))
    return Path(max(files, key=os.path.getmtime)) if files else None


def cmd_status() -> str:
    log = _latest_log_file()
    if not log:
        return "No batch test log found."
    try:
        lines = open(log).readlines()
        last = "".join(lines[-8:]) if len(lines) > 8 else "".join(lines)
        return _truncate(f"```{last}```")
    except Exception as e:
        return f"Log read error: {e}"


def cmd_test_stop(user_id: str) -> str:
    pf = _find_pid_file()
    if not pf:
        return "No running test found."
    try:
        pid = int(pf.read_text().strip())
        if user_id not in pf.name:
            return f"PID {pid} started by another user. Use SSH."
        os.kill(pid, signal.SIGTERM)
        pf.unlink(missing_ok=True)
        return f"Stopped (PID {pid})."
    except ProcessLookupError:
        pf.unlink(missing_ok=True)
        return "Process already gone. Cleaned up."
    except Exception as e:
        return f"Stop failed: {e}"


def cmd_test_result() -> str:
    rf = _latest_result_file()
    if not rf:
        return "No test result found."
    try:
        data = json.loads(open(rf).read())
        if isinstance(data, list):
            entries = len(data)
            facts = sum(len(e.get("facts", [])) for e in data)
            return f"Result: {entries} entries, {facts} facts (file: {os.path.basename(rf)})"
        return _truncate(f"```{json.dumps(data, indent=2)[:2500]}```")
    except Exception as e:
        return f"Result read error: {e}"


def cmd_log(n_lines: int) -> str:
    log = _latest_log_file()
    if not log:
        return "No log file found."
    try:
        lines = open(log).readlines()
        last = "".join(lines[-n_lines:]) if len(lines) > n_lines else "".join(lines)
        return _truncate(f"```{last}```")
    except Exception as e:
        return f"Log read error: {e}"


# ── state advancement ────────────────────────────────────────────────
def _advance(state: dict, action_id: str, action_value: str, user_id: str) -> tuple:
    """Returns (response_text, blocks_dict_or_None)."""
    current = state["state"]

    if action_id == "df_status":
        return cmd_status(), None

    elif action_id == "df_test_menu":
        state["state"] = "TEST_SUBMENU"
        return None, _build_test_menu_blocks()

    elif action_id == "df_log_menu":
        state["state"] = "LOG_LINES"
        return None, _build_log_lines_blocks()

    elif action_id == "df_test_start":
        state["state"] = "TEST_START_MODEL"
        return None, _build_model_select_blocks()

    elif action_id == "df_model_select":
        state["state"] = "WAITING_RESULT"
        state["context"]["model"] = action_value
        state["context"]["result"] = "Batch testing disabled — model_test_harness.py removed"
        return "Batch testing disabled — model_test_harness.py removed", None

    elif action_id == "df_test_result":
        return cmd_test_result(), None

    elif action_id == "df_test_stop":
        return cmd_test_stop(user_id), None

    elif action_id == "df_log_lines":
        n = int(action_value.split("_")[1])
        return cmd_log(n), None

    elif action_id == "df_back":
        if current in ("TEST_SUBMENU", "LOG_LINES", "TEST_START_MODEL", "WAITING_RESULT"):
            state["state"] = "MENU"
            return None, _build_menu_blocks()
        state["state"] = "MENU"
        return None, _build_menu_blocks()

    # fallback
    state["state"] = "MENU"
    return None, _build_menu_blocks()


async def _parse_slack_payload(request: Request) -> dict:
    """Parse Slack form-encoded payload, handling both slash commands and interactions."""
    body = await request.body()
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return json.loads(body.decode())
    parsed = dict(urllib.parse.parse_qsl(body.decode()))
    if "payload" in parsed:
        return json.loads(parsed["payload"])
    return parsed


# ── endpoints ────────────────────────────────────────────────────────
@router.post("/slack/commands")
async def slack_commands(request: Request):
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not verify_slack_signature(body, timestamp, signature):
        logger.warning("Slack cmd sig fail — ts=%s sig=%s body_len=%d", timestamp, signature[:20] if signature else "none", len(body))
        return JSONResponse({"text": "invalid signature"}, status_code=403)

    payload = dict(urllib.parse.parse_qsl(body.decode()))
    user_id = payload.get("user_id", "")
    channel_id = payload.get("channel_id", "")
    response_url = payload.get("response_url", "")

    state = _get_state(user_id, channel_id)
    state["response_url"] = response_url
    state["ts"] = time.time()
    state["state"] = "MENU"
    state["channel_id"] = channel_id
    state["user_id"] = user_id

    logger.info("Slack command /devforge by %s in %s", user_id, channel_id)
    return JSONResponse(_build_menu_blocks())


@router.post("/slack/interactions")
async def slack_interactions(request: Request):
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not verify_slack_signature(body, timestamp, signature):
        return JSONResponse({"text": "invalid signature"}, status_code=403)

    form = dict(urllib.parse.parse_qsl(body.decode()))
    payload = json.loads(form.get("payload", "{}"))

    user = payload.get("user", {})
    channel = payload.get("channel", {})
    user_id = user.get("id", "")
    channel_id = channel.get("id", "")
    response_url = payload.get("response_url", "")
    message_ts = payload.get("message", {}).get("ts", "")

    action = (payload.get("actions", [{}]) or [{}])[0]
    action_id = action.get("action_id", "")
    action_value = action.get("value", "")

    state = _get_state(user_id, channel_id)
    state["ts"] = time.time()

    logger.info("Slack interaction %s=%s by %s", action_id, action_value, user_id)

    result_text, blocks = _advance(state, action_id, action_value, user_id)

    if blocks:
        _post_to_response_url(response_url, blocks)
    elif result_text:
        _post_to_slack(channel_id, result_text)

    return JSONResponse({"text": "ok"})
