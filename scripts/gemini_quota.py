#!/usr/bin/env python3.11
"""Display Gemini API daily quota status."""

import json
import os
import sys
from urllib.request import urlopen, Request

PROXY_URL = os.environ.get("GEMINI_OPENAI_PROXY_URL", "http://127.0.0.1:4431")


def fmt_k(n: int) -> str:
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}k".replace(".0k", "k")


def bar(used: int, limit: int, width: int = 15) -> str:
    pct = min(1.0, used / limit) if limit else 0
    filled = round(pct * width)
    empty = width - filled
    return "█" * filled + "░" * empty


def main():
    req = Request(f"{PROXY_URL}/v1/quota", headers={"Accept": "application/json"})
    try:
        resp = urlopen(req, timeout=5)
        data = json.loads(resp.read())
    except Exception as e:
        print(f"\033[31m⚠ 프록시 연결 실패: {e}\033[0m")
        sys.exit(1)

    date = data["date"]
    total = data["total_keys"]
    avail = data["available_keys"]
    total_req = data["total_requests_today"]
    total_tokens = data["total_tokens"]

    # Group keys by account
    accounts: dict[str, list[dict]] = {}
    for k in data["keys"]:
        name: str = k["name"]
        # Extract account prefix (e.g., "mesids_senedu" from "mesids_senedu_gemini_09")
        parts = name.rsplit("_gemini_", 1)
        acct = parts[0] if len(parts) == 2 else name
        suffix = f"{parts[1]}" if len(parts) == 2 else ""
        entry = {**k, "suffix": suffix}
        accounts.setdefault(acct, []).append(entry)

    color = "\033[32m" if avail == total else "\033[33m"
    reset = "\033[0m"
    print(f"\n{color}Gemini Quota ({date}){reset} — {avail}/{total} keys available")
    print(f"{'─' * 55}")

    for acct in sorted(accounts):
        keys = accounts[acct]
        for k in keys:
            used = k["requests_used"]
            limit = k["rpd_limit"]
            remaining = k["requests_remaining"]
            pct = (used / limit) * 100 if limit else 0
            pct_str = f"{100 - pct:5.1f}%" if remaining > 0 else "\033[31m  EXHAUSTED\033[0m"
            acct_label = acct if k == keys[0] else ""
            print(
                f"  {acct_label:<24} {k['suffix']:<2} "
                f"{bar(used, limit)} "
                f"{used:>4}/{limit:<4} "
                f"{pct_str}"
            )

    print(f"{'─' * 55}")
    print(f"  Total: {fmt_k(total_req)} requests | {fmt_k(total_tokens)} tokens today")
    print()


if __name__ == "__main__":
    main()
