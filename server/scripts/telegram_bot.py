#!/usr/bin/env python3
"""telegram_bot.py — DevForge Telegram bot for remote operations.

Polls Telegram getUpdates, interprets Korean messages via Qwen3-4B (localhost:8080),
executes system commands, returns results.

Usage:
  python3 telegram_bot.py                  # poll once, process new messages
  python3 telegram_bot.py --daemon         # continuous polling loop (for systemd service)
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

from lib.text_quality import validate as validate_korean

# ── config ──────────────────────────────────────────────────────────
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
QWEN_ENDPOINT = "http://127.0.0.1:8080/v1/chat/completions"
OFFSET_FILE = Path("/var/tmp/telegram_bot_offset.txt")

SYSTEM_PROMPT = """You are the AI operator of a DevForge ARM server (Oracle Linux, Podman rootless, 22GB RAM).

Communicate with the user in natural, concise, friendly Korean. When you need real data, use the CMD: tool below. Feed the result back into a natural Korean response.

[Response format — CRITICAL for mobile readability]
The user reads on a phone. Structure every response clearly:

- Separate each information block with a BLANK LINE.
- Section headers: <b>bold</b>, followed by content on the next line.
- List items: each on its own line, starting with •.
- Values/stats: wrap in <code>code</code> tags.
- Summarize long command output to 5–10 key lines.
- Example:

<b>메모리 상태</b>
• 전체 22GB 중 4.2GB 사용 중 (19%).
• 여유 12GB, 가용 16GB.

<b>실행 중인 컨테이너</b>
• <code>devforge-qwen</code> — Up 3h (healthy)
• <code>postgres</code> — Up 2d (healthy)
• <code>devforge-api</code> — Up 2d (healthy)

총 3개 정상 작동 중이야.

[Available tool — CMD: protocol]
To execute a shell command, output exactly one line:

CMD: <shell command>

Examples:
CMD: free -h
CMD: podman ps --format '{{.Names}} {{.Status}}'
CMD: python3 scripts/cli.py worklog recent
CMD: cat docs/tasks.yaml
CMD: systemctl --user status devforge-api
CMD: journalctl --user -n 20 --no-pager -q

[Command reference]
- free -h — memory usage
- df -h / /mnt/lv_db /mnt/secure_meta — disk usage
- podman ps — container list
- python3 scripts/cli.py worklog recent — recent work log
- python3 scripts/cli.py worklog search <keyword> — search work log
- python3 scripts/cli.py activity recent --today — today's activity log
- cat docs/tasks.yaml — current task board
- cat data/nightly_status.yaml — nightly pipeline status
- systemctl --user status <service> — service status
- journalctl --user -n N --no-pager -q — journal logs

[Rules]
- The server uses Podman rootless. Never use "docker" — always use "podman".
- systemctl needs --user flag.
- Request ONE command per CMD: line. If you need multiple, do them sequentially across turns.
- For simple conversation or questions, respond directly in Korean — no CMD: needed.
- When you receive a [RESULT] block, compose a natural Korean explanation from it.
- HTML tags must be properly closed: <b>...</b>, <code>...</code>, <i>...</i>
- Avoid raw <, >, & characters in plain text — only use them inside HTML tags."""


# ── Telegram API ────────────────────────────────────────────────────
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
    # Phase 2 guardrail: soft-check Korean output quality before sending
    q = validate_korean(text)
    if not q["ok"]:
        issues = []
        for name, check in q["checks"].items():
            if not check["ok"]:
                issues.append(f"{name}={check}")
        print(f"[quality:warn] _send: {issues}", flush=True)
    # Try HTML parse mode first for rich formatting; fall back to plain text
    result = _tg("sendMessage", {"chat_id": target, "text": text, "parse_mode": "HTML"})
    if not result.get("ok"):
        result = _tg("sendMessage", {"chat_id": target, "text": text})
    return result


# ── Qwen operator ──────────────────────────────────────────────────
def _call_qwen_raw(messages: list, max_tokens: int = 512) -> str:
    """Single Qwen call, returns raw text."""
    body = {"messages": messages, "temperature": 0.3, "max_tokens": max_tokens}
    req = urllib.request.Request(QWEN_ENDPOINT, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read())
            choice = result.get("choices", [{}])[0]
            m = choice.get("message", {})
            content = m.get("content", "") or m.get("reasoning_content", "")
        return content.strip()
    except Exception as e:
        return f"[ERROR: {e}]"


def _chat_qwen(user_msg: str) -> str:
    """Qwen operator: can execute commands via CMD: protocol. Multi-turn."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg + "\n/no_think"},
    ]

    # Turn 1: Qwen decides — reply directly or request CMD
    resp1 = _call_qwen_raw(messages)
    if not resp1 or resp1.startswith("[ERROR"):
        return resp1 or "(응답 없음)"

    # No CMD request → direct reply
    if not resp1.startswith("CMD:"):
        return resp1

    # Extract and execute command
    cmd = resp1[len("CMD:"):].strip()
    if not cmd:
        return "(빈 명령어)"

    print(f"[telegram_bot] Qwen requested: {cmd[:100]}", flush=True)
    result = _exec_ssh(cmd)

    # Turn 2: Feed result back, Qwen composes natural response
    messages.append({"role": "assistant", "content": resp1})
    messages.append({"role": "user", "content": f"[RESULT]\n{result[:3000]}\n[/RESULT]\n\nCompose a natural Korean response based on the result above.\n/no_think"})

    resp2 = _call_qwen_raw(messages, max_tokens=512)
    return resp2 or result[:1500]


