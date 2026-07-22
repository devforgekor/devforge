# Bash → Python Migration — 재평가 보고서 (v1.2 → v2.0)

> 작성: 2026-07-22 (기준: v1.2 설계 문서, 현재 코드 v1.2 대비 18일 차이)

## 1. 주요 아키텍처 변경 사항

### 1.1 day-cycle.timer 완전 제거 (가장 큰 변화)

| 항목 | v1.2 설계 (2026-07-05) | 현재 (2026-07-22) |
|------|----------------------|-------------------|
| day_cycle 트리거 | devforge-day-cycle.timer (OnCalendar) | **Python watchdog** (`lib/watchdog/__init__.py`) |
| 타이머 파일 | 존재 | ~/.config/systemd/user/ 에 **없음** |
| 서비스 타입 | `Type=oneshot` + timer | `Type=oneshot`, `static` (수동/시스템 트리거) |
| watchdog 역할 | 명시 안됨 | pending turn 감지 → `systemctl start devforge-day-cycle.service` |
| 영향 | Phase 1a에서 timer변경 필요 | **Timer 변경 불필요**. watchdog Python 내에서 직접 호출 가능 |

**시사점**: day_cycle.sh → Python 전환 시 systemd timer/ExecStart 변경이 필요 없음. watchdog이 Python이므로 `day_cycle.main()`을 subprocess 대신 직접 import해서 호출하는 최적화도 가능.

### 1.2 pipeline_state: 7단계 → 6단계

```
v1.0: pending → batching → cleaned → scanned → extracted → verified → enriched → embedded
v1.2: pending → batching → cleaned → scanned → extracted+verified → enriched → embedded
```

변경:
- `extracted → verified` 분리 단계 제거. `day_verify.py` 호출 삭제
- day_cycle.sh 헤더 주석은 여전히 verify 언급 (stale, 코드는 이미 반영됨)
- budget gate 수: 6개 (scanned, extracted+verified, enriched, feedback embed 등)

### 1.3 pipeline_state: 7단계 → 6단계

이미 위에서 설명. 추가 분석:
- NEED_VERIFY 블록 통째로 제거됨 (L378-406 삭제)
- NEED_SUPPLEMENT 쿼리 조건: `pipeline_state = 'extracted'` → `pipeline_state = 'verified'`
- NEED_ENRICH 쿼리 대상: `pipeline_state = 'verified'`
- total pipeline_state 감소: 7 → 6

### 1.4 Reranker launch 함수 추가

`_launch_reranker()` (L140-166): `podman exec devforge-inference`로 reranker 4B Q8 실행.
기존에는 수동 실행이었으나 7월 18일 패치로 자동화됨:
- podman exec + taskset
- /health polling (최대 300s)

---

## 2. Bash 스크립트 현황: 복잡도 순위 (재평가)

### 2.1 복잡도 매트릭스

```
순위  점수   라인   DB  py3c curl 함수  스크립트
─────────────────────────────────────────────────────
 1    52   462L   19   2   3    5   day_cycle.sh         ← P0, 가장 복잡
 2    16   245L    0   3   1    6   night_cycle.sh       ← P0, python3-c 3회
 3    14    82L    1   4   0    0   run_baseline_monitor.sh ← P1, py3c 4회로
                                                    가장 밀도 높음
 4    10   100L    2   2   0    0   run_verify_compare.sh   ← P1, inline python 40L
 5     7   126L    0   1   0    4   claude_code_runner.sh   ← P1, A/B 로직
 6     6    56L    0   2   0    0   weekly_enrich_rebuild.sh ← P2
 7     5    99L    0   1   0    2   gemini_session_start.sh  ← P1, key rotation
 8     4   271L    0   0   0    4   auto_mode.sh         ← P0지만 점수 낮음
                                    (awk 파서가 주된 복잡성)
 9     3    35L    0   1   0    0   open-newhand.sh      ← 단순 git ops
10     1    41L    0   0   1    0   system_sync.sh        ← 단순 순차
11     1    85L    0   0   1    0   claude_code_wrapper.sh ← proxy wrapper
12     0    21L    0   0   0    0   run_pipeline_bg.sh    ← 사실상 dead code
13~16  0   2~15L   0   0   0    0   entrypoint/*.sh       ← PID 1, 변경 불가
```

