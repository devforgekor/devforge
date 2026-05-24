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

from lib.search.manager import WebSearchManager

_search_manager = WebSearchManager()

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

SYSTEM_PROMPT = """You are the DevForge operator assistant on an ARM server (Oracle Linux, Podman rootless, 22GB RAM).
You receive Korean messages from the server admin. Respond with a JSON action object ONLY, no other text.

For "reply" action: write conversational Korean that naturally answers the user.
For "ssh" action: the "reply" field should briefly describe what you found — use natural Korean like a helpful colleague. Keep it under 15 words.

Available actions:
- {"action": "reply", "text": "Korean answer"} — answer a question
- {"action": "ssh", "command": "shell command", "reply": "Korean description"} — run a command
- {"action": "search", "query": "search keywords"} — web search (code/docs/fact/general auto-routed, Brave/Exa/Tavily/you.com)
- {"action": "status"} — show system status (memory, containers, mode)
- {"action": "log", "lines": 20} — show recent journal logs
- {"action": "mode_switch", "target": "normal|batch|code"} — switch LLM mode
- {"action": "none"} — no action needed

When to use search:
- External knowledge questions (pricing, latest news, documentation references, error codes)
- Code/library documentation that is NOT in the local codebase
- DO NOT use search for: local system state, container status, file paths — use ssh or status instead.

DevForge CLI tools (in /opt/projects/server/scripts/):
- python3 scripts/cli.py worklog recent — today's work log entries
- python3 scripts/cli.py worklog search <keyword> — search worklog by keyword
- python3 scripts/cli.py worklog add "<title>" "<summary>" — add worklog entry
- python3 scripts/cli.py activity recent --today — today's activity log
- python3 scripts/cli.py activity search <keyword> — search activity log
- python3 scripts/cli.py save "<text>" — save memory
- python3 scripts/cli.py search "<query>" — search saved memories
- python3 scripts/cli.py recent — recent conversation turns
- cat docs/tasks.yaml — current task status (todo/in_progress/done)
- cat data/nightly_status.yaml — nightly pipeline status

For questions like "what did I work on today?", "what's the task status?", "show recent worklog" — use python3 scripts/cli.py.

Command rules:
- Use podman (NOT docker). This is a Podman rootless server.
- Use systemctl --user for user services.
- Prefer read-only commands. Only use write commands (restart, stop) when explicitly requested.
- Run CLI from /opt/projects/server/scripts/ directory."""


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


# ── Qwen interpreter ────────────────────────────────────────────────
def _ask_qwen(user_msg: str) -> Optional[dict]:
    body = {"messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": user_msg + "\n/no_think"}],
            "temperature": 0.1, "max_tokens": 512}
    return _call_qwen(body)


def _summarize_result(action: dict, raw_output: str) -> str:
    """Feed command output back to Qwen for natural Korean summary."""
    # Short output: show directly, no LLM summarization needed
    if len(raw_output) <= 1200:
        return raw_output

    summary_prompt = f"""command output:
{raw_output[:2000]}

Summarize this in 1-2 sentences of conversational Korean.
Respond ONLY with: {{\"text\": \"your summary here\"}}"""

    body = {"messages": [
        {"role": "system", "content": "You convert command output into natural Korean summaries. You MUST respond in JSON format: {\"text\": \"summary\"}"},
        {"role": "user", "content": summary_prompt + "\n/no_think"},
    ], "temperature": 0.3, "max_tokens": 256}
    result = _call_qwen(body)
    if result:
        text = result.get("text", "")
        # Only use the summary if it came through clean (not an error fallback)
        if text and "응답 파싱 실패" not in text and "연결 실패" not in text and len(text) > 10:
            return text
    # Fallback: return raw output (truncated if needed)
    return raw_output[:1500]


def _call_qwen(body: dict) -> Optional[dict]:
    req = urllib.request.Request(QWEN_ENDPOINT, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            choice = result.get("choices", [{}])[0]
            msg = choice.get("message", {})
            content = msg.get("content", "")
            # Fallback: if content is empty (thinking mode ate tokens), use reasoning_content tail
            if not content:
                reasoning = msg.get("reasoning_content", "")
                if reasoning:
                    # Take the last portion — typically contains the actual response intent
                    content = reasoning.split("\n\n")[-1].strip()
                    # If it still doesn't look like JSON, try second-to-last
                    if not content.startswith("{"):
                        parts = reasoning.split("\n\n")
                        content = parts[-2].strip() if len(parts) > 1 else reasoning.strip()
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


def _exec_search(query: str) -> str:
    try:
        result = _search_manager.search(query)
        if result is None:
            return "검색 결과 없음 — 모든 프로바이더 소진."
        source = result["source"]
        intent = result["intent"]
        results = result["results"][:5]
        lines = [f"*검색: {query}*  ({source}, {intent})\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "")[:120]
            url = r.get("url", "")
            snippet = r.get("snippet", "")[:200]
            lines.append(f"{i}. [{title}]({url})")
            if snippet:
                lines.append(f"   {snippet}")
        return "\n".join(lines)
    except Exception as e:
        return f"검색 실패: {e}"


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

    # Fast path: common queries → direct command execution (no Qwen, fast + reliable)
    _fast_paths = [
        (["오늘", "작업", "했"], "python3 scripts/cli.py worklog recent", "오늘 작업 내역이야:"),
        (["오늘", "뭐", "했"], "python3 scripts/cli.py worklog recent", "오늘 작업 내역이야:"),
        (["작업", "내역"], "python3 scripts/cli.py worklog recent", "작업 내역이야:"),
        (["진행", "작업"], "cat docs/tasks.yaml", "현재 작업 상태야:"),
        (["할 일", "todo"], "cat docs/tasks.yaml", "할 일 목록이야:"),
        (["컨테이너", "목록"], "podman ps --format '{{.Names}} {{.Status}}'", "컨테이너 목록이야:"),
        (["컨테이너", "상태"], "podman ps --format '{{.Names}} {{.Status}}'", "컨테이너 상태야:"),
    ]
    for keywords, cmd, prefix in _fast_paths:
        if all(kw in text for kw in keywords):
            result = _exec_ssh(cmd)
            return f"{prefix}\n\n{result}"

    # Direct command mode: ! prefix = SSH shell (always available)
    if text.startswith("!"):
        cmd = text[1:].strip()
        if not cmd:
            return "명령을 입력하세요. 예: `!podman ps`"
        return _exec_ssh(cmd)

    # Natural language only in normal mode (Qwen3-4B available)
    # No LLM calls in batch/code — pure Python, no RAM increase
    if mode != "normal":
        return f"지금은 {mode} 모드로 운영되고 있어 사용자의 요청에 응답할 수 없습니다.\n`!` 명령어는 사용 가능합니다. (예: `!podman ps`, `!free -h`)"

    # Natural language → Qwen interprets
    action = _ask_qwen(text)
    if action is None:
        return "Qwen3-4B 응답 없음 — 서버가 실행 중인지 확인하세요. `!` prefix로 직접 실행 가능."

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
        summary = _summarize_result(action, result)
        return f"{reply}\n\n{summary}" if reply else summary
    elif act == "search":
        query = action.get("query", "")
        if not query:
            return "검색어가 지정되지 않았습니다."
        return _exec_search(query)
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
