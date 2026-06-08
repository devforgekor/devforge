# Status: production
# Path: imported by — watchdog.py
"""Slack + Telegram 알림 — 30분 heartbeat 표 + state change alert.

출처: Slack API 문서, 모니터링 도구 사례
- Slack은 mrkdwn에서 테이블 미지원 → 코드 블록 사용
- 이모지 금지, 로그 경로 금지, 텍스트 전용
- Alert dedup: 5분/컴포넌트
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

def _slack_send(text: str) -> bool:
    token = _SECRETS.get("SLACK_BOT_TOKEN", "")
    if not token:
        return False
    channel = _SECRETS.get("SLACK_CHANNEL", SLACK_CHANNEL)
    payload = json.dumps({"channel": channel, "text": text, "mrkdwn": True}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage", data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return result.get("ok", False)
    except Exception:
        return False


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


def _notify_all(text: str):
    """Send to both Slack and Telegram."""
    _slack_send(text)
    _telegram_send(text)


# ── Heartbeat ──────────────────────────────────────────────────────

def heartbeat(state_summary: dict) -> None:
    """30분 heartbeat — 코드 블록 표 (이모지 X, 경로 X, 텍스트 전용)"""
    now_kst = kst_now()
    mode = state_summary.get("mode", "?").upper()

    lines = [f"DevForge Watchdog - {now_kst} KST  [{mode}]", ""]
    if state_summary.get("experiment_active"):
        lines.append("** EXPERIMENT MODE ** (monitor-only, no recovery)")
        lines.append("")

    lines.append("Container     Port  Mode  Status    Uptime")
    for c in state_summary.get("containers", []):
        lines.append(
            f"{c['name']:<12} :{c['port']:<3} {c.get('mode','?'):<6} "
            f"{'RUNNING' if c.get('ok') else 'DOWN':<8} {c.get('uptime','?')}"
        )

    lines.append("")
    lines.append("Service            Status")
    for s in state_summary.get("services", []):
        lines.append(f"{s['name']:<18} {s['detail']:<8}")

    mem = state_summary.get("memory", {})
    if mem:
        lines.append("")
        lines.append("Memory  {:>4}G / {:>4}G  {:>3}%".format(
            mem.get("used_gb", "?"), mem.get("total_gb", "?"), mem.get("pct", "?")))
        lines.append("Swap    {:>4}G / {:>4}G  {:>3}%".format(
            mem.get("swap_used_gb", "?"), mem.get("swap_total_gb", "?"), mem.get("swap_pct", "?")))

    timers = state_summary.get("timers", [])
    if timers:
        lines.append("")
        for t in timers:
            status = "OK" if t.get("ok") else "DELAY"
            lines.append(f"Timer {t['name']:<22} {status:>6}  {t.get('detail','')}")

    probes = state_summary.get("probes", [])
    if probes:
        lines.append("")
        for p in probes:
            t1 = "OK" if p.get("t1_ok") else "FAIL"
            t2 = p.get("t2_detail", "?")
            lines.append(f"LLM :{p['port']:<4} T1={t1:<4} T2={t2}")

    metrics = state_summary.get("metrics", {})
    if metrics:
        lines.append("")
        for port, m in metrics.items():
            gen = f"{m['gen_tps']:.1f}t/s" if m['gen_tps'] > 0 and m['gen_tps'] < float('inf') else "-"
            lines.append(f"Metrics :{port:<4} proc={m['processing']} def={m['deferred']} "
                         f"prompt={m['prompt_tps']:.1f} gen={gen} "
                         f"max_ctx={m['max_ctx']}")

    slots = state_summary.get("slots", {})
    if slots:
        lines.append("")
        for port, slot_list in slots.items():
            cache_info = []
            for s in slot_list:
                label = f"slot{s['id']}" if s['is_processing'] else f"  {s['id']}"
                cache_info.append(f"{label}={s['cache_pct']}%")
            if cache_info:
                lines.append(f"Cache :{port:<4} " + " ".join(cache_info))

    events = state_summary.get("events_30m", [])
    lines.append("")
    if events:
        lines.append(f"Events 30m: {len(events)}")
        for e in events[:5]:
            lines.append(f"  + {e.get('component','?')} {e.get('type','?')} {e.get('detail','')[:60]}")
    else:
        lines.append("Events 30m: 0")

    text = "```\n" + "\n".join(lines) + "\n```"
    _notify_all(text)


# ── Alert / Recovery ──────────────────────────────────────────────

def send_alert(component: str, state: str, detail: str) -> None:
    """State change alert (이모지 없음, 텍스트 전용)."""
    now_kst = kst_now()
    text = f"[{now_kst}] {component} -> {state}\n{detail[:200]}"
    _notify_all(text)


def send_recovery(component: str, detail: str) -> None:
    """Recovery notice."""
    now_kst = kst_now()
    text = f"[{now_kst}] {component} recovered ({detail})"
    _notify_all(text)
