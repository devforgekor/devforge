#!/usr/bin/env python3
# Status: experimental
# Path: none — watchdog for 5-phase experiment
"""Pipeline watchdog — polls log, checks health, sends Slack on each phase/result.

KST (UTC+9) for user-facing timestamps.
Usage: python3 watchdog_pipeline.py <log_file> [--interval 60]
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

LOG_FILE = sys.argv[1] if len(sys.argv) > 1 else None
POLL_INTERVAL = int(sys.argv[sys.argv.index("--interval") + 1]) if "--interval" in sys.argv else 120
SLACK_SECRETS = os.path.expanduser("~/.config/devforge/secrets.env")

PHASES = [
    "Phase 0: Boot",
    "Phase 1: Python Verify",
    "Phase 2: 7B Verify",
    "Phase 3: 7B-3B-7B Day PRJ",
    "Phase 4: Night P-R-J",
    "Phase 5: 27B IQ4_XS Verify",
    "Phase 6: Consolidated Feedback",
    "Phase 7: Restore Day",
]

# Phase -> Korean label for Slack
PHASE_KO = {
    "Phase 0": "부트 (Pod A + B day)",
    "Phase 1": "파이썬 검증",
    "Phase 2": "7B 검증",
    "Phase 3": "데이 PRJ (7B-3B-7B)",
    "Phase 4": "나이트 P-R-J (30B→14B→NextCoder)",
    "Phase 5": "27B 검증",
    "Phase 6": "통합 피드백",
    "Phase 7": "데이 모드 복원",
}


def kst_now() -> str:
    return datetime.now(KST).strftime("%m/%d %H:%M:%S")


def send_slack(msg: str) -> None:
    if not os.path.exists(SLACK_SECRETS):
        return
    # source env vars
    env = {}
    with open(SLACK_SECRETS) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    token = env.get("SLACK_BOT_TOKEN", "")
    channel = env.get("SLACK_CHANNEL", "U0APJGD8CBW")
    if not token:
        return
    import urllib.request
    payload = json.dumps({"channel": channel, "text": msg}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-type": "application/json",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def health_check(port: int, label: str) -> str:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            return "✅" if r.status == 200 else "⚠️"
    except Exception:
        return "❌"


def get_pipeline_pid() -> int:
    try:
        out = subprocess.check_output(["pgrep", "-f", "night.py --all"], timeout=10)
        return int(out.decode().strip().split("\n")[0])
    except Exception:
        return 0


def count_eval_outputs() -> dict:
    """Count intermediate JSON outputs in eval dir."""
    import glob
    evals = sorted(glob.glob("/opt/projects/server/data/eval/pipeline_*.json"))
    phases_found = {}
    for f in evals:
        name = os.path.basename(f)
        if name.startswith("pipeline_01_"):
            phases_found["py_verify"] = f
        elif name.startswith("pipeline_02_"):
            phases_found["sevenb_verify"] = f
        elif name.startswith("pipeline_03_"):
            phases_found["day_prj"] = f
        elif name.startswith("pipeline_04_night_prj_sub_p"):
            phases_found["night_p"] = f
        elif name.startswith("pipeline_04_night_prj_sub_r"):
            phases_found["night_r"] = f
        elif name.startswith("pipeline_04_night_prj_"):
            phases_found["night_prj"] = f
        elif name.startswith("pipeline_05_"):
            phases_found["verify"] = f
        elif name.startswith("pipeline_06_"):
            phases_found["feedback"] = f
    return phases_found


def read_results_summary(fpath: str) -> str:
    """Read a result file and return a one-line summary."""
    try:
        with open(fpath) as f:
            data = json.load(f)
    except Exception:
        return "?"
    if isinstance(data, dict):
        # Try to extract findings/verdicts count
        for key in ("findings", "verdicts", "verification_items", "proposals"):
            val = data.get(key, data.get("result", {}).get(key, None))
            if val is not None and isinstance(val, list):
                return f"{len(val)}건"
        # Phase 4 summary
        p = data.get("p", {})
        r = data.get("r", {})
        j = data.get("j", {})
        if p:
            return f"P:{p.get('proposals',0)}건 R:{r.get('verdicts',0)}건"
        verdict = data.get("result", {}).get("final_verdict", data.get("final_verdict", "?"))
        if verdict:
            conf = data.get("result", {}).get("confidence", data.get("confidence", "?"))
            return f"판정={verdict} conf={conf}"
    return "?"


def main():
    if not LOG_FILE or not os.path.exists(LOG_FILE):
        print("Usage: watchdog_pipeline.py <log_file> [--interval N]")
        sys.exit(1)

    last_phase = ""
    last_line_count = 0
    phase_start_time = time.monotonic()
    pid = get_pipeline_pid()

    send_slack(f"🤖 *파이프라인 와치독 시작* ({kst_now()} KST)\n"
               f"• 로그: {LOG_FILE}\n"
               f"• PID: {pid}\n"
               f"• 인터벌: {POLL_INTERVAL}s")

    while True:
        time.sleep(POLL_INTERVAL)

        # Check if pipeline is still alive
        alive = get_pipeline_pid() > 0

        # Read log
        current_lines = 0
        try:
            with open(LOG_FILE) as f:
                content = f.read()
                current_lines = len(content.strip().split("\n")) if content.strip() else 0
        except Exception:
            content = ""

        # Detect current phase
        current_phase = ""
        for ph in reversed(PHASES):
            if ph in content:
                current_phase = ph
                break

        # Health checks
        h_8080 = health_check(8080, "Pod B")
        h_8081 = health_check(8081, "Verify")
        h_8082 = health_check(8082, "Pod A")

        # Count eval outputs
        outputs = count_eval_outputs()

        # ── Phase transition detection ──
        if current_phase != last_phase and current_phase:
            elapsed = time.monotonic() - phase_start_time
            elapsed_min = int(elapsed / 60)
            short = current_phase.split(":")[0] if ":" in current_phase else current_phase
            ko_name = PHASE_KO.get(short, current_phase)

            # Build result summary from latest output
            result_line = ""
            for key, fpath in sorted(outputs.items()):
                result_line += f"\n    • {key}: {read_results_summary(fpath)}"

            status_line = ""
            if last_phase:
                status_line = f"\n  • 이전 단계 소요: {elapsed_min}분"

            msg = (
                f"🔄 *파이프라인 단계 전환* ({kst_now()} KST)\n"
                f"  • 현재: {ko_name}{status_line}"
            )
            if result_line:
                msg += f"\n  • 중간결과:{result_line}"

            # Phase-specific details
            if "30B Proposer" in current_phase or "Night P" in current_phase:
                msg += "\n\n📌 30B(16GB) 로딩 중... Pod A 정지 완료. 약 8-10분 소요 예상."
            elif "27B" in current_phase:
                msg += "\n\n📌 27B IQ4_XS(14.7GB) 로딩 중... 약 8-10분 소요 예상."

            send_slack(msg)
            last_phase = current_phase
            phase_start_time = time.monotonic()

        # ── New lines since last check ──
        if current_lines > last_line_count:
            new_content = content.strip().split("\n")[last_line_count:] if last_line_count > 0 else []
            last_line_count = current_lines

            # Check for errors
            errors = [l for l in new_content if "error" in l.lower() or "fail" in l.lower() or "timeout" in l.lower() or "oom" in l.lower()]
            if errors:
                err_lines = "\n".join(errors[:3])
                send_slack(
                    f"⚠️ *에러 감지* ({kst_now()} KST)\n"
                    f"```{err_lines[:500]}```"
                )

        # ── Periodic health summary (every 4th poll = ~8min) ──
        if int(time.monotonic()) % (POLL_INTERVAL * 4) < POLL_INTERVAL:
            outputs = count_eval_outputs()
            result_lines = "\n".join(f"    • {k}: {read_results_summary(v)}" for k, v in sorted(outputs.items())) or "    • (없음)"
            health_str = f"8080={h_8080} 8081={h_8081} 8082={h_8082}"
            short = current_phase.split(":")[0] if ":" in current_phase else current_phase
            ko_name = PHASE_KO.get(short, current_phase)
            send_slack(
                f"📊 *중간 상태* ({kst_now()} KST)\n"
                f"  • 단계: {ko_name}\n"
                f"  • 헬스: {health_str}\n"
                f"  • PID: {'🟢' if alive else '💀'}\n"
                f"  • 중간결과:\n{result_lines}"
            )

        # ── Check if pipeline completed ──
        if not alive and "complete" in content:
            # Read total elapsed
            elapsed = ""
            for line in content.strip().split("\n"):
                if "complete in" in line.lower():
                    parts = line.split("complete in")
                    if len(parts) > 1:
                        elapsed = parts[1].strip().replace("s", "초")
            # Read final results
            outputs = count_eval_outputs()
            result_lines = "\n".join(f"    • {k}: {read_results_summary(v)}" for k, v in sorted(outputs.items())) or "    • (없음)"
            send_slack(
                f"✅ *파이프라인 완료!* ({kst_now()} KST)\n"
                f"  • 총 소요: {elapsed}\n"
                f"  • 최종 결과:\n{result_lines}"
            )
            break

        if not alive:
            send_slack(f"💀 *파이프라인 중단* ({kst_now()} KST) — 프로세스 없음")
            break


if __name__ == "__main__":
    main()
