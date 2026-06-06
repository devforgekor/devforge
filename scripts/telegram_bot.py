#!/usr/bin/env python3
"""telegram_bot.py --- DevForge Telegram bot for remote operations.

Polls Telegram getUpdates, interprets Korean messages via bot_processor
(Qwen2.5-Coder-3B), executes commands, returns results.

Usage:
  python3 telegram_bot.py                  # poll once, process new messages
  python3 telegram_bot.py --daemon         # continuous polling loop (for systemd service)
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.bot_processor import process as bot_process
from lib.text_quality import validate as validate_korean


def _load_secrets():
    secrets = {}
    secrets_file = Path.home() / ".config/devforge/secrets.env"
    if secrets_file.exists():
        for line in secrets_file.read_text().split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                secrets[key.strip()] = val.strip().strip('"').strip("'")
    return secrets

SECRETS = _load_secrets()
TOKEN = SECRETS.get("TELEGRAM_TOKEN", "")
CHAT_ID = SECRETS.get("TELEGRAM_CHAT_ID", "")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
OFFSET_FILE = Path("/var/tmp/telegram_bot_offset.txt")


def _tg(method: str, data: dict, timeout: int = 15) -> dict:
    url = f"{BASE_URL}/{method}"
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _send(text: str, chat_id: str = ""):
    target = chat_id or CHAT_ID
    if len(text) > 4000:
        text = text[:4000] + "\n... (truncated)"
    q = validate_korean(text)
    if not q["ok"]:
        issues = [f"{name}={check}" for name, check in q["checks"].items() if not check["ok"]]
        print(f"[telegram_bot] quality:warn {issues}", flush=True)
    result = _tg("sendMessage", {"chat_id": target, "text": text, "parse_mode": "HTML"})
    if not result.get("ok"):
        result = _tg("sendMessage", {"chat_id": target, "text": text})
    return result


def _process(msg: dict) -> str:
    text = msg.get("text", "").strip()
    if not text:
        return ""
    return bot_process(text)


def run_once():
    offset = 0
    if OFFSET_FILE.exists():
        try:
            offset = int(OFFSET_FILE.read_text().strip())
        except Exception:
            pass
    updates = _tg("getUpdates", {"timeout": 30, "offset": offset, "allowed_updates": ["message"]}, timeout=55)
    if not updates.get("ok"):
        err = str(updates.get("error", ""))
        if "conflict" not in err.lower() and "time" not in err.lower() and "read" not in err.lower():
            print(f"[telegram_bot] getUpdates failed: {err}", flush=True)
        return
    for upd in updates.get("result", []):
        uid = upd.get("update_id", 0)
        msg = upd.get("message", {})
        cid = str(msg.get("chat", {}).get("id", ""))
        txt = msg.get("text", "")
        if not txt:
            OFFSET_FILE.write_text(str(uid + 1))
            continue
        if cid != CHAT_ID:
            _send(f"Unauthorized chat_id: {cid}", cid)
            OFFSET_FILE.write_text(str(uid + 1))
            continue
        print(f"[telegram_bot] {txt[:100]}", flush=True)
        resp = _process(msg)
        if resp:
            _send(resp, cid)
            print(f"[telegram_bot] replied ({len(resp)} chars)", flush=True)
        OFFSET_FILE.write_text(str(uid + 1))


def run_daemon(interval: int = 5):
    print(f"[telegram_bot] daemon start (poll={interval}s)", flush=True)
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            print("[telegram_bot] stopped.", flush=True)
            break
        except Exception as e:
            print(f"[telegram_bot] error: {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        run_daemon()
    else:
        run_once()
