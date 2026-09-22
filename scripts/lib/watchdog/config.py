# Status: production
# Path: imported by — lib/watchdog modules
"""Watchdog 설정 — 체크 대상, 간격, 임계값.

MODE=day (관찰형, 60s 주기):
  inference=reranker (:8080)
  inference=day (:8082) extractor (day verify via model swap on :8082)
  day_cycle.sh — watchdog-managed async pipeline (embed → extract → enrich → verify)
  Fix loop: inference (:8080)가 수정 담당
"""


# ── 인터벌 ──────────────────────────────────────────────────────────
CHECK_INTERVAL = 60  # seconds between check cycles
HEARTBEAT_INTERVAL = 1800  # 30min Slack heartbeat (aligned to :15 / :45)
LIVENESS_STALE_SEC = 900  # 15min — watchdog dead man's switch threshold
WATCHDOG_LIVENESS_FILE = "/var/tmp/watchdog_last_cycle_ts"  # watchdog self heartbeat (checked by liveness timer / external)
LATENCY_CHECK_INTERVAL = 300  # 5min between T3 latency checks

# ── LLM probe 관용성 (단일 오탐으로 inference 재시작 금지) ──────────
# day 모델은 CPU 프리필이 길어 60s를 넘기기 쉽고, 단계마다 모델을 로드/전환하므로
# :8082가 일시적으로 503(Loading)/timeout이 되는 것은 정상 상태다.
LLM_PROBE_TIMEOUT_SEC = 120  # probe 타임아웃(기존 60 → 120)
LLM_PROBE_FAIL_THRESHOLD = 3  # 연속 N회 실패에만 recovery 발동
# 모델 로딩/전환 중 정상 상태(장애 아님) — 카운트하지 않고 건너뜀.
LLM_PROBE_TRANSIENT = ("503", "loading model")

# ── MODE ────────────────────────────────────────────────────────────
MODE_FILE = "/opt/ai_data/scripts/current-system-mode.env"
MODE_FILE_INFERENCE = "/opt/ai_data/scripts/current-mode-inference.env"

# ── Watchdog 상태 영속화 ───────────────────────────────────────────
# 재시작 시 backoff 카운터/circuit breaker를 보존 (업계 표준: 상태 비휘발)
STATE_FILE = "/opt/ai_data/scripts/watchdog_state.json"
STATE_SAVE_INTERVAL = 300  # 5분마다 저장

# ── 포트 / 라벨 ─────────────────────────────────────────────────────
# Inference container serves all models across ports 8080-8084
LLM_TARGETS = {
    "day-extract": {"port": 8082, "label": "day-extract", "day_model": "extractor"},
    # Night-only model: verifier on :8084
    "night-verify": {"port": 8084, "label": "night-verify", "day_model": None},
}

# Day mode: check these ports for LLM probes
DAY_PORTS = {8080, 8082}

# ── 서비스 / 타이머 ─────────────────────────────────────────────────
# 핵심 파이프라인 서비스 — 다운 시 자동 재시작
SERVICE_TARGETS = [
    "devforge-turn-watcher",
    "openrouter-rr-proxy",
    "devforge-day-cycle",  # day 파이프라인 (async)
    "ebook-watcher",  # ebook 워처 (타이머와 쌍)
    "container-devforge-fastapi",  # 알림 허브 + Blob Explorer → 다운 시 자동 재시작
    "container-devforge-worker",  # raw_consumer → 다운 시 자동 재시작
    "ebook-api",  # ebook 백엔드 (:8089, Caddy /api)
    "devforge-news-api",  # news API (:8091, Caddy /news)
    "cashbook",  # 가계부 (:8100, Caddy /cashbook)
]

# system 스코프(rootful) 서비스 — alert-only (재시작은 root 필요).
SYSTEM_SERVICE_TARGETS = [
    "caddy",  # 공개 리버스 프록시 (rootful, 80/443)
    "netdata",  # 모니터링 대시보드
]

