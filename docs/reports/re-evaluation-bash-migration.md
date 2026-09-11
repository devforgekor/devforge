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

### 1.5 extract.py가 Self-Sufficient Pipeline으로 전환 (⭐ 결정적 변화)

**extract.py가 preflight gate, reranker launch, verify를 모두 내재화**했다. 이는 day_cycle.sh의 책임을 근본적으로 축소시킨 변경이다.

| 기능 | v1.2 설계 당시 | 현재 (2026-07-22) |
|------|---------------|-------------------|
| Model file 존재 확인 | day_cycle.sh 역할 아님 | extract.py `_preflight_gate()` |
| Memory budget 확인 | day_cycle.sh `_budget_gate()` | extract.py `_preflight_gate()` (check_memory_budget) |
| Reranker launch (:8080) | day_cycle.sh `_launch_reranker()` (L140-166) | extract.py `_launch_reranker()` + `_preflight_gate()` |
| Model pod start (day-extractor) | day_cycle.sh → ensure_inference | extract.py `_ensure_model_pod()` |
| NLI verify (extracted → verified) | day_cycle.sh → `day_verify.py` 호출 | extract.py Phase 2-3 (`_llm_nli_verify`, `_llm_nli_verify2`) |
| Pre-flight checks (port kill, stale cleanup) | day_cycle.sh 없음 | extract.py `_preflight_gate()` + `preflight_checks()` |

**변경 diff (464978e..HEAD):**
- `extract_verify.py`: `_llm_nli_verify2` 추가 — 2차 NLI verify
- `extract_llm.py`: `_quality_check_facts` 추가 — fact quality check
- `review_facts`: `nli_llm2`, `quality_checks` 컬럼 추가
- 재시도/복구 순서 변경: `ensure_model` → `preflight_checks` → `ensure_model(skip_if_healthy=True)` 순서로 preflight를 model start 전에 실행
- `check_model_file()`, `check_memory_budget()` watchdog checker 추가

**`_launch_reranker()` 중복 문제**: day_cycle.sh L140-166의 `_launch_reranker()`와 extract.py의 `_launch_reranker()`가 동일한 역할. extract.py `main()`이 실행될 때 reranker가 이미 떠 있으면 skip되므로 실제 중복 실행은 거의 없지만, day_cycle.sh 쪽 호출은 사실상 불필요해졌다.

### 1.6 day_cycle.sh 책임 변화 (핵심 결론)

day_cycle.sh는 더 이상 "두꺼운 오케스트레이터"가 아니다. 실제로 수행하는 일은:

| 책임 | 복잡도 | 이미 Python으로 처리됨? |
|------|--------|----------------------|
| System sync (gen_architecture, duckdns, watchdog, git commit) | 낮음 | 아니오 (그대로 bash) |
| Text clean → FTS5 refresh → Entity scan | 중간 | python3 subprocess 호출 (이미 Python) |
| **Day extract** → `extract.py` 호출 | **~18개 DB query 포함** | **extract.py가 독자적인 preflight/reranker/verify 수행** |
| Noise marker 처리 | 낮음 | DB UPDATE 쿼리 |
| NEUTRAL gate (Slack → stop) | 낮음 | Slack alert |
| Reranker recovery (`reranker_recover.py`) | 낮음 | python3 호출 (이미 Python) |
| Post-extract supplement | 중간 | python3 호출 + DB query |
| **Day enrich** → `enrich.py` 호출 | 낮음 | python3 호출 (이미 Python) |
| **Day embedding** → `embed_batch.py` 호출 | 낮음 | python3 호출 (이미 Python) |

**결론**: day_cycle.sh의 핵심 복잡성(verify, reranker, preflight)은 이미 extract.py로 이전됨. 남은 bash는 주로 "순차적 python3 호출 + 간단한 DB 쿼리 + Slack alert" 패턴이다. **Python 마이그레이션의 긴급도가 크게 낮아졌다.**

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

### 3.2 extract.py Self-Sufficient 전환 → day_cycle.sh 재평가

**가장 중요한 발견**: day_cycle.sh의 핵심 복잡성(verify, reranker launch, preflight, memory budget)이 이미 extract.py로 완전히 이전되었다. 추정 결과:

| 항목 | 이전 (v1.2) | 현재 (v2.0) |
|------|-----------|-----------|
| day_cycle.sh 책임 중 이미 Python화된 부분 | - | extract.py(verify, reranker), enrich.py, embed_batch.py |
| day_cycle.sh 마이그레이션 긴급도 | **높음 (P0)** | **중간 (전환 이익 축소)** |
| `_launch_reranker()` 중복 | 없음 | day_cycle.sh + extract.py 모두 호출. extract.py `main()`에서 이미 처리 |
| `day_verify.py` | day_cycle.sh에서 호출 | **삭제됨** (extract.py Phase 2-3으로 통합) |

**결론**: day_cycle.sh 전환의 ROI가 낮아졌다. 남은 bash 로직의 80%는 "python3 script.py 호출 + 간단한 DB query" 패턴으로, Python 전환 시 얻는 이점이 제한적이다. 대신:

1. **extract.py가 pipeline의 핵심 복잡성을 이미 처리** → day_cycle.sh는 thin wrapper에 가까움
2. **enrich.py도 preflight → ensure_model 순서 변경**으로 self-cleaning 강화
3. **남은 bash 리스크**: 19개 DB query의 raw SQL (injection 위험), python3 -c 2회

### 3.3 전환 대상 (재분류)