# ── action executors ────────────────────────────────────────────────
def _exec_status() -> str:
    lines = []
    mode_file = Path("/opt/ai_data/scripts/current-mode.env")
    if mode_file.exists():
        mode = mode_file.read_text().strip().replace("MODE=", "")
        lines.append(f"<b>운영 모드</b>\n<code>{mode}</code>")
    try:
        r = subprocess.run(["free", "-h"], capture_output=True, text=True, timeout=5)
        for l in r.stdout.split("\n"):
            if "Mem:" in l:
                parts = l.split()
                lines.append(f"\n<b>메모리</b>\n전체 {parts[1]} / 사용 {parts[2]} / 여유 {parts[-1]}")
    except Exception:
        pass
    try:
        r = subprocess.run(["podman", "ps", "--format", "{{.Names}} ({{.Status}})"],
                          capture_output=True, text=True, timeout=5)
        containers = [cl.strip() for cl in r.stdout.strip().split("\n")[:8] if cl.strip()]
        if containers:
            lines.append(f"\n<b>컨테이너</b>")
            for c in containers:
                lines.append(f"• <code>{c}</code>")
    except Exception:
        pass
    return "\n".join(lines)


def _exec_ssh(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True,
                          timeout=30, cwd="/opt/projects/server")
        out = r.stdout.strip() or r.stderr.strip() or "(no output)"
        if len(out) > 2500:
            out = out[:2500] + "\n... (truncated)"
        return out
    except subprocess.TimeoutExpired:
        return "명령 시간 초과 (30s)"
    except Exception as e:
        return f"명령 실행 실패: {e}"


def _exec_log(lines_count: int = 20) -> str:
    try:
        r = subprocess.run(["journalctl", "--user", "-n", str(lines_count), "--no-pager", "-q"],
                          capture_output=True, text=True, timeout=10)
        out = r.stdout.strip()
        if len(out) > 2500:
            out = "..." + out[-2500:]
        return out
    except Exception as e:
        return f"로그 조회 실패: {e}"


def _current_mode() -> str:
    """Read current LLM mode from mode file."""
    mf = Path("/opt/ai_data/scripts/current-mode.env")
    if mf.exists():
        return mf.read_text().strip().replace("MODE=", "")
    return "unknown"


# ── message processor ───────────────────────────────────────────────
def _process(msg: dict) -> str:
    text = msg.get("text", "").strip()
    if not text:
        return ""

    mode = _current_mode()

    # Fast path: built-in commands (no LLM call needed)
    if text in ("/start", "/help"):
        if mode != "normal":
            return (f"지금은 {mode} 모드로 운영되고 있어 사용자의 요청에 응답할 수 없습니다.\n\n"
                    "사용 가능한 명령어:\n"
                    "`!<command>` — 셸 직접 실행\n"
                    "`/status` — 시스템 상태\n"
                    "`/mode` — 현재 모드\n"
                    "`/log` — 최근 로그")
        return ("무엇을 도와드릴까요?\n\n"
                "그냥 한국어로 말씀하시면 됩니다.\n"
                "예: \"오늘 작업 내역 보여줘\", \"메모리 상태 어때?\"\n\n"
                "`!<command>` — 셸 명령 직접 실행\n"
                "`/status` — 시스템 상태\n"
                "`/mode` — 현재 모드\n"
                "`/log` — 최근 로그")

    if text == "/status":
        return _exec_status()

    if text == "/mode":
        return f"현재 모드: `{mode}`"

    if text == "/log":
        return _exec_log(20)

    # Direct command mode: ! prefix = SSH shell (always available, bypasses Qwen)
    if text.startswith("!"):
        cmd = text[1:].strip()
        if not cmd:
            return "명령을 입력하세요. 예: `!podman ps`"
        return _exec_ssh(cmd)

    # Natural language only in normal mode (Qwen3-4B available)
    if mode != "normal":
        return f"지금은 {mode} 모드로 운영되고 있어 사용자의 요청에 응답할 수 없습니다.\n`!` 명령어는 사용 가능합니다. (예: `!podman ps`, `!free -h`)"

    # Everything else → straight to Qwen, raw response
    return _chat_qwen(text)


# ── entry points ────────────────────────────────────────────────────
def run_once():
    offset = 0
    if OFFSET_FILE.exists():
        try:
            offset = int(OFFSET_FILE.read_text().strip())
        except Exception:
            pass

    # timeout=55s: Telegram long-poll is 30s + buffer. 409 Conflict on retry.
    updates = _tg("getUpdates", {"timeout": 30, "offset": offset, "allowed_updates": ["message"]}, timeout=55)
    if not updates.get("ok"):
        err = str(updates.get("error", ""))
        # Timeout is expected when no messages — not an error. 409 Conflict: another poll in flight, retry next cycle.
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