# Alert-only targets (monitor only, no recovery) — MCP/프록시/인프라
ALERT_ONLY_TARGETS = [
    "container-postgres",  # DB (exclusion, restart 금지)
    "container-devforge-mcp",  # MCP 서버
    "container-flaresolverr",  # Cloudflare 우회
    "anthropic-openrouter-proxy",  # Anthropic→OpenRouter 변환
    "anthropic-proxy",  # DeepSeek 역방향 프록시
    "or-rate-limiter",  # OpenRouter rate limiter
]

# ── svc pod 호스트 포트 포워딩 감시 ─────────────────────────────────
# rootless bridge에서는 published port가 rootlessport(userspace proxy)로
# 포워딩된다. 프로세스가 죽으면 컨테이너는 healthy여도 호스트에서 도달
# 불가가 된다(2026-09-12 사고). SSOT: ~/.config/containers/systemd/svc.pod
# 의 PublishPort (변경 시 동기화).
SVCPOD_UNIT = "svc-pod.service"
SVCPOD_PUBLISHED_PORTS = {
    8000: "devforge-mcp",
    8002: "devforge-fastapi",
    8085: "blob-explorer",
    8191: "flaresolverr",
}

# 타이머 감시 — max_idle 초과 시 미발동으로 간주 (kick/alert)
TIMER_TARGETS = {
    # free 모델 갱신 타이머 — 매일 15:30 UTC. 26h idle = 하루 넘게 안 돌면 알림.
    "devforge-openrouter-free-models.timer": {"expected": "free_models", "max_idle": 93600},
    "devforge-system-sync.timer": {"expected": "system_sync", "max_idle": 2700},  # 45분 (30분 주기+delay 여유)
    "devforge-news.timer": {"expected": "news", "max_idle": 25200},  # 6시간
    "devforge-daily-structure.timer": {"expected": "daily_structure", "max_idle": 90000},  # 25h
    "devforge-weekly-enrich-rebuild.timer": {
        "expected": "weekly_enrich",
        "max_idle": 604800,
    },  # 7일
    "devforge-restore-test.timer": {"expected": "restore_test", "max_idle": 2592000},  # 30일
    "devforge-backup-safety.timer": {"expected": "backup", "max_idle": 97200},  # 27h (OCI 백업)
    "devforge-dev-poll.timer": {"expected": "dev_poll", "max_idle": 1800},  # 10분
    "devforge-news-digest.timer": {"expected": "news_digest", "max_idle": 90000},  # 25h
    "kuhwa-schedule.timer": {"expected": "kuhwa", "max_idle": 90000},  # 25h
    "workspace-autopush.timer": {"expected": "workspace_autopush", "max_idle": 90000},  # 25h
    "reference-monitor.timer": {"expected": "reference", "max_idle": 604800},  # 7일
    # Phase 3 미등록 편입 (guide §5, 주기×1.5 여유)
    "golden-image-deploy-check.timer": {
        "expected": "golden_image_deploy_check",
        "max_idle": 1350,
    },  # 15분
    "workspace-autocommit.timer": {"expected": "workspace_autocommit", "max_idle": 2700},  # 30분
    "devforge-summary-retry.timer": {"expected": "summary_retry", "max_idle": 10800},  # 2시간
    "baseline-daily.timer": {"expected": "baseline_daily", "max_idle": 129600},  # 24h
    "kv-backup.timer": {"expected": "kv_backup", "max_idle": 907200},  # 주 1회×1.5 = 10.5일
    "golden-image-yearly-check.timer": {
        "expected": "golden_image_yearly",
        "max_idle": 34560000,
    },  # 연 1회, guide 지정 400일
}

# One-shot 서비스 결과 감시 (alert-only, 재시작 안 함).
# 타이머가 떠도 서비스가 실패하면 LastTrigger만으로는 감지 못 함 →
# ActiveState/Result로 실패를 잡는다 (daily-structure 실패→git 백로그 사례).
ONESHOT_RESULT_TARGETS = [
    "devforge-daily-structure.service",  # 문서 생성 + git push
    "devforge-backup.service",  # OCI 백업 (DB + app)
    "devforge-restore-test.service",  # 월간 복원 검증
    "devforge-system-sync.service",  # 30분 아키텍처/동기화
    "kv-backup.service",  # 주간 KV 백업 (Phase 3)
    "workspace-autocommit.service",  # workspace 자동 커밋 (Phase 3)
    "golden-image-deploy-check.service",  # 배포 헬스체크 (Phase 3)
]

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

