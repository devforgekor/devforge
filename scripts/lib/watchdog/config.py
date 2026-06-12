# Status: production
# Path: imported by — lib/watchdog modules
"""Watchdog 설정 — 체크 대상, 간격, 임계값.

MODE=day (관찰형):
  Pod A=reserved (:8080) operator
  Pod B=7B Q8 (:8082) extractor — daytime extraction
  :00/:30 → day_cycle.py (extract → py verify → 7B verify → global context)
  :15/:45 → classify.py (P-7B extractor → R-14B → J-14B)
  Fix loop: operator(Pod A)가 수정 담당

MODE=night (능동형):
  Phase 4: Night Debate (P:8081 → R:8082 → J:8083, sequential on Pod B)
  Phase 5: 27B verify (:8084)
  Fix loop: watchdog이 임시 podman 검증 후 feedback 문서 생성
"""

import os
from pathlib import Path

# ── 인터벌 ──────────────────────────────────────────────────────────
CHECK_INTERVAL = 60        # seconds between check cycles
HEARTBEAT_INTERVAL = 1800  # 30min Slack heartbeat
LATENCY_CHECK_INTERVAL = 300  # 5min between T3 latency checks

# ── MODE ────────────────────────────────────────────────────────────
MODE_FILE = "/opt/ai_data/scripts/current-system-mode.env"
MODE_FILE_A = "/opt/ai_data/scripts/current-mode-pod-a.env"
MODE_FILE_B = "/opt/ai_data/scripts/current-mode-pod-b.env"

# ── 포트 / 라벨 ─────────────────────────────────────────────────────
LLM_TARGETS = {
    "pod-a":  {"port": 8080, "label": "pod-a",  "day_model": "operator"},
    "pod-b":  {"port": 8082, "label": "pod-b",  "day_model": "extractor"},
    "verify": {"port": 8084, "label": "verify", "day_model": None},
}

DAY_PORTS = {8080, 8082}

# ── 서비스 / 타이머 ─────────────────────────────────────────────────
SERVICE_TARGETS = [
    "container-devforge-pod-a",
    "container-devforge-pod-b",
    "devforge-turn-watcher",
]

# Alert-only targets (monitor only, no recovery)
ALERT_ONLY_TARGETS = [
    "container-postgres",
]

TIMER_TARGETS = {
    "devforge-day-cycle.timer":  {"expected": "extract/classify", "max_idle": 3600+300},  # 1h cycle + 5분 버퍼
    "devforge-classify.timer":  {"expected": "classify",        "max_idle": 2700},
    "devforge-night-cycle.timer":   {"expected": "nightly",         "max_idle": 90000}, # 25h
}

# ── 컨테이너 exclusion (절대 재시작 금지) ───────────────────────────
CONTAINER_EXCLUSION = {"data-pod-infra", "postgres", "container-postgres"}

# ── CrashLoopBackOff 백오프 (K8s 패턴) ──────────────────────────────
BACKOFF_SCHEDULE = [0, 10, 20, 40, 80, 120, 300]  # seconds
MAX_RETRIES = 3
BACKOFF_RESET_SEC = 600  # 10min 정상 → 카운터 리셋
CIRCUIT_BREAKER_TIMEOUT = 120  # 2min OPEN → HALF_OPEN

# ── 리소스 임계값 ───────────────────────────────────────────────────
DISK_WARN_PCT = 85
DISK_CRIT_PCT = 92
SWAP_WARN_MB = 6000
SWAP_CRIT_MB = 9000
MEM_WARN_PCT = 80
MEM_CRIT_PCT = 90

# ── Slack ──────────────────────────────────────────────────────────
SLACK_SECRETS = os.path.expanduser("~/.config/devforge/secrets.env")
SLACK_CHANNEL = "U0APJGD8CBW"
ALERT_DEDUP_SEC = 300  # 5min per-component dedup

# ── 임시 podman 검증 ───────────────────────────────────────────────
SANDBOX_IMAGE = "python:3.12-alpine"
SANDBOX_TIMEOUT = 30  # seconds
SANDBOX_MEM_LIMIT = "128m"
