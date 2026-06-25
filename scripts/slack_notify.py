#!/usr/bin/env python3
# Status: production
# Path: tmux client-detached hook
"""slack_notify.py — Session-end report → Slack DM.

Queries proxy journald for this tmux session's token usage,
sends a formatted summary via Slack.

Requires in ~/.config/devforge/secrets.env:
  SLACK_BOT_TOKEN=<token>
  SLACK_CHANNEL=<channel>  (optional, defaults to DevForge_Bot DM)
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

KST = timezone(timedelta(hours=9))

# DeepSeek V4 Flash pricing (USD per 1M tokens)
PRICING = {
    "input": 0.14,       # cache miss
    "cache_read": 0.0028,  # cache hit (98% discount)
    "output": 0.28,
}
USD_TO_CNY = 0.14  # approximate conversion for balance display

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



DEEPSEEK_RE = re.compile(
    r"input=(\d+)\s+cache_read=(\d+)\s+cache_miss=(\d+)\s+output=(\d+)\s+hit_rate=(\d+)%"
)
STATS_NONE_RE = re.compile(
    r"input=(\d+)\s+output=(\d+)\s+stats=none"
)


def _proxy_usage(unit: str, since_ts: Optional[int] = None) -> Dict[str, Any]:
    """Aggregate proxy token usage from journald since given Unix timestamp."""
    try:
        since = f"@{since_ts}" if since_ts else "30 min ago"
        r = subprocess.run(
            ["journalctl", "--user", "-u", unit,
             "--since", since, "--no-pager"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        return {"label": unit, "error": str(e), "requests": 0}

    totals: Dict[str, int] = {"input": 0, "cache_read": 0, "cache_miss": 0, "output": 0}
    hit_rates: List[int] = []
    count = 0
    none_count = 0
    for line in r.stdout.split("\n"):
        m = DEEPSEEK_RE.search(line)
        if m:
            count += 1
            totals["input"] += int(m.group(1))
            totals["cache_read"] += int(m.group(2))
            totals["output"] += int(m.group(4))
            hit_rates.append(int(m.group(5)))
            continue
        m2 = STATS_NONE_RE.search(line)
        if m2:
            none_count += 1
            totals["input"] += int(m2.group(1))
            totals["output"] += int(m2.group(2))

    avg_hit = round(sum(hit_rates) / len(hit_rates)) if hit_rates else 0
    return {"label": unit, "requests": count, **totals, "avg_hit_rate": avg_hit, "stats_none": none_count}



def _compact(n: int) -> str:
    """Format token count: <1M → '123k', ≥1M → '12M', <1000 → '500'."""
    if n < 1000:
        return str(n)
    if n >= 1_000_000:
        return f"{round(n / 1_000_000):,}M"
    return f"{round(n / 1000):,}k"


def _calc_cost(t: Dict[str, int]) -> Dict[str, float]:
    """Calculate DeepSeek V4 Flash cost in USD."""
    def _cny(usd: float) -> float:
        return usd / USD_TO_CNY
    inp_u = t.get("input", 0) * PRICING["input"] / 1_000_000
    cache_u = t.get("cache_read", 0) * PRICING["cache_read"] / 1_000_000
    out_u = t.get("output", 0) * PRICING["output"] / 1_000_000
    tot_u = inp_u + cache_u + out_u
    return {"usd": inp_u, "cny": _cny(inp_u),
            "cache_usd": cache_u, "cache_cny": _cny(cache_u),
            "out_usd": out_u, "out_cny": _cny(out_u),
            "total_usd": tot_u, "total_cny": _cny(tot_u)}


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



def main() -> int:
    args = _parse_args()
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M")

    # client-detached fires for every detach; skip if session still has clients
    if args.session_name:
        try:
            clients = subprocess.run(
                ["tmux", "list-clients", "-t", args.session_name],
                capture_output=True, text=True, timeout=5,
            )
            if clients.stdout.strip():
                print(f"  Session '{args.session_name}' still has clients — skip", file=sys.stderr)
                return 0
        except FileNotFoundError:
            pass  # tmux not in PATH (standalone test)
        except Exception:
            pass

    bal = _balance()

    lines = [f"*DevForge — Session Report* ({now_kst} KST)", ""]

    proxy = _proxy_usage("anthropic-proxy", args.session_start)
    total_req = proxy["requests"] + proxy["stats_none"]

    if total_req > 0:
        none_str = f" | stats none: {proxy['stats_none']}" if proxy["stats_none"] else ""
        lines.append(f">*Claude Code* (DeepSeek V4 Flash)")
        lines.append(f">  Calls: {total_req}{none_str}")

        cost = _calc_cost(proxy)

        lines.append(f">  Input: {_compact(proxy['input']):>7} tok  ¥{cost['cny']:.2f} (${cost['usd']:.2f})")
        lines.append(f">  Output: {_compact(proxy['output']):>7} tok  ¥{cost['out_cny']:.2f} (${cost['out_usd']:.2f})")
        lines.append(f">  Cache: {_compact(proxy['cache_read']):>7} tok  ¥{cost['cache_cny']:.2f} (${cost['cache_usd']:.2f})")

        hit = proxy["avg_hit_rate"]
        lines.append(f">  Hit rate: {hit}%")
        lines.append(f">  *Total: ¥{cost['total_cny']:.2f} (${cost['total_usd']:.2f})*")
        lines.append("")
    else:
        lines.append(">No API activity in this session")
        lines.append("")

    if bal:
        usd = float(bal) * USD_TO_CNY
        lines.append(f"DeepSeek balance: ¥{bal} (~${usd:.2f})")

    text = "\n".join(lines)
    print(text, file=sys.stderr)

    if total_req == 0:
        print("  Nothing to report — skipping", file=sys.stderr)
        return 0

    return 0 if _slack_send(text) else 1


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--session-name", type=str, default=None,
                   help="Tmux session name (to check if last client)")
    p.add_argument("--session-start", type=int, default=None,
                   help="Unix timestamp of tmux session creation")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(main())
