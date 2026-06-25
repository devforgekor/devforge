#!/usr/bin/env python3
# Status: experimental
"""Night Cycle V2: Driver for P-R-J pipeline with dry-run support.
"""

import os
import sys
import argparse
import time
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

def log(msg: str) -> None:
    log_ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{log_ts}] {msg}", flush=True)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Simulate execution")
    args = parser.parse_args()

    log("=" * 60)
    log(f"DevForge Night Cycle V2 {'(DRY RUN)' if args.dry_run else ''}")
    log("=" * 60)

    # In Night Cycle, we run night_debate_pipeline.py
    # For dry-run, we skip actual heavy swap and just verify logic.
    
    import subprocess
    cmd = [sys.executable, "-u", "/opt/projects/server/scripts/pipelines/night_debate_pipeline.py"]
    if args.dry_run:
        cmd.append("--dry-run")
    
    log(f"[exec] {' '.join(cmd)}")
    
    # Run with limited output for confirmation
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    
    count = 0
    for line in process.stdout:
        print(line, end='', flush=True)
        count += 1
        if "NIGHT PIPELINE COMPLETE" in line or "Experiment complete" in line:
             break
        if count > 200: # safety break
             break
    
    process.terminate()
    log("=" * 60)
    log("Night Cycle V2 execution finished (or intercepted)")
    log("=" * 60)

if __name__ == "__main__":
    main()
