# Watchdog 전면 감사 (Audit) — 감시/복구 갭 분석

> 작성일: 2026-09-08
> 대상: devforge-watchdog 전체 로직

---

## 1. 현재 와치독 감시 범위

### 1.1 서비스 감시 (SERVICE_TARGETS)

| 대상 | 감시 | 복구 |
|------|------|------|
| `devforge-turn-watcher` | ✅ alive 체크 | ✅ auto-restart |
| `openrouter-rr-proxy` | ✅ alive 체크 | ✅ auto-restart |
| 그 외 **16개 서비스** | ❌ | ❌ |

### 1.2 컨테이너 감시 (ALERT_ONLY_TARGETS)

| 대상 | 감시 | 복구 |
|------|------|------|
| `container-postgres` | ✅ alive 체크 (alert only) | ❌ (exclusion) |
| 그 외 **6개 컨테이너** | ❌ | ❌ |

### 1.3 타이머 감시 (TIMER_TARGETS)

| 대상 | 감시 | 복구 |
|------|------|------|
| `ebook-watcher.service` | ✅ `check_ebook_pipeline()` 전용 체크 (프로세스+로그 활동) | ✅ 재시작 |
| `devforge-openrouter-free-models.timer` | ✅ idle 체크 | ✅ kick |

> **ebook-watcher 개선 (2026-09-09)**: 기존 15분 `ebook-watcher.timer`는 제거.
> watchdog이 60초마다 `check_ebook_pipeline()`으로 프로세스 존재 + 마지막 로그 활동(20분)을
> 확인해 hang/죽음을 감지하고 재시작한다. (systemd WatchdogSec과 이중 감시)
| 그 외 **11개 타이머** | ❌ | ❌ |

### 1.4 LLM 감시 (LLM_TARGETS + 포트 체크)

| 대상 | 감시 | 복구 |
|------|------|------|
| day-extract (:8082) | ✅ T1 + T2 + T3 | ✅ day_fix_loop (Phase 1c) |
| night-verify (:8084) | ✅ T1 + T2 + T3 (선택적) | ✅ (night 제거됨) |
| reranker (:8080) | ❌ (DAY_PORTS에만 있음) | ✅ port conflict 감지 |
| embed (:8081) | ❌ | ❌ |

### 1.5 파이프라인 감시

| 대상 | 감시 | 복구 |
|------|------|------|
| day_cycle.sh 프로세스 | ✅ pgrep | ✅ systemctl start |
| pipeline_state 3600s 정체 | ✅ 이벤트 기록 | ✅ day_cycle 재시작 (Phase 2) |
| intermediate states (30min) | ✅ SQL stale 체크 | ✅ state 리셋 |
| heartbeat (workers) | ✅ 30min staleness | ✅ pulse resolve |
| slot deadlock | ✅ /slots 분석 | ✅ 컨테이너 재시작 |
| token stagnation | ✅ /metrics 분석 | ❌ (이벤트만) |

---

## 2. 갭 분석 — 감시·복구가 필요한 대상

### P0 — 필수 (시스템 안정성 직결)

| # | 대상 | 유형 | 사유 | 권장 |
|---|------|------|------|------|
| 1 | `devforge-inference` | **컨테이너** | 모든 LLM 작업의 중심. 죽으면 enrich/extract/embed 전부 중단 | ✅ check_inference_container() (구현 완료) |
| 2 | `container-postgres` | **컨테이너** | ALERT_ONLY → 자동 복구로 격상. DB 다운 시 모든 파이프라인 중단 | SERVICE_TARGETS로 이동 |
| 3 | `devforge-day-cycle.service` | **서비스** | 현재 파이프라인 프로세스만 감시, 서비스 자체는 미감시 | SERVICE_TARGETS에 추가 |
| 4 | `ebook-watcher.service` | **서비스** | 타이머는 감시하지만 서비스는 미감시. 타이머가 죽었을 때 서비스도 확인 필요 | SERVICE_TARGETS에 추가 |

### P1 — 중요 (간접적 영향)

| # | 대상 | 유형 | 사유 | 권장 |
|---|------|------|------|------|
| 5 | `container-devforge-mcp` | **컨테이너** | MCP 서버 다운 시 Claude Code/Session 도구 사용 불가 | ALERT_ONLY 추가 |
| 6 | `devforge-mcp` | **서비스** | MCP HTTP 서버 (:8000) | ALERT_ONLY 추가 |
| 7 | `devforge-worker` | **컨테이너** | 데이터 처리 worker | ALERT_ONLY 추가 |
| 8 | `devforge-system-sync.timer` | **타이머** | 시스템 상태 동기화 (15분 주기) | TIMER_TARGETS 추가 |
| 9 | `devforge-news.timer` | **타이머** | 뉴스 수집 (6시간) | TIMER_TARGETS 추가 |
| 10 | 장기 미실행 타이머들 | **타이머** | restore-test, refresh-reminder 등 | TIMER_TARGETS에 max_idle=90d로 추가 |

