#!/usr/bin/env python3
# Status: production
# Path: systemd:devforge-telegram
"""telegram_bot.py --- DevForge Telegram bot for remote operations.

Polls Telegram getUpdates, interprets Korean messages via bot_processor
(Qwen2.5-Coder-7B), executes commands, returns results.

Also handles file send/receive:
  - User sends file → downloaded to uploads/ + registered in file_registry DB
  - Agent calls --send-file <path> → file sent to user via sendDocument

Usage:
  python3 telegram_bot.py                          # poll once, process new messages
  python3 telegram_bot.py --daemon                 # continuous polling loop (for systemd service)
  python3 telegram_bot.py --send-file <path>        # send a file to CHAT_ID
  python3 telegram_bot.py --send-file <path> --chat <chat_id>
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from bot_processor import process as bot_process
from text_quality import validate as validate_korean
from lib.file_registry import receive_telegram_file, register_file


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


def _send_file(file_path: str, chat_id: str = "", caption: str = "") -> bool:
    """Send a local file to Telegram chat via sendDocument.

    Uses multipart upload via urllib (Telegram bot API).

    Args:
        file_path: Absolute path to the file on disk.
        chat_id: Target chat (defaults to configured CHAT_ID).
        caption: Optional caption (displayed below the file).

    Returns:
        True on success, False on failure.
    """
    target = chat_id or CHAT_ID
    if not Path(file_path).exists():
        print(f"[telegram_bot] File not found: {file_path}", flush=True)
        return False

    # Build multipart/form-data manually
    boundary = "----WebKitFormBoundary" + os.urandom(16).hex()
    body = bytearray()
    _append_field = lambda name, val: body.extend(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode()
    )
    _append_file = lambda name, fpath: (
        body.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{Path(fpath).name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
        ),
        body.extend(Path(fpath).read_bytes()),
        body.extend(b"\r\n"),
    )

    _append_field("chat_id", target)
    if caption:
        _append_field("caption", caption)
    _append_file("document", file_path)
    body.extend(f"--{boundary}--\r\n".encode())

    url = f"{BASE_URL}/sendDocument"
    req = urllib.request.Request(url, data=bytes(body))
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            ok = result.get("ok", False)
            if ok:
                print(f"[telegram_bot] File sent: {file_path}", flush=True)
            else:
                print(f"[telegram_bot] sendDocument failed: {result.get('description', '?')}", flush=True)
            return ok
    except Exception as e:
        print(f"[telegram_bot] sendDocument error: {e}", flush=True)
        return False


def send_file_by_id(file_id: str, chat_id: str = "") -> bool:
    """Send a file from file_registry DB to Telegram chat.

    Looks up the file by UUID, reads the local path, sends via sendDocument.

    Args:
        file_id: UUID from file_registry.
        chat_id: Target chat (defaults to configured CHAT_ID).

    Returns:
        True on success.
    """
    from lib.file_registry import get_file as _get_file
    rec = _get_file(file_id)
    if not rec:
        print(f"[telegram_bot] File not found in registry: {file_id}", flush=True)
        return False
    return _send_file(rec["path"], chat_id=chat_id, caption=rec.get("filename", ""))


def _handle_document(msg: dict) -> str:
    """Handle a file/photo message from Telegram.

    Downloads the file, registers in file_registry DB,
    and returns a confirmation message.

    Args:
        msg: Telegram message dict (should contain 'document' key).

    Returns:
        Response text for the user.
    """
    doc = msg.get("document") or msg.get("photo", [{}])[-1] if msg.get("photo") else None
    if not doc:
        return ""

    file_id = doc.get("file_id", "")
    if not file_id:
        return "파일을 식별할 수 없습니다."

    file_name = doc.get("file_name", "") if "document" in msg else "photo.jpg"
    sender = str(msg.get("from", {}).get("id", ""))
    chat_id = str(msg.get("chat", {}).get("id", ""))

    result_id = receive_telegram_file(file_id, sender=sender, filename=file_name)
    if result_id:
        return f"파일 저장됨: {file_name}\n(id: {result_id})"
    return f"파일 저장 실패"


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

        # File messages (document/photo)
        if not txt and (msg.get("document") or msg.get("photo")):
            if cid != CHAT_ID:
                OFFSET_FILE.write_text(str(uid + 1))
                continue
            resp = _handle_document(msg)
            if resp:
                _send(resp, cid)
            OFFSET_FILE.write_text(str(uid + 1))
            continue

        # Skip non-text messages
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
    # --send-file <path> [--chat <chat_id>]: send a file from CLI
    if "--send-file" in sys.argv:
        idx = sys.argv.index("--send-file") + 1
        if idx < len(sys.argv):
            fpath = sys.argv[idx]
            chat = ""
            if "--chat" in sys.argv:
                ci = sys.argv.index("--chat") + 1
                if ci < len(sys.argv):
                    chat = sys.argv[ci]
            _send_file(fpath, chat_id=chat)
        sys.exit(0)

    if "--daemon" in sys.argv:
        run_daemon()
    else:
        run_once()
