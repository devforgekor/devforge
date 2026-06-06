#!/usr/bin/env python3
# Status: experimental
# Path: none — library
"""telegram_send.py --- Send files or text to your Telegram chat.

Usage:
  python3 telegram_send.py <file>           # send file via sendDocument
  python3 telegram_send.py <file> --text "msg"  # send file with caption
  echo "hello" | python3 telegram_send.py   # send piped text via sendMessage
  python3 telegram_send.py --text "msg"     # send text message only

Output: prints the URL or status of the sent message.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Optional


def _load_secrets() -> dict:
    secrets = {}
    sf = Path.home() / ".config/devforge/secrets.env"
    if sf.exists():
        for line in sf.read_text().split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                secrets[key.strip()] = val.strip().strip('"').strip("'")
    return secrets


SECRETS = _load_secrets()
TOKEN = SECRETS.get("TELEGRAM_TOKEN", "")
CHAT_ID = SECRETS.get("TELEGRAM_CHAT_ID", "")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"


def _tg(method: str, data: dict = None, files: dict = None, timeout: int = 60) -> dict:
    """Call Telegram Bot API."""
    url = f"{BASE_URL}/{method}"
    body = json.dumps(data).encode() if data else None

    if files:
        import uuid
        from email.mime.multipart import MIMEMultipart
        from email.mime.application import MIMEApplication
        from email.mime.text import MIMEText

        boundary = f"boundary.{uuid.uuid4().hex}"
        parts = []
        if data:
            for key, val in data.items():
                parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{val}\r\n".encode())
        for field_name, (filename, content, mime_type) in files.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field_name}\"; filename=\"{filename}\"\r\nContent-Type: {mime_type}\r\n\r\n".encode()
                + content
                + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_file(filepath: str, caption: Optional[str] = None) -> bool:
    """Send a document via Telegram sendDocument."""
    fpath = Path(filepath)
    if not fpath.exists():
        print(f"ERROR: file not found: {filepath}", file=sys.stderr)
        return False

    size_mb = fpath.stat().st_size / (1024 * 1024)
    if size_mb > 50:
        print(f"ERROR: file too large ({size_mb:.1f}MB > 50MB limit)", file=sys.stderr)
        return False

    data = {"chat_id": CHAT_ID}
    if caption:
        data["caption"] = caption[:1024]

    content = fpath.read_bytes()
    ext = fpath.suffix.lower()
    mime_map = {".md": "text/markdown", ".txt": "text/plain", ".json": "application/json",
                ".yaml": "text/yaml", ".yml": "text/yaml", ".py": "text/x-python",
                ".sh": "text/x-shellscript", ".log": "text/plain",
                ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg"}
    mime_type = mime_map.get(ext, "application/octet-stream")

    result = _tg("sendDocument", data=data, files={"document": (fpath.name, content, mime_type)})
    if result.get("ok"):
        doc = result.get("result", {}).get("document", {})
        print(f"Sent: {doc.get('file_name', fpath.name)} ({size_mb:.2f}MB)", file=sys.stderr)
        return True
    else:
        print(f"ERROR: {result.get('description', result)}", file=sys.stderr)
        return False


def send_text(text: str) -> bool:
    """Send a text message via Telegram sendMessage."""
    max_len = 4000
    if len(text) > max_len:
        text = text[:max_len] + "\n... (truncated)"

    result = _tg("sendMessage", data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"})
    if not result.get("ok"):
        # Retry without parse_mode
        result = _tg("sendMessage", data={"chat_id": CHAT_ID, "text": text})
    if result.get("ok"):
        print("Sent: text message", file=sys.stderr)
        return True
    else:
        print(f"ERROR: {result.get('description', result)}", file=sys.stderr)
        return False


def _parse_args(argv: list[str]) -> tuple:
    """Parse CLI args: [file] [--text caption]"""
    filepath = None
    text_arg = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--text":
            i += 1
            if i < len(argv):
                text_arg = argv[i]
        elif a.startswith("--text="):
            text_arg = a[len("--text="):]
        elif not a.startswith("--"):
            filepath = a
        i += 1
    return filepath, text_arg


def main():
    if not TOKEN or not CHAT_ID:
        print("ERROR: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set in secrets.env", file=sys.stderr)
        sys.exit(1)

    filepath, text_arg = _parse_args(sys.argv)
    piped = not sys.stdin.isatty()
    piped_text = sys.stdin.read().strip() if piped else ""

    if filepath:
        ok = send_file(filepath, caption=text_arg)
    elif piped_text:
        ok = send_text(text_arg + "\n\n" + piped_text if text_arg else piped_text)
    elif text_arg:
        ok = send_text(text_arg)
    else:
        print("Usage: telegram_send.py <file> [--text caption]  |  echo 'msg' | telegram_send.py  |  telegram_send.py --text 'msg'", file=sys.stderr)
        sys.exit(1)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
