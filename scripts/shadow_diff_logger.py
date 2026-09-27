#!/usr/bin/env python3.12
# Status: experimental
# Path: systemd user devforge-shadow-diff.timer (hourly) or manual — Phase 3 shadow diff evidence
"""Shadow diff sampler — JSONL evidence trail for the Phase 3 ≥14-cycle gate.

Each sample appends one JSONL record (date-partitioned file in logs/).
Default mode loops until diff=0; --once takes a single sample and exits
(timer/oneshot mode — exit 0 when the sample was recorded, 1 on error,
regardless of diff value: this collects evidence, it is not the gate).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path("/opt/projects/server/logs")
LOG_DIR.mkdir(exist_ok=True)
JSONL_FILE = LOG_DIR / f"shadow_diff_{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"

SINCE = "2026-09-20T00:00:00"
INTERVAL_SEC = 300  # 5분

# [WHY] shadow_diff counters may carry a trailing note, e.g.
# "text mismatch  : 583 (excluded: 583)" — take the first number after ':'.
_COLON_NUM = re.compile(r":\s*(\d+)")
_DIFF_NUM = re.compile(r"diff=(\d+)")


def parse_metrics(output: str) -> dict:
    """Extract counters from shadow_diff.py text output."""
    data: dict = {}
    for line in output.split("\n"):
        if "prod chunks" in line:
            data["prod_chunks"] = int(_COLON_NUM.search(line).group(1))
        elif "shadow chunks" in line:
            data["shadow_chunks"] = int(_COLON_NUM.search(line).group(1))
        elif "matched" in line and "missing" not in line:
            data["matched"] = int(_COLON_NUM.search(line).group(1))
        elif "missing(real)" in line:
            data["missing_real"] = int(_COLON_NUM.search(line).group(1))
        elif "text mismatch" in line:
            data["text_mismatch"] = int(_COLON_NUM.search(line).group(1))
        elif "vector mismatch" in line:
            data["vector_mismatch"] = int(_COLON_NUM.search(line).group(1))
        elif line.startswith("RESULT:"):
            m = _DIFF_NUM.search(line)
            if m:
                data["diff"] = int(m.group(1))
                data["passed"] = "✅" in line
    return data


def run_shadow_diff(since: str = SINCE) -> dict:
    """Run shadow_diff.py and parse output."""
    result = subprocess.run(
        [
            "python3",
            "/opt/projects/server/scripts/shadow_diff.py",
            "--mode",
            "embed",
            "--since",
            since,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    data = {"ts": datetime.now(timezone.utc).isoformat(), "raw": result.stdout}
    data.update(parse_metrics(result.stdout))
    return data


def write_jsonl(data: dict) -> None:
    """Append to JSONL file."""
    with open(JSONL_FILE, "a") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def write_obs(data: dict) -> None:
    """Write to DB via MCP obs_write (disabled - endpoint TBD)."""
    # TODO: Fix MCP endpoint when available
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Shadow diff JSONL sampler")
    parser.add_argument("--once", action="store_true", help="single sample then exit (timer mode)")
    parser.add_argument("--interval", type=int, default=INTERVAL_SEC, help="loop interval seconds")
    parser.add_argument("--since", default=SINCE, help="parity window (ISO timestamp)")
    args = parser.parse_args()

    print(f"Shadow diff logger started — interval={args.interval}s, log={JSONL_FILE}")
    print(f"Since: {args.since}")

    while True:
        try:
            data = run_shadow_diff(args.since)
            write_jsonl(data)
            write_obs(data)

            status = "✅ PASS" if data.get("passed") else f"❌ diff={data.get('diff', '?')}"
            print(
                f"[{data['ts']}] {status} | shadow={data.get('shadow_chunks', '?')}/"
                f"{data.get('prod_chunks', '?')} | missing_real={data.get('missing_real', '?')} | "
                f"mismatch={data.get('text_mismatch', '?')}+{data.get('vector_mismatch', '?')}"
            )
            if args.once:
                return 0
            if data.get("passed"):
                print("🎉 diff=0 achieved!")
                return 0

        except Exception as e:
            print(f"[error] {e}", file=sys.stderr)
            if args.once:
                return 1

        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