### P2 — 선택적 (모니터링만)

| # | 대상 | 유형 | 사유 | 권장 |
|---|------|------|------|------|
| 11 | `anthropic-*proxy`, `gemini-proxy`, `or-rate-limiter` | **서비스** | LLM API 프록시들. 죽으면 해당 경로 불가 | ALERT_ONLY 추가 |
| 12 | `container-flaresolverr` | **컨테이너** | Cloudflare 우회. ebook 크롤링에 필요 | ALERT_ONLY 추가 |
| 13 | `devforge-daily-structure.timer` | **타이머** | DB 통계 정리 (매일 00:00) | TIMER_TARGETS 추가 |
| 14 | `devforge-summary-retry.timer` | **타이머** | 요약 재시도 (2시간) | TIMER_TARGETS 추가 |

---

## 3. 복구 로직의 설계적 문제

### 3.1 복구의 단계성 부족

현재 복구는 모두 **1단계**로만 되어 있음:

```
recover_service(name) → systemctl restart       # 한 번만 시도
recover_port_conflict() → kill + rm + start      # 한 번만 시도
```

필요한 복구 패턴:
```
1차: 서비스 재시작
2차: 5초 후 재시도
3차: 컨테이너 재시작
4차: 전체 kill_all + 재시작
```

### 3.2 복구 후 Health Check 부족

```python
# recovery.py — 현재
recover_service(name):
    systemctl restart name
    return True  # ❌ health 확인 없음
```

필요:
```python
recover_service(name):
    systemctl restart name
    time.sleep(5)
    return check_service(name)[0]  # ✅ 실제 health 확인
```

### 3.3 중복 복구 시도 (Circuit Breaker 부재)

`recover_service()`가 시스템드 서비스에 대해 `graduated_recover()`를 통해 호출되지만, **복구 후에도 서비스가 바로 죽으면** 60초마다 무한 재시도:

```
60s: 감지 → restart → 5초 후 살아있음
120s: 다시 죽음 → 감지 → restart → ... (무한)
```

→ `ComponentTracker`의 circuit breaker가 있지만, 일부 복구 경로에서 우회됨.

### 3.4 token stagnation이 감지만 하고 복구 안 함

`_check_token_stagnation()`은 `add_event()`만 하고 아무 조치 없음. slot deadlock과 동일한 증상(추론 중단)이므로 동일한 복구(컨테이너 재시작) 필요.

---

## 4. 변경 제안 요약

### 4.1 config.py — 감시 대상 확장

```python
SERVICE_TARGETS = [
    "devforge-turn-watcher",
    "openrouter-rr-proxy",
    "devforge-day-cycle",       # ← 추가
    "ebook-watcher",            # ← 전용 체크(check_ebook_pipeline) 사용
]

ALERT_ONLY_TARGETS = [
    "container-postgres",
    "container-devforge-mcp",   # ← 추가
    "container-flaresolverr",   # ← 추가
    "anthropic-openrouter-proxy",  # ← 추가
    "gemini-openai-proxy",      # ← 추가
]

TIMER_TARGETS = {
    # ebook-watcher.timer 제거됨 (2026-09-09) — watchdog이 check_ebook_pipeline으로 직접 감시
    "devforge-openrouter-free-models.timer": {"max_idle": 93600},
    "devforge-system-sync.timer": {"max_idle": 1800},      # ← 추가 (15분)
    "devforge-news.timer": {"max_idle": 25200},              # ← 추가 (6시간)
    "devforge-daily-structure.timer": {"max_idle": 90000},   # ← 추가 (25h)
    "devforge-weekly-enrich-rebuild.timer": {"max_idle": 604800},  # ← 추가 (7일)
    "devforge-restore-test.timer": {"max_idle": 2592000},   # ← 추가 (30일)
    "reference-monitor.timer": {"max_idle": 604800},        # ← 추가 (7일)
}
```

### 4.2 checker.py — 이미 완료

- `check_port_conflict()` ✅ 구현 완료
- `check_inference_container()` ✅ 구현 완료

### 4.3 recovery.py — 복구 후 Health Check 추가

```python
def recover_service(name: str) -> bool:
    """systemctl restart + health check."""
    ...
    systemctl restart name
    time.sleep(5)
    # Health check
    ok, detail = check_service(name)
    return ok

def recover_container(name: str) -> bool:
    """Container restart + health check."""
    ...
    _podman_start_inference()
    time.sleep(5)
    # Container health check
    ok, detail = check_inference_container()
    return ok
```

### 4.4 orchestrator.py — token stagnation도 복구

```python
def _check_token_stagnation(results, dry_run=False):
    ...
    stagnated = _state.check_token_stagnation()
    for s in stagnated:
        add_event(...)
        # ← 추가: slot deadlock과 동일한 복구
        if not dry_run:
            recover_inference_cascade()
```

