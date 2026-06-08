# Status: production
# Path: imported by — lib/watchdog modules
"""Watchdog 설정 — 체크 대상, 간격, 임계값.

MODE=day (관찰형):
  Pod A=7B Q8 (:8082) reviewer — verify/MCP/proposer/judge 전담
  Pod B=3B Q8 (:8080) extractor — daytime extraction
  :00/:30 → day_cycle.py (extract → py verify → 7B verify → global context)
  :15/:45 → classify.py (P-7B → R-3B → J-7B)
  Fix loop: 7B reviewer(Pod A)가 수정 담당

MODE=night (능동형):
  Phase 4: P-R-J (30B→14B→NextCoder, sequential on Pod B :8080)
  Phase 5: 27B verify (:8081)
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
    "pod-a":  {"port": 8082, "label": "pod-a",  "day_model": "reviewer"},
    "pod-b":  {"port": 8080, "label": "pod-b",  "day_model": "extractor"},
    "verify": {"port": 8081, "label": "verify", "day_model": None},
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
    "devforge-15m-cycle.timer": {"expected": "extract/classify", "max_idle": 2700},  # 45m (30m 주기 + 완충)
    "devforge-classify.timer":  {"expected": "classify",        "max_idle": 2700},
    "devforge-nightly.timer":   {"expected": "nightly",         "max_idle": 90000}, # 25h
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