**핵심 지표**: `python3 -c inline` 총 16회 (8개 스크립트). DB query 22회 (3개 스크립트).

### 2.2 `python3 -c inline` 분포

| 스크립트 | 횟수 | 용도 |
|---------|------|------|
| run_baseline_monitor.sh | 4 | JSON 파싱 (extract.py 출력 처리) |
| night_cycle.sh | 3 | test heartbeat, queue count, status YAML |
| day_cycle.sh | 2 | Slack alert, day_phase_model |
| run_verify_compare.sh | 2 | config patch, model registry patch |
| weekly_enrich_rebuild.sh | 2 | quality_check import, enrich rebuild |
| claude_code_runner.sh | 1 | jitter 계산 |
| gemini_session_start.sh | 1 | key rotation (KeyRotator.load_state) |
| open-newhand.sh | 1 | commit message generation |

### 2.3 시스템 서비스 ExecStart 현황

| 서비스 | 현재 ExecStart | `python3 -c` | DB | 변경 대상? |
|--------|---------------|-------------|-----|----------|
| devforge-day-cycle | `/bin/bash day_cycle.sh` | 2 | 19 | **선택적** (watchdog→Python 최적화 가능) |
| devforge-night-cycle | `night_cycle.sh` | 3 | 0 | **예** |
| devforge-system-sync | `/bin/bash system_sync.sh` | 0 | 0 | **아니오** (41L, 단순) |
| devforge-weekly-enrich-rebuild | `/bin/bash weekly_enrich_rebuild.sh` | 2 | 0 | **예** |
| devforge-watchdog | `/usr/bin/python3 watchdog.py` | - | - | **이미 Python** |
| devforge-tg-webhook | `/usr/bin/python3.11 tg_webhook.py` | - | - | **이미 Python** |

---

## 3. 범위 재조정 제안

### 3.1 제외 대상 (Bash 유지)

| 스크립트 | 라인 | 사유 |
|---------|------|------|
| `system_sync.sh` | 41 | 단순 순차 실행. python3 호출 2회. 전환 이점 없음 |
| `open-newhand.sh` | 35 | git add/commit/push + python3 message gen. 일회성 수동 CLI |
| `run_pipeline_bg.sh` | 21 | _archive 경로 참조. dead code 가능성 |
| `github_mcp_wrapper.sh` | 15 | `exec` + secrets sourcing. shell 필수 |
| `fastapi-entrypoint.sh` | 2 | PID 1 (`exec`). 변경 불가 |
| `worker-entrypoint.sh` | 8 | PID 1 (`exec`). 변경 불가 |
| `mcp_entrypoint.sh` | 3 | PID 1 (`exec`). 변경 불가 |

### 3.2 전환 대상 (재분류)

