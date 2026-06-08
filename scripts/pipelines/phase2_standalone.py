#!/usr/bin/env python3
# Status: deprecated
# Path: none — replaced by day_cycle.py (15m_cycle.sh heavy phase), remove after 2026-07
"""Phase 2 standalone runner with reliable logging."""
import sys, os, json, time
sys.path.insert(0, "/opt/projects/server/scripts")

LOGFILE = "/tmp/phase2-v5.log"

class ReliableLog:
    def __init__(self, path):
        self.f = open(path, "w", buffering=1)
    def write(self, msg):
        ts = time.strftime("%H:%M:%S", time.gmtime())
        line = f"[{ts}] {msg}"
        self.f.write(line + "\n")
        self.f.flush()
        os.fsync(self.f.fileno())
        print(line, flush=True)

log = ReliableLog(LOGFILE).write

log("=== Phase 2 Standalone ===")
log("PID: {}".format(os.getpid()))

# Kill stale night.py
import signal, subprocess
current = os.getpid()
for pid_str, comm in [ln.strip().split(None, 1) for ln in subprocess.check_output(["ps", "-e", "-o", "pid=,comm="], timeout=10, text=True).strip().split("\n") if ln.strip()]:
    try:
        pid = int(pid_str)
    except ValueError:
        continue
    if pid == current:
        continue
    if "python3" not in comm:
        continue
    try:
        with open(f"/proc/{pid}/cmdline") as cf:
            cmdline = cf.read().replace("\0", " ")
        if "night.py" not in cmdline:
            continue
    except (OSError, IOError):
        continue
    os.kill(pid, signal.SIGTERM)
    log(f"Killed stale PID {pid}")

os.chdir("/opt/projects/server/scripts/pipelines")

log("Importing night modules...")
from lib.llm_client import call_llm, MODEL_REGISTRY
# Import specific Phase 2 pieces
import importlib.util as iutil
spec = iutil.spec_from_file_location("night", "/opt/projects/server/scripts/pipelines/night.py")
night = iutil.module_from_spec(spec)
spec.loader.exec_module(night)

# Load data
log("Loading eval data...")
all_data = night.load_eval_data()
log(f"Loaded {all_data['meta']['total_findings']} findings from {all_data['meta']['total_files']} files")

# Run Phase 2
log("Running phase_2_sevenb_verify...")
result = night.phase_2_sevenb_verify(all_data)
log(f"Phase 2 complete!")
log(f"  verification_items: {len(result.get('result', {}).get('verification_items', []))}")
log(f"  category_summary: {result.get('category_summary', 'NONE')[:100]}")

# Save output
night.save_output("02_sevenb_verify", result)
log("=== ALL DONE ===")
