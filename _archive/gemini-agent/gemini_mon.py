#!/usr/bin/env python3
"""Compact Gemini key monitor — shows the most recently used key, refreshes 5s."""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
STATE_FILE = os.path.expanduser("~/.cache/devforge/gemini_rotator_state.json")

_save_err = sys.stderr
sys.stderr = open(os.devnull, "w")
try:
    from lib.auth.key_loader import load_api_keys
    KEYS = load_api_keys()
except Exception:
    KEYS = []
sys.stderr.close()
sys.stderr = _save_err


def render():
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            state = json.loads(open(STATE_FILE).read())
        except Exception:
            pass

    calls = state.get("calls", {})
    last_used = state.get("last_used", {})
    backoff = state.get("backoff_until", {})

    now = time.time()

    # Most recently used key
    active_idx = 0
    active_ts = 0
    for i in range(len(KEYS)):
        lu = last_used.get(str(i), 0)
        if lu > active_ts:
            active_ts = lu
            active_idx = i

    name, _ = KEYS[active_idx]
    acct_part = name.rsplit("_gemini_", 1)[0] if "_gemini_" in name else name
    key_suffix = name.rsplit("_", 1)[-1]
    si = str(active_idx)
    c = calls.get(si, 0)
    bu = backoff.get(si, 0)

    # Remaining %: approximate daily quota (default 50 calls/key/day)
    LIMIT = int(os.environ.get("GEMINI_DAILY_LIMIT", "50"))
    used = min(c, LIMIT)
    pct = max(0, 100 - int(used / LIMIT * 100))
    if bu > now:
        pct = 0

    if pct >= 80:
        pct_color = "\033[92m"
    elif pct >= 50:
        pct_color = "\033[93m"
    else:
        pct_color = "\033[91m"

    if bu > now:
        h, r = divmod(int(bu - now), 3600)
        m, s = divmod(r, 60)
        if h > 0:
            rem = f"{h}h{m:02d}m"
        elif m > 0:
            rem = f"{m}m{s:02d}s"
        else:
            rem = f"{s}s"
        status = f"\033[91mBACKOFF {rem}\033[0m"
    else:
        status = f"\033[92mREADY\033[0m"

    ts = datetime.now(tz=KST).strftime("%H:%M:%S")
    bar_filled = "█" * (pct // 10) + "░" * (10 - pct // 10)
    return (
        f" [{ts}] \033[1m{acct_part}_{key_suffix}\033[0m"
        f"  {pct_color}{bar_filled} {pct:3d}%\033[0m"
        f"  {status}"
    )


def main():
    try:
        while True:
            out = render()
            print(f"\r\033[K{out}", end="", flush=True)
            time.sleep(5)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