```
Phase 0: model_ctl.sh (240L) → lib/model_ctl.py
  - python3 -c inline 2회 (dict mapping, port resolution)
  - 기존 Python lib(db.py, model_registry.py, notify.py) 활용 가능
  - _ensure_model → subprocess.run(podman run ...)
  - 의존성: Phase 0 완료 → Phase 1, 2에서 import

Phase 1: day_cycle.sh (462L) → day_cycle.py
  - watchdog trigger이므로 ExecStart 변경 불필요. subprocess.run으로 호출
  - OR: watchdog이 직접 day_cycle.main() import (추가 최적화)
  - pipeline_state 6-stage FSM in Python
  - 19개 DB query → psql_json() 일괄 변환
  - _launch_reranker → subprocess.run(podman exec ...) + /health polling
  - _budget_gate → Python 함수
  - _slack_alert → lib/notify.py 기존 함수 사용 가능

Phase 2a: night_cycle.sh (245L) → night_cycle.py
  - mode 전환 / trap / retry / wait_for_model
  - _set_mode → Python dict (env file 대체)
  - timer 기반이므로 ExecStart 변경 필요

Phase 2b: auto_mode.sh (271L) → auto_mode.py
  - awk Markdown parser → Python 're' + state machine
  - Claude Code subprocess runner
  - _ensure_memory → psutil (설치 필요) 또는 /proc/meminfo

Phase 3a: claude_code_runner.sh (126L) → claude_code_runner.py
  - A/B 4-variant proxy env switching
  - sequence loop + CSV summary
  - systemctl set-environment → subprocess

Phase 3b: claude_code_wrapper.sh (85L) → claude_code_wrapper.py
  - JSON canonicalization (python3 -c heredoc)
  - model alias remapping + curl

Phase 3c: gemini_session_start.sh (99L) → gemini_session.py
  - key rotation → lib KeyRotator import로 대체
  - tmux session → subprocess.run

Phase 3d: run_baseline_monitor.sh (82L) → tests/baseline_monitor.py
  - JSON 파싱 (4회 python3 -c) → Python dict 직접
  - YAML 생성 → yaml.dump

Phase 3e: run_verify_compare.sh (100L) → tests/verify_compare.py
  - 40라인 python3 -c config patch → 직접 module mutation
  - file polling → time.sleep loop

Phase 3f: weekly_enrich_rebuild.sh (56L) → weekly_enrich_rebuild.py
  - 2회 python3 -c inline → import로 대체
  - 단순 quality check → rebuild sequence
```

### 3.3 전환 순서 결정 요인 재평가

| 요인 | 가중치 | 설명 |
|------|--------|------|
| `python3 -c inline` 밀도 | 상 | inline 제거가 1순위 목표 |
| DB query 수 | 중 | psql_json() 마이그레이션 |
| 실행 빈도 | 상 | timer/trigger 빈도 높은 스크립트 우선 |
| 복잡도/리스크 | 상 | 고복잡도 = 늦게 전환 (Phase 분산) |
| 라이브러리 의존성 | 상 | model_ctl 선행 필수 |

### 3.4 최종 추천 전환 순서

```
Phase 0: model_ctl.sh (240L)          ← 모든 Phase의 선행조건
  ↓
Phase 1: day_cycle.sh (462L)          ← 가장 큰 병목, 19 DB queries
  ↓                                   watchdog→Python 직접호출로 최적화 가능
Phase 2a: night_cycle.sh (245L)       ← 3회 python3 -c, timer 기반
Phase 2b: auto_mode.sh (271L)         ← awk→re 파서 전환 필요
  ↓
Phase 3a: run_baseline_monitor.sh (82L) ← py3c 4회로 밀도 가장 높음
Phase 3b: weekly_enrich_rebuild.sh (56L) ← timer 기반, 간단
Phase 3c: claude_code_runner.sh (126L)  ← 수동 CLI
Phase 3d: gemini_session_start.sh (99L) ← 수동 CLI
Phase 3e: run_verify_compare.sh (100L)  ← 테스트 도구
Phase 3f: claude_code_wrapper.sh (85L)  ← runner.sh에서 호출, 낮은 우선순위
```

---

## 4. 문서(v1.2) 대비 수정 필요 사항

### 4.1 Executive Summary 및 1.1 표

- 스크립트 수: 18개 → **13개** (+ 3 entrypoints 유지)
- 라인 수: ~2,500L → **~1,890L** (entrypoints 제외 시 **~1,640L**)
- day_cycle.sh 설명: `pipeline_state 7단계 FSM` → **6단계** + reranker launch
- run_pipeline_bg.sh: Phase 3 대상에서 **제외** (dead code)

### 4.2 Phase Plan 전면 재작성

