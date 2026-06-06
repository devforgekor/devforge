#!/usr/bin/env python3
# Status: production
# Path: session end hook
"""slack_notify.py — Session-end report for all agents → Slack DM.

Queries DB for per-agent turn counts and proxy journald for token usage,
then sends a formatted summary via Slack.

Requires in ~/.config/devforge/secrets.env:
  SLACK_BOT_TOKEN=<token>
  SLACK_CHANNEL=<channel>  (optional, defaults to DevForge_Bot DM)

Intended as Claude Code SessionEnd hook.
"""

import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))
from lib.db import psql

KST = timezone(timedelta(hours=9))

_SECRETS: Dict[str, str] = {}
_SF = Path.home() / ".config/devforge/secrets.env"
if _SF.exists():
    for _line in _SF.read_text().split("\n"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _SECRETS[_k.strip()] = _v.strip().strip('"').strip("'")


def _slack_send(text: str) -> bool:
    token = _SECRETS.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("  SLACK_BOT_TOKEN not configured", file=sys.stderr)
        return False
    channel = _SECRETS.get("SLACK_CHANNEL", "U0APJGD8CBW")
    payload = json.dumps({"channel": channel, "text": text, "mrkdwn": True}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage", data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                print(f"  Slack API error: {result.get('error', '?')}", file=sys.stderr)
                return False
            return True
    except Exception as e:
        print(f"  Slack send failed: {e}", file=sys.stderr)
        return False


# ── Proxy usage parsers ────────────────────────────────────────

DEEPSEEK_RE = re.compile(
    r"input=(\d+)\s+cache_read=(\d+)\s+cache_create=(\d+)\s+output=(\d+)\s+hit_rate=(\d+)%"
)


def _proxy_usage(unit: str, since_minutes: int = 30) -> Dict[str, Any]:
    """Aggregate proxy token usage from journald for the last N minutes."""
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", unit,
             "--since", f"{since_minutes} min ago", "--no-pager"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        return {"label": unit, "error": str(e), "requests": 0}

    totals: Dict[str, int] = {"input": 0, "cache_read": 0, "output": 0}
    hit_rates: List[int] = []
    count = 0
    for line in r.stdout.split("\n"):
        m = DEEPSEEK_RE.search(line)
        if m:
            count += 1
            totals["input"] += int(m.group(1))
            totals["cache_read"] += int(m.group(2))
            totals["output"] += int(m.group(4))
            hit_rates.append(int(m.group(5)))

    avg_hit = round(sum(hit_rates) / len(hit_rates)) if hit_rates else 0
    return {"label": unit, "requests": count, **totals, "avg_hit_rate": avg_hit}


# ── DB queries ─────────────────────────────────────────────────

def _agents() -> List[Dict[str, Any]]:
    """Per-agent summary: last session, turn count, tokens if available."""
    agents: List[Dict[str, Any]] = []
    rows = psql("""
        SELECT a.agent, a.last_session, a.turns, a.tokens
        FROM (
            SELECT t.agent,
                   MAX(c.created_at)::timestamptz AT TIME ZONE 'Asia/Seoul' AS last_session,
                   COUNT(*)::int AS turns,
                   COALESCE(SUM((t.meta->>'tokens')::int), 0) AS tokens
            FROM turns t
            JOIN conversations c ON t.conversation_id = c.id
            GROUP BY t.agent
        ) a
        ORDER BY a.turns DESC
    """)
    if not rows:
        return agents
    for line in rows.split("\n"):
        if "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) >= 4:
            agents.append({
                "name": parts[0],
                "last": parts[1] if parts[1] and parts[1] != "None" else None,
                "turns": int(parts[2]),
                "tokens": int(parts[3]),
            })
    return agents


def _recent_turns(minutes: int = 30) -> int:
    """Count turns ingested in the last N minutes across all agents."""
    r = psql(f"""
        SELECT COUNT(*) FROM turns
        WHERE created_at > NOW() - INTERVAL '{minutes} minutes'
    """)
    return int(r.strip()) if r and r.strip().isdigit() else 0


def _balance() -> Optional[str]:
    try:
        import http.client
        api_key = _SECRETS.get("DEEPSEEK_API_KEY") or _SECRETS.get("ANTHROPIC_AUTH_TOKEN")
        if not api_key:
            return None
        conn = http.client.HTTPSConnection("api.deepseek.com", timeout=5)
        conn.request("GET", "/user/balance", headers={"Authorization": f"Bearer {api_key}"})
        resp = conn.getresponse()
        if resp.status == 200:
            data = json.loads(resp.read())
            for bi in data.get("balance_infos", []):
                if bi.get("currency") == "CNY":
                    return bi.get("topped_up_balance", "?")
    except Exception:
        pass
    return None


# ── Report builder ─────────────────────────────────────────────

def main() -> int:
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    agents = _agents()
    recent = _recent_turns(30)
    bal = _balance()

    lines = [f"*DevForge — Session Report* ({now_kst} KST)", ""]

    # Proxy usage (DeepSeek = Claude Code)
    proxy = _proxy_usage("anthropic-proxy", 30)
    if proxy.get("requests", 0) > 0:
        hit = proxy["avg_hit_rate"]
        bar = "█" * (hit // 10) + "░" * (10 - hit // 10)
        lines.append(f">*Claude Code (DeepSeek V4 Flash)*")
        lines.append(f">  API calls: {proxy['requests']}")
        lines.append(f">  Input: {proxy['input']:,} tok | Cache read: {proxy['cache_read']:,} tok")
        lines.append(f">  Output: {proxy['output']:,} tok | Hit rate: {hit}% {bar}")
        lines.append("")
    elif recent == 0:
        lines.append(">No API activity in last 30 min")
        lines.append("")

    # Per-agent turns
    turn_total = sum(a["turns"] for a in agents)
    if turn_total > 0:
        lines.append(f"*Agents — {turn_total:,} total turns*")
        now = datetime.now(KST)
        for a in agents:
            when = ""
            if a["last"]:
                try:
                    last_dt = datetime.fromisoformat(a["last"])
                    delta = now - last_dt
                    if delta.total_seconds() < 3600:
                        when = f" ({int(delta.total_seconds()/60)}m ago)"
                    elif delta.days < 1:
                        when = f" ({int(delta.total_seconds()/3600)}h ago)"
                    else:
                        when = f" ({delta.days}d ago)"
                except Exception:
                    pass
            tok = f" | {a['tokens']:,} tok" if a["tokens"] else ""
            lines.append(f"  `{a['name']:12s}` {a['turns']:5d} turns{tok}{when}")
        lines.append("")

    if bal:
        usd = float(bal) * 0.14
        lines.append(f"DeepSeek balance: ¥{bal} (~${usd:.2f})")

    text = "\n".join(lines)
    print(text, file=sys.stderr)

    if proxy.get("requests", 0) == 0 and turn_total == 0:
        print("  Nothing to report — skipping", file=sys.stderr)
        return 0

    return 0 if _slack_send(text) else 1


if __name__ == "__main__":
    sys.exit(main())
