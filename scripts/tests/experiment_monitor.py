#!/usr/bin/env python3
# Status: experimental
# Path: none — manual background experiment status reporter to Slack
"Experiment monitor — background Slack status reporter for long experiments."

import json
import os
import subprocess
import time
import urllib.request

from lib.test_common import test_complete, test_setup

SERVER_DIR = "/opt/projects/server"
EXPER_DIR = os.path.join(SERVER_DIR, "data", "experiment")
RUNNER_PID_FILE = os.path.join(EXPER_DIR, "exp_runner.pid")
RUNNER_LOG = os.path.join(EXPER_DIR, "exp_5phase.log")

# Slack
SLACK_TOKEN = ""
SLACK_CHANNEL = ""
_sf = os.path.join(os.path.expanduser("~"), ".config/devforge/secrets.env")
if os.path.exists(_sf):
    for line in open(_sf).read().split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            if k.strip() == "SLACK_BOT_TOKEN":
                SLACK_TOKEN = v
            elif k.strip() == "SLACK_CHANNEL":
                SLACK_CHANNEL = v


def slack_send(text):
    if not SLACK_TOKEN:
        return
    payload = json.dumps({"channel": SLACK_CHANNEL, "text": text, "mrkdwn": True}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
            if not resp.get("ok"):
                print(f"Slack error: {resp.get('error', '?')}", flush=True)
    except Exception as e:
        print(f"Slack failed: {e}", flush=True)


def get_runner_pid():
    try:
        return int(open(RUNNER_PID_FILE).read().strip())
    except:
        return None


def is_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except:
        return False


def get_phase_metrics():
    phases = {}
    for p in range(5):
        mp = os.path.join(EXPER_DIR, f"phase{p}_metrics.json")
        if os.path.exists(mp):
            with open(mp) as f:
                phases[p] = json.load(f)
    return phases


def get_llm_cpu():
    """Return %CPU of first llama-server on inference ports."""
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,%cpu,%mem,args", "--sort=-%cpu"], timeout=5, text=True
        )
        for line in out.split("\n"):
            if "llama-server" in line and "port 808" in line:
                return line.split()[1]
        return "?"
    except:
        return "?"


def get_memory():
    try:
        out = subprocess.check_output(["free", "-h"], timeout=5, text=True)
        lines = out.strip().split("\n")
        mem = lines[1].split()
        swap = lines[2].split()
        return f"{mem[2]}/{mem[1]}", f"{swap[2]}/{swap[1]}"
    except:
        return "?/", "?/"


def check_phase0_progress():
    """Check if there are any intermediate output files indicating progress."""
    recent = []
    try:
        out = subprocess.check_output(
            [
                "find",
                "/opt/projects/server/data/eval",
                "-name",
                "*.json",
                "-mmin",
                "-90",
                "-type",
                "f",
            ],
            timeout=5,
            text=True,
        )
        for line in out.strip().split("\n"):
            if line:
                fname = os.path.basename(line)
                recent.append(fname)
    except:
        pass
    return recent


def build_status():
    pid = get_runner_pid()
    alive = is_alive(pid) if pid else False

    if not alive:
        # Process dead - final report
        phases = get_phase_metrics()
        completed = [p for p, m in phases.items() if m.get("success")]
        msg = (
            f"*[Experiment Monitor]* 실험 종료됨\n"
            f"Runner PID {pid}: DEAD\n"
            f"완료된 Phase: {completed if completed else '없음'}"
        )
        # Check log tail for error
        if os.path.exists(RUNNER_LOG):
            tail = subprocess.check_output(["tail", "-20", RUNNER_LOG], timeout=5, text=True)
            msg += f"\n로그 마지막 20줄:\n```\n{tail}\n```"
        return msg

    # Alive - build progress report
    runner_elapsed = "?"
    try:
        out = subprocess.check_output(["ps", "-o", "etime=", "-p", str(pid)], timeout=5, text=True)
        runner_elapsed = out.strip()
    except:
        pass

    phases = get_phase_metrics()
    completed_phases = sorted(phases.keys())
    current_phase = completed_phases[-1] + 1 if completed_phases else 0

    # Check prj_cycle (exp_runner internal)
    prj_alive = False
    prj_elapsed = "?"
    try:
        out = subprocess.check_output(["ps", "-eo", "pid,etime,args"], timeout=5, text=True)
        for line in out.split("\n"):
            if "prj_cycle.py" in line and "grep" not in line:
                prj_alive = True
                prj_elapsed = line.split(None, 2)[1]
    except:
        pass

    inference_cpu = get_llm_cpu()
    mem, swap = get_memory()
    recent_eval = check_phase0_progress()

    # Container status
    containers = ""
    try:
        out = subprocess.check_output(
            [
                "podman",
                "ps",
                "--filter",
                "name=devforge-inference",
                "--format",
                "{{.Names}} {{.Status}}",
            ],
            timeout=5,
            text=True,
        )
        containers = out.strip()
    except:
        pass

    phase_summary = "Phase 0 (진행 중)" if current_phase == 0 else f"Phase {current_phase} 진행 중"
    if len(completed_phases) == 5:
        phase_summary = "*모든 Phase 완료!*"
    elif completed_phases:
        phase_summary = f"완료: {completed_phases}, 현재: Phase {current_phase}"

    recent_str = "\n".join(recent_eval[-5:]) if recent_eval else "(없음)"

    msg = (
        f"*[Experiment Monitor]* 진행 보고\n"
        f"• Runner: PID {pid}, {runner_elapsed} 경과\n"
        f"• prj_cycle: {'ALIVE' if prj_alive else 'DEAD'} ({prj_elapsed})\n"
        f"• {phase_summary}\n"
        f"• Inference: {inference_cpu}% CPU\n"
        f"• Memory: {mem} | Swap: {swap}\n"
        f"• Containers: {containers}\n"
        f"• 최근 eval 파일: {recent_str}"
    )

    return msg


def main():
    TEST = test_setup("experiment_monitor", "Background Slack status reporter for long experiments")
    # 단독 실행 시: 무한루프 돌며 30분마다 Slack 전송
    interval = 1800  # 30분
    print(f"[Experiment Monitor] 시작됨. {interval // 60}분 간격 Slack 보고.", flush=True)
    slack_send("*[Experiment Monitor]* 실험 모니터링 시작. 30분 간격 보고.")

    while True:
        time.sleep(interval)
        msg = build_status()
        # 프로세스 죽었으면 마지막 보고 후 종료
        pid = get_runner_pid()
        alive = is_alive(pid) if pid else False
        if not alive and pid:
            slack_send(msg)
            slack_send("*[Experiment Monitor]* Runner 종료 감지 — 모니터 종료합니다.")
            print("Runner dead, monitor exiting.", flush=True)
            test_complete("runner_dead")
            return
        slack_send(msg)
        # 모든 Phase 완료 체크
        phases_completed = sum(
            1 for p in range(5) if os.path.exists(os.path.join(EXPER_DIR, f"phase{p}_metrics.json"))
        )
        if phases_completed >= 5:
            slack_send(msg)
            slack_send("*[Experiment Monitor]* 5개 Phase 모두 완료! 최종 보고서를 확인하세요.")
            print("All 5 phases done, monitor exiting.", flush=True)
            test_complete("all_phases_complete")
            return


if __name__ == "__main__":
    main()
