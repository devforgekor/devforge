# Status: production
# Path: imported by — watchdog.py
"""Slack 알림 — 30분 heartbeat (Block Kit in-place) + state change alert (colored).

Block Kit 형식 (mrkdwn 테이블 → header/section/fields/context):
- Heartbeat: chat.update로 같은 메시지 갱신 (채널 낭비 감소)
- Alert: attachment color로 심각도 표시 (good/warning/danger)
- Alert dedup: 5분/컴포넌트

Telegram: 파일 전송 전용 (watchdog 알림 금지)
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from lib.watchdog.config import SLACK_SECRETS, SLACK_CHANNEL, ALERT_DEDUP_SEC
from .messenger import log_message

KST = timezone(timedelta(hours=9))

# Secrets cache
_SECRETS: dict[str, str] = {}
_SF = Path(SLACK_SECRETS)
if _SF.exists():
    for _line in _SF.read_text().split("\n"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _SECRETS[_k.strip()] = _v.strip().strip('"').strip("'")


def kst_now() -> str:
    return datetime.now(KST).strftime("%m/%d %H:%M")


# ── Slack ──────────────────────────────────────────────────────────

def _slack_channel() -> str:
    return _SECRETS.get("SLACK_CHANNEL", SLACK_CHANNEL)


def _slack_api(method: str, payload: dict, timeout: int = 10) -> dict:
    """Call Slack Web API with urllib (no external deps)."""
    token = _SECRETS.get("SLACK_BOT_TOKEN", "")
    if not token:
        return {"ok": False}
    payload.setdefault("channel", _slack_channel())
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://slack.com/api/{method}", data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return {"ok": False}


# ── Heartbeat (Block Kit, in-place update via chat.update) ─────────

_HEARTBEAT_TS_FILE = Path("/var/tmp/watchdog_slack_heartbeat_ts.txt")


def _build_heartbeat_blocks(state: dict) -> tuple[list, str]:
    """Build Block Kit blocks + fallback text for heartbeat.

    Two modes:
      - Protection active (test/experiment): Only active workers, no full system status.
      - Normal: Full system status (containers, services, timers, probes).
    """
    now_kst = kst_now()
    mode = state.get("mode", "?").upper()
    fallback = f"DevForge Watchdog — {now_kst} KST  [{mode}]"

    blocks = [{
        "type": "header",
        "text": {"type": "plain_text", "text": f"DevForge Watchdog — {now_kst} KST  [{mode}]"},
    }]

    if state.get("experiment_active"):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*EXPERIMENT MODE* — monitor-only, no recovery"},
        })

    active_pulses = state.get("active_pulses", [])

    if active_pulses:
        # ── Test/Protection mode: show workers only ──
        lines = []
        for p in active_pulses:
            name = p.get("pulse_id", "").replace("heartbeat_", "", 1)
            age_sec = p.get("age_sec", 0)
            age_str = f"{age_sec // 60}m" if age_sec > 60 else f"{age_sec}s"
            inst = p.get("instruction", "")[:80]
            lines.append(f"• *{name}* ({age_str}) — {inst}")
        if lines:
            blocks.append({"type": "divider"})
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            })
        # Events only — no containers/services/probes during test
        events = state.get("events_30m", [])
        if events:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"events: {len(events)}"}],
            })
        return blocks, fallback

    # ── Normal mode: full system status ──

    # Containers
    containers = state.get("containers", [])
    if containers:
        blocks.append({"type": "divider"})
        for c in containers:
            icon = "OK" if c.get("ok") else "DOWN"
            blocks.append({
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{c['name']}*\nport :{c['port']}  mode {c.get('mode','?')}"},
                    {"type": "mrkdwn", "text": f"*Status*\n{icon}\n{c.get('uptime','')}"},
                ],
            })

    # Memory / Swap
    mem = state.get("memory", {})
    if mem:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Memory*\n{mem.get('used_gb','?')}G / {mem.get('total_gb','?')}G  {mem.get('pct','?')}%"},
                {"type": "mrkdwn", "text": f"*Swap*\n{mem.get('swap_used_gb','?')}G / {mem.get('swap_total_gb','?')}G  {mem.get('swap_pct','?')}%"},
            ],
        })

    # Services
    services = state.get("services", [])
    if services:
        blocks.append({"type": "divider"})
        for i in range(0, len(services), 2):
            chunk = services[i:i+2]
            blocks.append({
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{s['name']}*\n{'OK' if s.get('ok') else 'DOWN'}"}
                    for s in chunk
                ],
            })

    # Timers
    timers = state.get("timers", [])
    if timers:
        blocks.append({"type": "divider"})
        for i in range(0, len(timers), 2):
            chunk = timers[i:i+2]
            blocks.append({
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{t['name']}*\n{'OK' if t.get('ok') else 'DELAY'}  {t.get('detail','')[:20]}"}
                    for t in chunk
                ],
            })

    # LLM Probes
    probes = state.get("probes", [])
    if probes:
        blocks.append({"type": "divider"})
        for p in probes:
            t1 = "OK" if p.get("t1_ok") else "FAIL"
            blocks.append({
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*LLM :{p['port']}*"},
                    {"type": "mrkdwn", "text": f"T1={t1}  {p.get('t2_detail','')[:30]}"},
                ],
            })

    # Metrics + cache + events (context line)
    ctx_parts = []
    metrics = state.get("metrics", {})
    if metrics:
        for port, m in metrics.items():
            gen = f"{m['gen_tps']:.1f}t/s" if m.get('gen_tps', 0) > 0 and m['gen_tps'] < float('inf') else "-"
            ctx_parts.append(f":{port} pr={m['prompt_tps']:.1f} gen={gen}")
    slots = state.get("slots", {})
    if slots:
        for port, slot_list in slots.items():
            caches = " ".join(f"s{s['id']}={s['cache_pct']}%" for s in slot_list)
            ctx_parts.append(f":{port} [{caches}]")
    events = state.get("events_30m", [])
    ctx_parts.append(f"events: {len(events)}")

    if ctx_parts:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " | ".join(ctx_parts)}],
        })

    return blocks, fallback


def _post_or_update_heartbeat(blocks: list, fallback: str):
    """Post new heartbeat or update existing one in-place."""
    ts_file = _HEARTBEAT_TS_FILE
    if ts_file.exists():
        message_ts = ts_file.read_text().strip()
        result = _slack_api("chat.update", {"ts": message_ts, "text": fallback, "blocks": blocks})
        if result.get("ok"):
            return
    result = _slack_api("chat.postMessage", {"text": fallback, "blocks": blocks})
    if result.get("ok") and result.get("ts"):
        ts_file.write_text(str(result["ts"]))


def heartbeat(state_summary: dict) -> None:
    """30분 heartbeat — Block Kit, in-place update."""
    blocks, fallback = _build_heartbeat_blocks(state_summary)
    _post_or_update_heartbeat(blocks, fallback)


# ── Telegram ───────────────────────────────────────────────────────

def _telegram_send(text: str) -> bool:
    token = _SECRETS.get("TELEGRAM_TOKEN", "")
    chat_id = _SECRETS.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return result.get("ok", False)
    except Exception:
        return False


# ── Alert / Recovery (colored attachments) ─────────────────────────


def _alert_color(state: str) -> str:
    ls = state.lower()
    if "down" in ls or "crit" in ls or "fail" in ls:
        return "danger"
    if "delay" in ls or "warn" in ls or "latency" in ls:
        return "warning"
    return "good"


def send_alert(component: str, state: str, detail: str) -> None:
    log_message("watchdog", "operator", "alert", f"{component} is {state}", detail)
    """State change alert with colored attachment."""
    now_kst = kst_now()
    _slack_api("chat.postMessage", {
        "text": f"[{now_kst}] {component} -> {state}",
        "attachments": [{
            "color": _alert_color(state),
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": f"{component}  ->  {state}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": detail[:200]}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{now_kst} KST"}]},
            ],
        }],
    })


def send_recovery(component: str, detail: str) -> None:
    log_message("watchdog", "operator", "recovery", f"{component} recovered", detail)
    """Recovery notice with green attachment."""
    now_kst = kst_now()
    _slack_api("chat.postMessage", {
        "text": f"[{now_kst}] {component} recovered ({detail})",
        "attachments": [{
            "color": "good",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": f"{component}  recovered"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": detail[:200]}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{now_kst} KST"}]},
            ],
        }],
    })