# ── Slack (Azure KV → env var) ───────────────────────────
SLACK_CHANNEL = "U0APJGD8CBW"
ALERT_DEDUP_SEC = 300  # 5min per-component dedup

# ── Heartbeat (Dead Man's Switch) ────────────────────────────────────
HEARTBEAT_STALE_SEC = 1800  # 30min without heartbeat → hang 판정
HEARTBEAT_WORKERS: dict[str, int] = {
    "embed_batch": 1800,  # embed_batch.py batch loop
    "liveness_embed_batch": 1800,  # background liveness thread (embed_batch.py)
    "entity_scan": 1800,  # entity_scan.py — deterministic entity scan
    "text_clean": 1800,  # text_clean.py — unified text preprocessing
    "day_extract": 1800,  # extract.py — LLM extraction pipeline
    "day_enrich": 1800,  # enrich.py — LLM enrichment pipeline
    "news_collector": 25200,  # news collector (6h timer) — completion heartbeat only
}  # worker_name → max_age_seconds. Only register workers that actually call heartbeat().

# Stale completion-heartbeat workers that should be kicked once (with cooldown)
# instead of auto-resolved. One-shot scheduled jobs (news) fit this model:
# the timer firing is not proof the run completed, so on stale heartbeat we
# re-start the service to self-heal, guarded to avoid restart storms.
STALE_HEARTBEAT_KICKS: dict[str, str] = {
    "news_collector": "devforge-news.service",
}
STALE_KICK_COOLDOWN_SEC = 900  # min gap between kicks per worker

# ── Pipeline intermediate state recovery ──────────────────────────
# Stale intermediate states indicate worker crash mid-batch.
# Threshold per state: max single LLM call time + safety margin.
# extracting → scanned, enriching → verified
PIPELINE_INTERMEDIATE_STATES: dict[str, dict] = {
    "extracting": {"to_state": "scanned", "stale_sec": 1800},  # 30 min
    "enriching": {"to_state": "verified", "stale_sec": 1800},  # 30 min
}

# ── Token stagnation detection ────────────────────────────────────
# If /metrics shows processing > 0 but aggregate token counters don't
# advance for STAGNATION_STUCK_CYCLES consecutive cycles → system hang.
# Catches cont-batching deadlocks that slot-level check misses (task_id
# keeps changing but no tokens generated).
TOKEN_STAGNATION_THRESHOLD = 5  # cycles (~5 min @ 60s)

# ── 임시 podman 검증 ───────────────────────────────────────────────
SANDBOX_IMAGE = "python:3.12-alpine"
SANDBOX_TIMEOUT = 30  # seconds
SANDBOX_MEM_LIMIT = "128m"

# ── Deep Dive 7단계 sandbox 검증 (task #24) ─────────────────────────
# fixloop의 compile()-only 검증과 별도로, 실제 test 실행용 상수.
# --network none --read-only로 프로젝트 디렉토리만 읽기 마운트.
# 주의: SANDBOX_IMAGE(python:3.12-alpine)에는 pytest가 없고 --network none이라
# 설치도 불가 — 1차 구현은 stdlib unittest만 지원(예: "python -m unittest discover").
SANDBOX_VERIFY_TIMEOUT = 120  # seconds
SANDBOX_VERIFY_MEM_LIMIT = "256m"
# project_dir이 이 경로 하위가 아니면 sandbox_verify를 거부한다.
# host 임의 경로 읽기전용 마운트로 인한 정보 유출 방지
# 정보 유출을 막기 위한 allowlist.
SANDBOX_VERIFY_ALLOWED_ROOT = "/opt/projects/server"
