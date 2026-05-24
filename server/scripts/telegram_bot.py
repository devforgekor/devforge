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

SYSTEM_PROMPT = """너는 DevForge 서버의 AI 운영자야. ARM 서버(Oracle Linux, Podman rootless, 22GB RAM)에서 작동 중이야.

사용자와 자연스러운 한국어로 대화해. 간결하고 친근하게. 명령어 실행이 필요하면 아래 도구를 호출하고, 그 결과를 바탕으로 자연스럽게 답변해.

[사용 가능한 도구]
아래 형식으로 정확히 한 줄을 출력하면 도구가 실행되고 결과를 받을 수 있어:

CMD: <셸 명령어>

예:
CMD: free -h
CMD: podman ps --format '{{.Names}} {{.Status}}'
CMD: python3 scripts/cli.py worklog recent
CMD: cat docs/tasks.yaml
CMD: systemctl --user status devforge-api
CMD: journalctl --user -n 20 --no-pager -q

[주요 명령어 레퍼런스]
- free -h — 메모리 상태
- df -h / /mnt/lv_db /mnt/secure_meta — 디스크 용량
- podman ps — 컨테이너 목록
- python3 scripts/cli.py worklog recent — 최근 작업 로그
- python3 scripts/cli.py worklog search <키워드> — 작업 로그 검색
- python3 scripts/cli.py activity recent --today — 오늘 활동 로그
- cat docs/tasks.yaml — 현재 작업 보드
- cat data/nightly_status.yaml — nightly 파이프라인 상태
- systemctl --user status <서비스> — 서비스 상태
- journalctl --user -n N --no-pager -q — 저널 로그

[중요 규칙]
- 명령어는 Podman rootless 환경에서 실행돼. docker 대신 podman 사용.
- systemctl은 --user 붙여야 해.
- CMD: 한 번에 하나의 명령어만 요청. 여러 개가 필요하면 순차적으로 해.
- 단순 대화나 질문에는 CMD: 없이 바로 한국어로 답변해.
- 명령어 실행 결과는 [RESULT]로 시작하는 블록으로 받게 돼. 그걸 보고 자연스럽게 설명해줘."""


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
    return _tg("sendMessage", {"chat_id": target, "text": text})


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

    print(f"[telegram_bot] Qwen 요청: {cmd[:100]}", flush=True)
    result = _exec_ssh(cmd)

    # Turn 2: Feed result back, Qwen composes natural response
    messages.append({"role": "assistant", "content": resp1})
    messages.append({"role": "user", "content": f"[RESULT]\n{result[:3000]}\n[/RESULT]\n\n위 결과를 바탕으로 자연스러운 한국어로 답변해줘.\n/no_think"})

    resp2 = _call_qwen_raw(messages, max_tokens=512)
    return resp2 or result[:1500]


# ── action executors ────────────────────────────────────────────────
def _exec_status() -> str:
    lines = []
    mode_file = Path("/opt/ai_data/scripts/current-mode.env")
    if mode_file.exists():
        mode = mode_file.read_text().strip().replace("MODE=", "")
        lines.append(f"현재 {mode} 모드로 운영 중이야.")
    try:
        r = subprocess.run(["free", "-h"], capture_output=True, text=True, timeout=5)
        for l in r.stdout.split("\n"):
            if "Mem:" in l:
                parts = l.split()
                lines.append(f"메모리는 전체 {parts[1]} 중 {parts[2]} 사용 중이고, {parts[-1]} 남았어.")
    except Exception:
        pass
    try:
        r = subprocess.run(["podman", "ps", "--format", "{{.Names}} ({{.Status}})"],
                          capture_output=True, text=True, timeout=5)
        containers = [cl.strip() for cl in r.stdout.strip().split("\n")[:8] if cl.strip()]
        if containers:
            lines.append("실행 중인 컨테이너는 " + ", ".join(containers) + ".")
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
