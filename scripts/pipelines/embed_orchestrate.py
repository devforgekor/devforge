#!/usr/bin/env python3.12
# Status: experimental
# Path: day_cycle.sh (embedding stage wrapper) — ≤10 lines logic
"""Wrapper for devforge pipeline orchestrate — replaces embed_batch.py for turns."""

import subprocess
import sys

def main() -> int:
    # Pass through all args to devforge pipeline orchestrate
    # day_cycle.sh calls this with: --limit N --budget-sec N [--shadow] [--shadow-since TS]
    cmd = ["devforge", "pipeline", "orchestrate"] + sys.argv[1:]
    return subprocess.run(cmd).returncode

if __name__ == "__main__":
    sys.exit(main())