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

SYSTEM_PROMPT = """You are the DevForge operator assistant on an ARM server (Oracle Linux, Podman, 22GB RAM).
You receive Korean messages from the server admin. Respond with a JSON action object ONLY, no other text.

Available actions:
- {"action": "reply", "text": "Korean answer"} — answer a question
- {"action": "ssh", "command": "shell command", "reply": "Korean description"} — run a command
- {"action": "status"} — show system status
- {"action": "log", "lines": 20} — show recent journal logs
- {"action": "mode_switch", "target": "normal|batch|code"} — switch LLM mode
- {"action": "none"} — no action needed

For SSH: generate safe commands. Prefer read-only (systemctl status, podman ps, free -h, df -h, journalctl, ps aux, ls, cat, grep, curl health). Only use write commands (systemctl restart, podman stop) when the user explicitly requests them."""


# ── Telegram API ────────────────────────────────────────────────────
def _tg(method: str, data: dict) -> dict:
    url = f"{BASE_URL}/{method}"
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _send(text: str, chat_id: str = ""):
    target = chat_id or CHAT_ID
    if len(text) > 4000:
        text = text[:4000] + "\n... (truncated)"
    return _tg("sendMessage", {"chat_id": target, "text": text})


# ── Qwen interpreter ────────────────────────────────────────────────
def _ask_qwen(user_msg: str) -> Optional[dict]:
    body = {"messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": user_msg}],
            "temperature": 0.1, "max_tokens": 512}
    req = urllib.request.Request(QWEN_ENDPOINT, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            content = json.loads(resp.read()).get("choices", [{}])[0].get("message", {}).get("content", "")
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        return json.loads(content)
    except json.JSONDecodeError:
        return {"action": "reply", "text": f"Qwen 응답 파싱 실패: {content[:300]}"}
    except Exception as e:
        return {"action": "reply", "text": f"Qwen 연결 실패: {e}"}


# ── action executors ────────────────────────────────────────────────
def _exec_status() -> str:
    lines = ["*DevForge 상태*\n"]
    mode_file = Path("/opt/ai_data/scripts/current-mode.env")
    if mode_file.exists():
        lines.append(f"모드: `{mode_file.read_text().strip().replace('MODE=', '')}`")
    try:
        r = subprocess.run(["free", "-h"], capture_output=True, text=True, timeout=5)
        for l in r.stdout.split("\n"):
            if "Mem:" in l:
                lines.append(f"메모리: {l.split()[1:]}")  # total used free ...
    except Exception:
        pass
    try:
        r = subprocess.run(["podman", "ps", "--format", "{{.Names}} {{.Status}}"],
                          capture_output=True, text=True, timeout=5)
        lines.append("\n*컨테이너:*")
        for cl in r.stdout.strip().split("\n")[:10]:
            lines.append(f"  `{cl}`")
    except Exception:
        pass
    return "\n".join(lines)


def _exec_ssh(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True,
                          timeout=30, cwd="/opt/projects/server")
        out = r.stdout.strip() or r.stderr.strip() or "(no output)"
        if len(out) > 2800:
            out = out[:2800] + "\n... (truncated)"
        return f"```\n{out}\n```\n종료코드: {r.returncode}"
    except subprocess.TimeoutExpired:
        return "명령 시간 초과 (30s)"
    except Exception as e:
        return f"명령 실행 실패: {e}"


def _exec_log(lines_count: int = 20) -> str:
    try:
        r = subprocess.run(["journalctl", "--user", "-n", str(lines_count), "--no-pager", "-q"],
                          capture_output=True, text=True, timeout=10)
        out = r.stdout.strip()
        if len(out) > 2800:
            out = out[-2800:]
        return f"```\n{out}\n```"
    except Exception as e:
        return f"로그 조회 실패: {e}"


def _exec_mode_switch(target: str) -> str:
    if target not in ("normal", "batch", "code"):
        return f"잘못된 모드: {target}. normal/batch/code 중 하나를 지정하세요."
    script = "/opt/projects/server/scripts/swap_llm_mode.sh"
    try:
        r = subprocess.run(["bash", script, target], capture_output=True, text=True, timeout=900)
        out = r.stdout.strip()
        if len(out) > 2500:
            out = out[-2500:]
        return f"모드 전환 (종료코드 {r.returncode}):\n```\n{out}\n```"
    except subprocess.TimeoutExpired:
        return "모드 전환 시간 초과 (15분)"
    except Exception as e:
        return f"모드 전환 실패: {e}"


# ── message processor ───────────────────────────────────────────────
def _process(msg: dict) -> str:
    text = msg.get("text", "").strip()
    if not text:
        return ""

    # Fast path: built-in commands (no LLM call needed)
    if text in ("/start", "/help"):
        return ("*DevForge Telegram Bot*\n\n"
                "`/status` — 시스템 상태\n"
                "`/mode` — 현재 LLM 모드\n"
                "`/log` — 최근 로그\n"
                "`/help` — 도움말\n\n"
                "한국어로 원하는 작업을 설명하면 Qwen3-4B가 해석하여 실행합니다.")

    if text == "/status":
        return _exec_status()

    if text == "/mode":
        mf = Path("/opt/ai_data/scripts/current-mode.env")
        if mf.exists():
            return f"현재 모드: `{mf.read_text().strip().replace('MODE=', '')}`"
        return "모드 파일을 찾을 수 없습니다."

    if text == "/log":
        return _exec_log(20)

    # Natural language → Qwen interprets
    action = _ask_qwen(text)
    if action is None:
        return "Qwen3-4B 응답 없음 — 서버가 실행 중인지 확인하세요."

    act = action.get("action", "reply")

    if act == "reply":
        return action.get("text", "처리 완료")
    elif act == "status":
        return _exec_status()
    elif act == "ssh":
        cmd = action.get("command", "")
        reply = action.get("reply", "")
        if not cmd:
            return "명령이 지정되지 않았습니다."
        result = _exec_ssh(cmd)
        return f"{reply}\n\n{result}" if reply else result
    elif act == "mode_switch":
        return _exec_mode_switch(action.get("target", ""))
    elif act == "log":
        return _exec_log(int(action.get("lines", 20)))
    elif act == "none":
        return ""
    else:
        return f"알 수 없는 액션: {act}"


# ── entry points ────────────────────────────────────────────────────
def run_once():
    offset = 0
    if OFFSET_FILE.exists():
        try:
            offset = int(OFFSET_FILE.read_text().strip())
        except Exception:
            pass

    updates = _tg("getUpdates", {"timeout": 30, "offset": offset, "allowed_updates": ["message"]})
    if not updates.get("ok"):
        print(f"[telegram_bot] getUpdates failed: {updates.get('error')}", flush=True)
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
