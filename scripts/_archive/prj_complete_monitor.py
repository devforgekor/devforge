#!/usr/bin/env python3
# Status: deprecated
# Path: _archive/prj_complete_monitor.py — orphan, no callers. Remove after 2026-07-06.
"""prj_complete_monitor.py — PID 595297 완료 대기 → 결과 정리 → Azure Blob 업로드"""
import json, os, subprocess, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

PID = 667216
EXPER_DIR = "/opt/projects/server/data/experiment"
STORAGE_ACCOUNT = "stshareddevforgeprodkrc"
CONTAINER = "devforge"

# Slack
_SF = Path.home() / ".config/devforge/secrets.env"
_SLACK_TOKEN = ""
_SLACK_CHANNEL = "U0APJGD8CBW"
if _SF.exists():
    for _line in _SF.read_text().split("\n"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _k.strip() == "SLACK_BOT_TOKEN":
                _SLACK_TOKEN = _v.strip().strip('"').strip("'")
            elif _k.strip() == "SLACK_CHANNEL":
                _SLACK_CHANNEL = _v.strip().strip('"').strip("'")

def slack(text):
    if not _SLACK_TOKEN:
        return
    payload = json.dumps({"channel": _SLACK_CHANNEL, "text": text, "mrkdwn": True}).encode()
    try:
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage", data=payload,
            headers={"Authorization": f"Bearer {_SLACK_TOKEN}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            json.loads(r.read())
    except Exception as e:
        print(f"Slack error: {e}")

def log(m):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {m}", flush=True)

# Wait for PID
log(f"Waiting for PID {PID} to complete...")
while True:
    try:
        os.kill(PID, 0)
        time.sleep(60)
    except OSError:
        break
log("PID completed. Gathering results...")
time.sleep(5)

# Collect output files
files = sorted(os.listdir(EXPER_DIR))
result_files = [f for f in files if f.startswith(("exp_", "pipeline_state_")) and f.endswith(".json")]
log(f"Found {len(result_files)} result files")

# Read pipeline state
state_path = os.path.join(EXPER_DIR, "pipeline_state_r1_norubric.json")
summary = {}
if os.path.exists(state_path):
    with open(state_path) as f:
        state = json.load(f)
    day_verify_data = state.get("day_verify", {})
    prj = state.get("prj", [])
    summary = {
        "round": 1,
        "with_rubric": False,
        "day_verify": {"verdict": day_verify_data.get("final_verdict", "?"), "confidence": day_verify_data.get("confidence", 0)},
        "prj": [{
            "rotation": r.get("rotation"),
            "P": f"{r.get('p_model','?')}({r.get('P_score','?')})",
            "R": f"{r.get('r_model','?')}({r.get('R_score','?')})",
            "decision": r.get("decision"),
            "consensus": r.get("consensus")
        } for r in prj],
        "total_approved": sum(1 for r in prj if r.get("decision") == "APPROVED")
    }

log(f"Summary: day_verify={summary.get('day_verify',{}).get('verdict','?')}, PRJ={summary.get('total_approved',0)}/3 approved")

# Upload to Azure Blob
uploaded = False
try:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    blob_path = f"prj_results_{timestamp}"
    log(f"Uploading {len(result_files)} files to {CONTAINER}/{blob_path}...")

    # az storage blob upload-batch requires a local directory
    upload_dir = os.path.join(EXPER_DIR, "_upload")
    os.makedirs(upload_dir, exist_ok=True)
    for fname in result_files:
        fpath = os.path.join(EXPER_DIR, fname)
        if os.path.getsize(fpath) > 0:
            os.symlink(fpath, os.path.join(upload_dir, fname))

    result = subprocess.run(
        ["az", "storage", "blob", "upload-batch",
         "--account-name", STORAGE_ACCOUNT,
         "-d", f"{CONTAINER}/{blob_path}",
         "-s", upload_dir,
         "--overwrite"],
        capture_output=True, text=True, timeout=300)
    if result.returncode == 0:
        uploaded = True
        log("Upload complete")
    else:
        log(f"Upload stderr: {result.stderr[:200]}")

    # Cleanup
    subprocess.run(["rm", "-rf", upload_dir], capture_output=True, timeout=10)
except Exception as e:
    log(f"Upload error: {e}")

url = f"https://{STORAGE_ACCOUNT}.blob.core.windows.net/{CONTAINER}/{blob_path}/"
msg = (
    f"*P-R-J 실험 완료*\n"
    f"> day_verify: {summary.get('day_verify',{}).get('verdict','?')} (conf={summary.get('day_verify',{}).get('confidence','?')})\n"
    f"> P-R-J: {summary.get('total_approved',0)}/3 approved\n"
    f"{'Azure Blob: ' + url if uploaded else f'로컬: `{EXPER_DIR}/` (' + str(len(result_files)) + '개 파일)'}"
)
slack(msg)
log("Done.")
