# Fitness Functions + heartbeat + drift — 구현 가이드 (표준 기반)

> Status: proposed · Date: 2026-09-24 · Owner: devforge
> Related: `reports/systemic-wiring-gap-analysis-20260924.md`(문제), `plans/detection-remediation-architecture.md`, `plans/watchdog-standard-compliance.md`, `reports/logic-recording-tracking-audit-20260923.md`
> Deep Dive: `dp-20260924-fitness-heartbeat-drift-guide` (Yggdrasil)
> **틀**: Fitness Functions(프레임 1개) 안에 **dead-man's switch**·**drift detection**(구체 체크 2개). context7 검증 포함.

---

## 0. 비목표 (중요)

- **구조 변경 없음** — 이 서버는 이미 표준(src-layout+Hexagonal+Quadlet). 적용은 **검증 계층 추가(additive)**.
- **도구 0 추가** — Argo CD/Flux(K8s GitOps) **비채택**(단일 호스트에 과설계). **개념만** 차용.
- 표준을 시스템에 **right-size**해 적용(시스템을 표준에 맞추지 않음).

---

## 1. 표준 근거 + context7 검증

| 표준 | 정의 | context7 검증 |
|---|---|---|
| **Fitness Functions** | "architectural characteristic에 대한 **객관적 무결성 평가**" — 실행 가능 검사로 drift 차단 (Building Evolutionary Architectures) | `import-linter` 계약(Forbidden/Layers/Independence…) = 아키텍처 fitness; `pytest` fixtures/parametrize |
| **Dead-man's switch** | 주기 작업이 **안 돌면** 경보(heartbeat+grace) | `systemd` `OnSuccess=`/`OnFailure=`/`SuccessAction=`/`FailureAction=`, `Type=oneshot`+`RemainAfterExit=` |
| **Drift detection** | desired vs live **지속 대조** → OutOfSync | (개념) Argo CD/Flux; 우리는 경량 대조로 구현 |

> 검증: `scripts/deploy/kv-fetch-env.py python3 scripts/cli.py research docs "<lib>" "<q>" --keys CONTEXT7-*`.

---

## 2. Fitness Functions (프레임)

### 2.1 카테고리 매핑
| 축 | 우리 선택 |
|---|---|
| atomic / holistic | **atomic**(개별 계약·배선) 위주 |
| triggered / continuous | **triggered**(CI/commit) + heartbeat는 **continuous** |
| automated / manual | **automated** |
| threshold / trend | **threshold**(존재/불일치), 신선도는 trend(grace) |

### 2.2 우리 fitness 목록 (5 근본원인 ↔ 검사)
| # | 근본원인 | Fitness function | 위치 |
|---|---|---|---|
| 1 | 배선 미검증 | 모든 `.timer`/`.service`의 `ExecStart`·`Unit`·훅이 **실재 스크립트**를 참조 | `tests/fitness/test_wiring.py` |
| 2 | 이식 갭 | legacy 기능(예: incident→issue)이 v2에 **존재** | `tests/fitness/test_port_parity.py` |
| 3 | 완결성 | 마이그레이션 applied(`alembic current==head`), 활성 기능 전제 충족 | `tests/fitness/test_completion.py` |
| 4 | 계약 분열 | `context_jsonb` SSOT — text-only 소비자 0, 라우팅 표가 모든 prefix 커버 | `tests/fitness/test_contract.py` |
| 5 | 문서=실제 | 문서가 주장한 경로/배선 실재(기존 `test_docs_index`/`test_code_structure` 확장) | `tests/unit/test_docs_index.py`(확장) |

### 2.3 구현 스케치 (pytest)
```python
# tests/fitness/test_wiring.py
"""Fitness: every timer/service ExecStart and hook references an existing file."""
import re
from pathlib import Path
ROOT = Path("/opt/projects/server")

def _exec_paths(unit_text: str) -> list[str]:
    return re.findall(r"^ExecStart=[^/]*?(-?)(/[^ \n]+)", unit_text, re.M)  # 대략

def test_timer_services_exist():
    for t in (ROOT / "systemd/user").glob("*.timer"):
        svc = t.with_suffix(".service")
        assert svc.exists(), f"{t.name} references missing {svc.name}"

def test_unit_exec_files_exist():
    for u in list((ROOT/"systemd/user").glob("*.service")) + list((ROOT/"containers/systemd").glob("*.container")):
        for _, path in _exec_paths(u.read_text()):
            if path.startswith("/opt/projects/server"):
                assert Path(path).exists(), f"{u.name}: missing {path}"
```
- **배선 실증**: 위 검사가 **`handover-gen.timer` 부재**(§3에서 신설) 같은 silent no-op을 **CI에서 차단**.
- **문서정합**: `test_docs_index`에 "문서가 언급한 unit/timer 실재" 검사 추가.
- **CI**: 기존 test 잡이 `tests/` 전체를 돌리므로 **자동 포함**(별도 배선 불필요).

---

## 3. Dead-man's switch (체크 1)