```
Phase 0: model_ctl.sh (240L) → lib/model_ctl.py
  - python3 -c inline 2회 (dict mapping, port resolution)
  - 기존 Python lib(db.py, model_registry.py, notify.py) 활용 가능
  - _ensure_model → subprocess.run(podman run ...)
  - 의존성: Phase 0 완료 → Phase 1, 2에서 import

[Phase 1: day_cycle.sh (462L) → day_cycle.py  ← ROI 재평가 필요]
  - extract.py가 verify/reranker/preflight를 이미 처리 → day_cycle 전환 이익 감소
  - watchdog trigger이므로 ExecStart 변경 불필요
  - 남은 bash: 19개 DB query, 14회 python3 subprocess 호출, 3회 curl
  - 전환 시 얻는 것: DB query 통일(psql_json), budget gate Python화
  - 전환 시 잃는 것: watchdog→bash→python3 체인의 bash 레이어 (사실상 없음)
  - 권장: Phase 0 완료 후 재평가. 긴급하지 않음.

Phase 2a: night_cycle.sh (245L) → night_cycle.py
  - mode 전환 / trap / retry / wait_for_model
  - python3 -c 3회, curl 1회
  - timer 기반이므로 ExecStart 변경 필요
  - priority: medium (extract.py처럼 self-cleaning 안 되어 있음)

Phase 2b: auto_mode.sh (271L) → auto_mode.py
  - awk Markdown parser → Python 're' + state machine
  - Claude Code subprocess runner
  - _ensure_memory → /proc/meminfo
  - priority: low (실행 빈도 낮음, crono not triggered)

Phase 3a: run_baseline_monitor.sh (82L) → tests/baseline_monitor.py
  - JSON 파싱 (4회 python3 -c) → Python dict 직접
  - YAML 생성 → yaml.dump
  - priority: medium (python3 -c 밀도 가장 높음)

Phase 3b: weekly_enrich_rebuild.sh (56L) → weekly_enrich_rebuild.py
  - 2회 python3 -c inline → import로 대체
  - 단순 quality check → rebuild sequence
  - priority: low (56L)

Phase 3c: claude_code_runner.sh (126L) → claude_code_runner.py
  - A/B 4-variant proxy env switching
  - sequence loop + CSV summary
  - priority: low (수동 CLI, 빈도 낮음)

Phase 3d: gemini_session_start.sh (99L) → gemini_session.py
  - key rotation → lib KeyRotator import로 대체
  - tmux session → subprocess.run
  - priority: low (수동 CLI)

Phase 3e: run_verify_compare.sh (100L) → tests/verify_compare.py
  - 40라인 python3 -c config patch → 직접 module mutation
  - file polling → time.sleep loop
  - priority: very low (일회성 테스트 도구)

Phase 3f: claude_code_wrapper.sh (85L) → claude_code_wrapper.py
  - JSON canonicalization (python3 -c heredoc)
  - model alias remapping + curl
  - priority: very low (runner가 호출, 간접 영향)
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

### 5.1 가장 중요한 발견

**extract.py가 self-sufficient pipeline이 되면서 bash→Python 마이그레이션의 rationale이 약화되었다.** 원래 계획은 "bash의 복잡한 로직을 Python으로 옮기자"였는데, 이미 가장 복잡한 부분(verify, reranker, preflight, memory budget)은 Python이 처리하고 있다. 남은 bash는 대부분 "python3 script.py 실행 + 간단한 DB query + Slack alert" 패턴이다.

### 5.2 추천 전략

| 우선순위 | 대상 | 사유 |
|---------|------|------|
| **1순위** | `model_ctl.sh` → Python | 유일한 bash library. Phase 1 완료 시 모든 Python 스크립트가 subprocess.run(podman ...) 대신 import 가능. 240L 중복 코드 제거. |
| **2순위** | `run_baseline_monitor.sh` → Python | py3c 4회/82L = 가장 높은 python3-c 밀도. JSON 파싱을 직접 dict로 대체. |
| **3순위** | `night_cycle.sh` → Python | python3 -c 3회, mode 전환 복잡도. ExecStart 변경 필요. |
| **4순위** | `day_cycle.sh` → Python | ROI 낮음. extract.py가 이미 핵심 처리. 보류 후 재평가. |
| **보류** | auto_mode, weekly_enrich, claude_code_runner 등 | 실행 빈도 낮음. 전환 이익 대비 비용 큼. |
| **전환 불필요** | system_sync, open-newhand, entrypoints, run_pipeline_bg | 97L 합계. 전환 이점 미미. |

### 5.3 day_cycle-watchdog 연동 결정

timer가 사라졌으므로 선택지는:
- **A (권장)**: 현행 유지. `systemctl start devforge-day-cycle.service` → `/bin/bash day_cycle.sh` → python3 subprocess. bash 레이어는 462L이지만 핵심 로직은 이미 Python.
- **B**: ExecStart만 `/usr/bin/python3 day_cycle.py`로 변경. watchdog은 systemctl start 그대로. 단순 변경.
- **C**: watchdog이 `day_cycle.main()` 직접 import. 복잡도 증가 대비 이익 없음.

**권장: B**. `day_cycle.sh`의 남은 로직이 얇아졌으므로 Python 전환 시 예상 공수가 4-6h → 2-3h로 단축. 단, 리스크/이익 비율을 고려해 Phase 0(model_ctl → Python) 완료 후 재평가.

### 5.4 총 예상 공수 (재추정)

| Phase | Script | v1.2 공수 | v2.0 공수 | 비고 |
|-------|--------|----------|----------|------|
| Phase 0 | model_ctl.sh | 2-3h | **1-2h** | 기존 lib(db.py, model_registry.py) 활용 |
| Phase 1 | day_cycle.sh | 4-6h | **2-3h** (보류) | extract.py가 핵심 로직 흡수 |
| Phase 2a | night_cycle.sh | 3-4h | 3-4h | 변경 없음 |
| Phase 2b | auto_mode.sh | 3-4h | 2-3h | awk 파서만 전환 |
| Phase 3a | baseline_monitor | 1h | 0.5-1h | py3c 4회 제거 |
| Phase 3b~f | 나머지 5개 | 3.5-5h | 3-5h | 모두 수동/저빈도 |
| **선택 전환** | | ~16.5h | **~12-16h** | Phase 1 보류 시 **~10-13h** |
| **model_ctl only** | | - | **1-2h** | 최소 실행 옵션 |

### 5.5 최종 권장

1. **지금 당장**: `model_ctl.sh` → Python (1-2h). 가장 큰 중복 코드이고, 모든 Python 스크립트에 이득.
2. **다음**: `run_baseline_monitor.sh` (py3c 4회, 0.5-1h). 밀도 대비 전환 비용 최소.
3. **검토 후**: `day_cycle.sh` 전환 여부는 Phase 0 완료 후 extract.py와의 인터페이스가 안정화되면 재평가.
4. **하지 않음**: entrypoints 3개, system_sync, open-newhand, run_pipeline_bg. 전환 불필요.
5. **문서**: 설계 문서(v1.2)를 이 재평가 내용으로 대체하거나, `design-bash-to-python-migration.md` v2.0으로 업데이트.
