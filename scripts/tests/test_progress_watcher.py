#!/usr/bin/env python3
# Status: experimental
# Path: none — manual background watcher for 14B comparison test
"14B Test Progress Watcher — reads test log, sends Slack updates."
import json, os, re, sys, time, urllib.request

from lib.test_common import test_setup, test_heartbeat

LOG_PATH = "/tmp/14b_test1_10axis.log"
RESULTS_PATH = "/tmp/14b_comparison_results.json"
SLACK_TOKEN = None
SLACK_CHANNEL = None

# Load secrets
sf = os.path.expanduser("~/.config/devforge/secrets.env")
if os.path.exists(sf):
    for line in open(sf).read().split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        if k.strip() == "SLACK_BOT_TOKEN":
            SLACK_TOKEN = v
        elif k.strip() == "SLACK_CHANNEL":
            SLACK_CHANNEL = v

if not SLACK_TOKEN:
    print("ERROR: SLACK_BOT_TOKEN not found")
    sys.exit(1)

last_sent_progress = ""  # avoid duplicate messages

def slack_post(text):
    """Send a Slack message via chat.postMessage."""
    try:
        payload = json.dumps({
            "channel": SLACK_CHANNEL,
            "text": text,
        }).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=payload,
            headers={
                "Authorization": f"Bearer {SLACK_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status == 200
    except Exception as e:
        print(f"Slack post failed: {e}")
        return False


def read_progress():
    """Read test log and extract current progress."""
    if not os.path.exists(LOG_PATH):
        return None, "waiting for log..."

    with open(LOG_PATH) as f:
        lines = f.readlines()

    # Find model progress lines
    models_done = []
    current = None
    for line in lines:
        # Model start: [N/5] Model Name
        m = re.search(r'\[(\d+)/5\]\s+(.+?)\s+\(', line)
        if m:
            current = m.group(2)
        # Case done: Extract: N facts
        case_m = re.search(r'Extract:\s+(\d+)\s+facts', line)
        # MCP done
        mcp_m = re.search(r'MCP:\s+score=(\d+)', line)
        # Verify done
        vfy_m = re.search(r'Verify:\s+verdict=(\S+)', line)
        # Model done: Saved to
        saved_m = re.search(r'Saved to.*\((\w+)\s+done\)', line)
        if saved_m and current:
            models_done.append((current, saved_m.group(1)))
            current = None

    # Find the most recent stats
    results = {"models_done": models_done, "current": current, "lines": len(lines)}

    # Check results file for scores
    if os.path.exists(RESULTS_PATH):
        try:
            with open(RESULTS_PATH) as f:
                data = json.load(f)
            for tag, v in data.items():
                if isinstance(v, dict) and "cases" in v:
                    n_cases = len(v["cases"])
                    model_name = v.get("model", {}).get("name", tag)
                    # Try compute_10axis if all 6 cases done
                    if n_cases == 6 and "model" in v:
                        # Just report basic stats
                        ev = sum(c["extract"].get("evidence_matched", 0) for c in v["cases"])
                        ref_n = sum(c["extract"].get("ref_n", 0) for c in v["cases"])
                        results.setdefault("score_preview", []).append(
                            f"{model_name}: {ev}/{ref_n} evidence, {n_cases}/6 cases"
                        )
                    elif n_cases < 6 and n_cases > 0:
                        results.setdefault("score_preview", []).append(
                            f"{model_name}: {n_cases}/6 cases done"
                        )
        except Exception:
            pass

    return results, None


def format_message(results):
    """Format progress into a Slack message."""
    if results is None:
        return "🔍 14B Test 1 (10-Axis) — waiting for log..."

    lines = ["*14B 5-Model Extract Test 1 (10-Axis — no rubric)*"]
    lines.append(f"Log lines: {results['lines']}")

    if results.get("current"):
        lines.append(f"🔄 Current: *{results['current']}*")

    if results.get("models_done"):
        done_str = ", ".join(f"{n}({t})" for n, t in results["models_done"])
        lines.append(f"✅ Done: {done_str}")

    if results.get("score_preview"):
        lines.append("📊 Scores:")
        for s in results["score_preview"]:
            lines.append(f"  • {s}")

    return "\n".join(lines)


# Main loop
TEST = test_setup("test_progress_watcher", "Background watcher for 14B comparison test")
print(f"Watcher started. Monitoring {LOG_PATH}")
time.sleep(30)  # Let test boot up

last_msg = ""
while True:
    try:
        results, err = read_progress()
        if err:
            print(f"Read error: {err}")
            time.sleep(60)
            continue

        msg = format_message(results)
        if msg != last_msg:
            print(f"[{time.strftime('%H:%M:%S')}] Sending update...")
            if slack_post(msg):
                last_msg = msg
                print("  Sent OK")
            else:
                print("  Send FAILED")

        # Check if test is complete
        if os.path.exists(RESULTS_PATH):
            try:
                with open(RESULTS_PATH) as f:
                    data = json.load(f)
                all_done = all(
                    isinstance(v, dict) and len(v.get("cases", [])) == 6
                    for v in data.values()
                    if isinstance(v, dict)
                )
                # Check if process still running
                import subprocess
                r = subprocess.run(["pgrep", "-f", "model_comparison_test.py"],
                                   capture_output=True, text=True)
                if all_done and (not r.stdout.strip()):
                    print("Test complete!")
                    slack_post("✅ *14B Test 1 Complete!* — Results in /tmp/14b_comparison_results.json")
                    break
            except Exception:
                pass

    except Exception as e:
        print(f"Loop error: {e}")

    time.sleep(120)  # Check every 2 minutes