---

## 5. 최종 권장 우선순위

| 순위 | 변경 | 파일 | 예상 시간 |
|------|------|------|----------|
| 🥇 | SERVICE_TARGETS에 day-cycle, ebook-watcher 추가 | config.py | 1분 |
| 🥇 | ALERT_ONLY_TARGETS에 mcp, flaresolverr, proxies 추가 | config.py | 1분 |
| 🥇 | TIMER_TARGETS에 주요 타이머 6개 추가 | config.py | 2분 |
| 🥈 | `recover_service()`에 health check 추가 | recovery.py | 5분 |
| 🥈 | `recover_container()`에 health check 추가 | recovery.py | 5분 |
| 🥉 | token stagnation 복구 추가 | orchestrator.py | 3분 |
| 🥉 | container-postgres SERVICE_TARGETS로 격상 | config.py | 1분 |

---

## 6. 2026-09-09 패치 기록 (완료)

### 6.0 ebook-watcher 전용 감시 도입 (이전 패치)
- **문제**: `check_service(name)` = `svc_active`(systemd active)만 확인 → loop이
  멈춰도(hang) 감지 못함. 15분 `ebook-watcher.timer`가 유일한 health check였음
- **수정**: `check_ebook_pipeline()` 추가 (`checker.py`)
  - ① systemd 서비스 active 여부
  - ② `pipeline.py loop` 프로세스 존재 (pgrep)
  - ③ journal 마지막 Cycle/collect 로그 시간 → 20분 초과 시 hang 판정
  - `check_all_services()`에서 ebook-watcher만 이 전용 체크 사용
- **`ebook-watcher.timer` 제거**: config.py TIMER_TARGETS에서 삭제 + systemd timer 비활성화
  → watchdog이 메인으로 ebook 파이프라인을 감시/관리
- **커밋**: `39913d2`

### 6.1 상태 영속화 (backoff/circuit 보존)
- **문제**: `WatchdogState`가 메모리만 → watchdog 재시작 시 backoff 카운터/circuit breaker 초기화
  → 재시작 직후 지속 실패 컴포넌트를 백오프 없이 매 60초마다 재시도 (restart storm)
- **수정**: `ComponentTracker.to_dict()/from_dict()`, `WatchdogState.save_state()/load_state()` 추가
  - `STATE_FILE=/opt/ai_data/scripts/watchdog_state.json`, 5분 주기 저장
  - watchdog 시작 시 `load_state()`로 복원
- **검증**: 실패 상태(UNHEALTHY, consec=4, circuit_open) 저장 → 복원 후 동일 유지

### 6.2 ebook-watcher 복구 후 readiness 확인
- **문제**: `recover_service`가 `svc_active`(systemd active)만 확인 → `Type=notify`+`WatchdogSec`
  서비스는 `READY=1` 수신 전 `activating` 상태라 healthy로 오판 가능
- **수정**: `recover_ebook_watcher()` 추가 — restart 후 `check_ebook_pipeline()`
  (프로세스 존재 + 로그 활동 20분)으로 **진짜 준비** 확인 (최대 6회×5초 대기)
- `_run_services`에서 ebook-watcher만 이 전용 복구 사용

### 6.3 백오프 jitter
- **문제**: `BACKOFF_SCHEDULE` 고정값 → 여러 컴포넌트 동시 실패 시 동시 재시작 (retry storm)
- **수정**: `backoff_sec()`에 ±10% jitter 적용 (`base * uniform(0.9, 1.1)`)
- **검증**: 실패 6회 → 270~326s 분포 (base 300 ±10%)

### 6.4 커밋 요약

| 커밋 | 내용 | 파일 |
|------|------|------|
| `39913d2` | ebook-watcher 전용 체크(`check_ebook_pipeline`) + 15분 timer 제거 | checker.py, config.py |
| `f875134` | 상태 영속화 + ebook readiness 복구 + backoff jitter | state.py, recovery.py, orchestrator.py, config.py |
| `23fe63f` | 문서 기록 (watchdog audit) | watchdog-comprehensive-audit.md |
| `044974b` | 문서 기록 (timer 제거 반영) | watchdog-comprehensive-audit.md |

### 6.5 관련 ebooklib 패치 (병행)

ebook 파이프라인 쪽도 함께 hardening 되었다 (`/opt/workspace/minihome/apps/ebooklib`):

| 커밋 | 내용 |
|------|------|
| `6cdda03` | systemd WatchdogSec + queue 락 + DLQ + 적응형 딜레이 |
| `5cdd0c4` | collect 락 / queue 락 분리 (flock 무력화 버그 수정) |
| `d44a7e6` | 문서 업데이트 |

> ebook-watcher.service: `Type=notify`, `WatchdogSec=600`, `Restart=on-watchdog`
> → watchdog의 `check_ebook_pipeline`(로그 기반)과 **이중 감시** 구조