| 구분 | v1.2 | v2.0 변경 |
|------|------|-----------|
| Phase 구분 | Phase 0, 1a, 1b, 2a, 2b, 2c, 3 | Phase 0, 1, 2a, 2b, 3a~3f |
| Phase 1a (day_cycle) | systemd ExecStart 변경 + timer | **timer 없음**. watchdog trigger |
| Phase 2c (verify_compare) | Phase 2 | Phase 3e (우선순위 하향) |
| Phase 3 (system_sync 등 7개) | 전환 대상 | **4개 제외** (system_sync, open-newhand 등) |
| Phase 1b (night_cycle) | 2번째 | Phase 2a (model_ctl 선행 필요) |
| Phase 2b (baseline_monitor) | Phase 2 | Phase 3a (테스트 도구, 우선순위 하향) |

### 4.3 Module Architecture (Section 3)

- day_cycle.py: watchdog이 직접 import 불필요. subprocess로 호출 유지.
- lib/model_ctl.py: 기존 lib/db.py, lib/model_registry.py, lib/notify.py 활용
- _launch_reranker → day_cycle.py의 `def launch_reranker()`

### 4.4 Systemd 변경 요약 (Section 5)

- devforge-day-cycle.service: ExecStart는 bash 유지 or watchdog 직접 호출 방식으로 설계 변경
- devforge-night-cycle.service: `/bin/bash night_cycle.sh` → `/usr/bin/python3 night_cycle.py`
- devforge-system-sync.service: **unchanged** (전환 제외)
- devforge-weekly-enrich-rebuild.service: `/bin/bash weekly_enrich_rebuild.sh` → `/usr/bin/python3 weekly_enrich_rebuild.py`
- devforge-auto.service: 타이머 없음 (service 파일 없음?) — 확인 필요

### 4.5 Testing Strategy (Section 7)

- watchdog 기반 day_cycle 테스트: `systemctl --user stop devforge-day-cycle.service` 대신 watchdog 동기화 필요
- parallel run verification: 여전히 유효

### 4.6 Migration Timeline (Section 8)

| Phase | Script | Lines | Est. Effort (v1.2) | Est. Effort (v2.0) |
|-------|--------|-------|-------------------|-------------------|
| Phase 0 | `model_ctl.sh` | 240 | 2-3h | **1-2h** (기존 lib 활용) |
| Phase 1 | `day_cycle.sh` | 462 | 4-6h | **3-5h** (watchdog 연동 고려) |
| Phase 2a | `night_cycle.sh` | 245 | 3-4h | 3-4h |
| Phase 2b | `auto_mode.sh` | 271 | 3-4h | **2-3h** (awk 파서만 집중) |
| Phase 3a | `run_baseline_monitor.sh` | 82 | 1h | 0.5-1h |
| Phase 3b | `weekly_enrich_rebuild.sh` | 56 | 0.5h | 0.5h |
| Phase 3c | `claude_code_runner.sh` | 126 | 2h | 1.5-2h |
| Phase 3d | `gemini_session_start.sh` | 99 | 1-2h | 1h |
| Phase 3e | `run_verify_compare.sh` | 100 | 1-2h | 1-2h |
| Phase 3f | `claude_code_wrapper.sh` | 85 | 1h | 0.5-1h |
| **Total** | | **~1,766** | **~20h** | **~16.5h** |

---

## 5. 권장사항

1. **model_ctl → Python: 우선순위 유지**. 기존 lib/db.py, lib/model_registry.py, lib/notify.py 활용 시 추정 시간 2-3h → 1-2h로 단축
2. **day_cycle → Python: watchdog 연동 설계 명확화**. bash 유지 후 subprocess.run()으로 호출할지, watchdog이 직접 import할지 결정 필요
3. **system_sync.sh, open-newhand.sh, run_pipeline_bg.sh: 전환 제외**. 3개 스크립트 합계 97L, 전환 이점 미미
4. **run_baseline_monitor.sh: 우선순위 상향**. py3c 4회/82L로 밀도 가장 높음 (Phase 3a)
5. **run_pipeline_bg.sh: dead code 확인 후 삭제 고려**. _archive 경로 참조