### 3.1 원리
작업 성공 시 **heartbeat 기록** → watcher가 **interval+grace** 내 미수신 시 경보(Healthchecks/Dead Man's Snitch).

### 3.2 구현 (경량, 도구 0)
1. **공통 ping**: `scripts/deploy/heartbeat-ping.sh <job>` → `/opt/ai_data/scripts/heartbeats/<job>.ts`에 `time.time()` 원자적 write.
2. **유닛 배선**(systemd 표준):
   ```ini
   # oneshot 서비스에 성공 시 ping
   ExecStartPost=/opt/projects/server/scripts/deploy/heartbeat-ping.sh handover-gen
   # (대안) OnSuccess=heartbeat@handover-gen.service  (context7 검증: OnSuccess/SuccessAction)
   ```
3. **watcher**(watchdog 재사용): 신규 `check_heartbeats` — registry의 `{job: (interval, grace)}`에 대해 `now - mtime > interval+grace` → **alert**(기존 `HealthCheck`/incident 재사용).
4. **registry**: `specs/heartbeat-registry.yaml`(job → interval, grace).

### 3.3 대상 (우선)
| job | interval | grace | 비고 |
|---|---|---|---|
| `handover-gen` | 10m | 5m | **신설**(현재 부재 → §2 fitness가 잡음) |
| `dev-poll` | 10m | 5m | devforge-dev-poll |
| `daily-structure` | 24h | 1h | 자동커밋/푸시 |
| `backup` | 24h | 2h | devforge-backup |
| `system-sync` | 30m | 10m | |

### 3.4 시사점
- **silent no-op 직접 해결**: "돌아야 하는데 조용히 안 도는" 모든 것을 **heartbeat로 탐지**(phase_tracker류 문제 재발 차단).
- **B(catch-up)의 트리거**와 정합: heartbeat 미수신 = "미실행" → D1의 B 컨트롤러 입력.

---

## 4. Drift detection (체크 2)

### 4.1 원리
**desired(Git/spec) vs live(system)** 대조 → OutOfSync 보고/교정. (Argo/Flux 개념, 도구 비채택.)

### 4.2 기존 자산 + 확장
| 대상 | 기존 | 확장 |
|---|---|---|
| 유닛(Quadlet/systemd) | `scripts/deploy/sync-units.sh --check` | 주기 실행 + CI |
| 코드 구조 | `tests/unit/test_code_structure.py`(양방향) | 유지 |
| 문서 | `tests/unit/test_docs_index.py`(등록) | 배선·경로 실재 추가 |
| **spec vs live** | — | 신규 `scripts/drift_check.py`: `specs/*.yaml`/unit desired vs `systemctl`/`podman` live 대조 |

### 4.3 구현 스케치
```python
# scripts/drift_check.py (요지) — desired(specs/units) vs live(systemctl/podman)
# - unit desired: systemd/user/*.timer·*.service vs systemctl --user list-units
# - container desired: containers/systemd/*.container vs podman ps
# - 출력: OutOfSync 목록 + exit 1 (CI/타이머에서 사용)
```
- **주기**: watchdog 타이머 또는 `drift-check.timer`(경량).
- **CI**: `drift_check.py`를 build 전 단계에(desired만 검증, live는 배포 후).

---

## 5. 구현 매핑 (파일)

| 구분 | 파일 | 내용 |
|---|---|---|
| 신규 | `tests/fitness/test_wiring.py` | 배선 fitness |
| 신규 | `tests/fitness/test_port_parity.py` | 이식 패리티 fitness |
| 신규 | `tests/fitness/test_completion.py` | 완결성(migration/활성) fitness |
| 신규 | `tests/fitness/test_contract.py` | 계약(context_jsonb/라우팅) fitness |
| 수정 | `tests/unit/test_docs_index.py` | 문서-실제(배선·경로) 확장 |
| 신규 | `scripts/deploy/heartbeat-ping.sh` | 공통 ping |
| 신규 | `specs/heartbeat-registry.yaml` | job→interval/grace |
| 수정 | watchdog | `check_heartbeats` 추가 |
| 신규 | `scripts/drift_check.py` | spec vs live 대조 |

> 신규 파일은 AGENTS "신규 파일 승인" 필요.

---

## 6. 테스트/검증

| 검증 | 방법 |
|---|---|
| fitness 자체 | 각 테스트가 **의도적 위반**에서 실패(예: 존재하지 않는 ExecStart 주입) |
| heartbeat | ping 후 ts 생성, grace 초과 시 watchdog alert(모의) |
| drift | desired와 live 불일치 주입 → exit 1 |
| 회귀 | `pytest -x --tb=short`, `ruff`, `mypy`, `lint-imports` 4 KEPT |

---

## 7. 롤아웃 (경량·증분)

| 단계 | 내용 | 게이트 |
|---|---|---|
| **F1** | 배선·문서 fitness(§2.2 1·5) | CI green, silent no-op(handover-gen) 노출 |
| **F2** | **heartbeat**(§3) — `handover-gen` 신설 + ping + watchdog | grace 내 미수신 alert 확인 |
| **F3** | 이식·완결·계약 fitness(§2.2 2·3·4) | v2 이식/마이그레이션 진행도 가시화 |
| **F4** | drift(§4) | OutOfSync 보고 |

> shadow-run 창 중 서비스 재기동 금지 → **F1(테스트/CI)은 즉시**, F2(유닛)는 창 이후.

---

## 8. 미해결 / 승인

1. **신규 파일**(fitness 4 + ping + registry + drift) — 승인 필요.
2. **heartbeat registry 값**(interval/grace) 초기 휴리스틱 → 실측 후 확정.
3. **drift_check 범위**(specs 어디까지 desired로 볼지) 확정.
4. **v2 이식**(issue-gen 등)은 별도(detection-remediation guide).

## 9. 출처
- Building Evolutionary Architectures — fitness functions(atomic/holistic, triggered/continuous, threshold/trend)
- Healthchecks.io / Dead Man's Snitch / Cronitor / deadcron / DeadManCheck — heartbeat+grace, output assertions
- Argo CD / Flux(CNCF) — desired vs live drift detection(개념만 차용)
- context7: systemd(`OnSuccess`/`OnFailure`/`SuccessAction`/`FailureAction`, `Type=oneshot`+`RemainAfterExit`), import-linter(계약 유형), pytest(fixtures/parametrize)
