#!/usr/bin/env python3
# Status: production
# Path: imported by — notice/telegram_bot.py, notice/slack.py
"""scripts/notice/lib/bot_processor.py -- Shared bot message processor for Telegram & Slack.

Uses Qwen2.5-Coder-7B (:8082) for intent classification and conversation.
No DeepSeek / Claude Code tokens consumed.

Flow:
  process(text) -> response_text
    1. classify_intent(text) -> Intent  (7B LLM, cheap)
    2. dispatch action based on intent
    3. return response string

Note: BOT_LLM_ENDPOINT defaults to :8082 (Pod A). Pod A death produces
[ERROR] messages but does NOT crash the calling service.
"""

import json, os, subprocess, sys, urllib.request
from enum import Enum
from pathlib import Path
from typing import Optional


class Intent(Enum):
    STATUS = "status"
    SHELL = "shell"
    LOG = "log"
    MODE = "mode"
    CHAT = "chat"


LLM_ENDPOINT = os.environ.get("BOT_LLM_ENDPOINT", "http://127.0.0.1:8082/v1/chat/completions")
LLM_MODEL = os.environ.get("BOT_LLM_MODEL", "qwen2.5-coder-7b")
PROJECT_DIR = Path(os.environ.get("PROJECT_DIR", "/opt/projects/server"))


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

_SECRETS = _load_secrets()


def _call_llm(messages: list, temperature: float = 0.3, max_tokens: int = 512) -> str:
    body = {"model": LLM_MODEL, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    req = urllib.request.Request(LLM_ENDPOINT, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read())
            choice = result.get("choices", [{}])[0]
            m = choice.get("message", {})
            content = m.get("content", "") or m.get("reasoning_content", "")
        return content.strip()
    except Exception as e:
        return f"[ERROR: {e}]"


_CLASSIFY_SYSTEM = (
    "You are a precise intent classifier for a server operator bot. "
    "Respond with EXACTLY ONE word: status, shell, log, mode, or chat.\n\n"
    "status = user asks about system status (memory, disk, containers, uptime)\n"
    "shell = user wants to run a shell command or script\n"
    "log = user wants to see logs or recent activity\n"
    "mode = user asks about current operational mode\n"
    "chat = anything else: greeting, question, conversation"
)


def classify_intent(text: str) -> Intent:
    messages = [{"role": "system", "content": _CLASSIFY_SYSTEM}, {"role": "user", "content": text}]
    raw = _call_llm(messages, temperature=0.1, max_tokens=10).strip().lower()
    for intent in Intent:
        if intent.value in raw:
            return intent
    return Intent.CHAT


def _exec_status() -> str:
    lines = []
    for label, mf in [("A", Path("/opt/ai_data/scripts/current-mode-pod-a.env")), ("B", Path("/opt/ai_data/scripts/current-mode-pod-b.env"))]:
        if mf.exists():
            m = mf.read_text().strip().replace("MODE=", "")
            lines.append(f"<b>모드 Pod {label}</b>\n<code>{m}</code>")
    try:
        r = subprocess.run(["free", "-h"], capture_output=True, text=True, timeout=5)
        for l in r.stdout.split("\n"):
            if "Mem:" in l:
                parts = l.split()
                lines.append(f"\n<b>메모리</b>\n전체 {parts[1]} / 사용 {parts[2]} / 여유 {parts[-1]}")
    except Exception:
        pass
    try:
        r = subprocess.run(["podman", "ps", "--format", "{{.Names}} ({{.Status}})"], capture_output=True, text=True, timeout=5)
        containers = [cl.strip() for cl in r.stdout.strip().split("\n")[:8] if cl.strip()]
        if containers:
            lines.append("\n<b>컨테이너</b>")
            for c in containers:
                lines.append(f"• <code>{c}</code>")
    except Exception:
        pass
    return "\n".join(lines) or "(상태 정보 없음)"


def _exec_shell(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30, cwd=str(PROJECT_DIR))
        out = r.stdout.strip() or r.stderr.strip() or "(no output)"
        return out[:2500] + "\n... (truncated)" if len(out) > 2500 else out
    except subprocess.TimeoutExpired:
        return "명령 시간 초과 (30s)"
    except Exception as e:
        return f"명령 실행 실패: {e}"


def _exec_log(lines_count: int = 20) -> str:
    try:
        r = subprocess.run(["journalctl", "--user", "-n", str(lines_count), "--no-pager", "-q"], capture_output=True, text=True, timeout=10)
        out = r.stdout.strip()
        return "..." + out[-2500:] if len(out) > 2500 else out
    except Exception as e:
        return f"로그 조회 실패: {e}"


def _current_mode() -> str:
    mf = Path("/opt/ai_data/scripts/current-mode-pod-b.env")
    if mf.exists():
        mode = mf.read_text().strip().replace("MODE=", "")
        return "review" if mode.startswith("review-") else mode
    return "unknown"


_CHAT_SYSTEM = """You are the AI operator of a DevForge ARM server (Oracle Linux, Podman rootless, 22GB RAM).

Communicate with the user in natural, concise, friendly Korean.

[Response format — mobile readability]
- Separate each block with a BLANK LINE.
- Section headers: <b>bold</b>, content on next line.
- List items: each on its own line, starting with •.
- Values: wrap in <code>code</code> tags.
- Summarize long output to 5-10 key lines.

[CMD: protocol]
When you need to execute a shell command to answer, output exactly:
CMD: <command>

Examples:
CMD: free -h
CMD: podman ps --format '{{.Names}} {{.Status}}'
CMD: df -h /

[Rules]
- Use "podman" never "docker".
- systemctl needs --user flag.
- One command per CMD: line.
- Response naturally in Korean after receiving [RESULT].
- /no_think — do not include thinking blocks.
- HTML tags must be properly closed."""


def _chat(text: str) -> str:
    messages = [{"role": "system", "content": _CHAT_SYSTEM}, {"role": "user", "content": text + "\n/no_think"}]
    resp1 = _call_llm(messages)
    if not resp1 or resp1.startswith("[ERROR"):
        return resp1 or "(응답 없음)"
    if not resp1.startswith("CMD:"):
        return resp1
    cmd = resp1[len("CMD:"):].strip()
    if not cmd:
        return "(빈 명령어)"
    print(f"[bot_processor] executing: {cmd}", file=sys.stderr, flush=True)
    result = _exec_shell(cmd)
    messages.append({"role": "assistant", "content": resp1})
    messages.append({"role": "user", "content": f"[RESULT]\n{result[:3000]}\n[/RESULT]\n\nCompose a natural Korean response based on the result above.\n/no_think"})
    resp2 = _call_llm(messages, max_tokens=512)
    return resp2 or result[:1500]


def process(text: str) -> str:
    if not text or not text.strip():
        return ""
    text = text.strip()
    if text.startswith("!"):
        cmd = text[1:].strip()
        return "명령을 입력하세요. 예: `!podman ps`" if not cmd else _exec_shell(cmd)
    intent = classify_intent(text)
    print(f"[bot_processor] intent={intent.value} text={text[:60]}", file=sys.stderr, flush=True)
    if intent == Intent.STATUS:
        return _exec_status()
    elif intent == Intent.SHELL:
        return _chat(text)
    elif intent == Intent.LOG:
        return _exec_log(20)
    elif intent == Intent.MODE:
        return f"현재 운영 모드: <code>{_current_mode()}</code>"
    else:
        return _chat(text)
