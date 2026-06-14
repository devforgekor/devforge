# Status: production
# Path: imported by — lib/watchdog modules
"""Watchdog 설정 — 체크 대상, 간격, 임계값.

MODE=day (관찰형, 60s 주기):
  Pod A=reserved (:8080) operator
  Pod B=day (:8082) 7B Q8 extractor
  :8083 (14B Q6_K) day verify
  매시 :00 → day_cycle.sh (system sync → embed(f16) → extract(7B) → verify(14B))
  Fix loop: Pod A (:8080)가 수정 담당

MODE=night (능동형, 60s 주기):
  Night Debate (:8081 30B P → :8082 7B R → :8083 N14B J, sequential)
  Night Verify (:8084 27B) → review_consumer.py
  Proxy Audit → proxy_reviewer.py (DeepSeek Pro)
  Fix loop: watchdog이 임시 podman 검증 후 feedback 문서 생성
"""

import os
from pathlib import Path

# ── 인터벌 ──────────────────────────────────────────────────────────
CHECK_INTERVAL = 60        # seconds between check cycles
HEARTBEAT_INTERVAL = 1800  # 30min Slack heartbeat (aligned to :15 / :45)
LIVENESS_STALE_SEC = 900   # 15min — watchdog dead man's switch threshold
LATENCY_CHECK_INTERVAL = 300  # 5min between T3 latency checks

# ── MODE ────────────────────────────────────────────────────────────
MODE_FILE = "/opt/ai_data/scripts/current-system-mode.env"
MODE_FILE_A = "/opt/ai_data/scripts/current-mode-pod-a.env"
MODE_FILE_B = "/opt/ai_data/scripts/current-mode-pod-b.env"

# ── 포트 / 라벨 ─────────────────────────────────────────────────────
# Day mode targets: Pod A reserved + Pod B day chain
LLM_TARGETS = {
    "pod-a":     {"port": 8080, "label": "pod-a",     "day_model": "operator"},
    "day-extract": {"port": 8082, "label": "day-extract", "day_model": "extractor"},
    "day-verify": {"port": 8083, "label": "day-verify", "day_model": "judge"},
    # Night-only model: 27B verifier on :8084
    "night-verify": {"port": 8084, "label": "night-verify", "day_model": None},
}

# Day mode: check these ports for LLM probes
DAY_PORTS = {8080, 8082, 8083}

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
    "devforge-day-cycle.timer":    {"expected": "day_cycle",     "max_idle": 3900},     # 1h cycle + 5min buffer
    "devforge-night-cycle.timer":  {"expected": "night_cycle",   "max_idle": 90000},    # 25h
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
