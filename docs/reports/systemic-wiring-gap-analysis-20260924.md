# 시스템적 배선 갭 분석 (2026-09-24)

> Status: record · Date: 2026-09-24 · Owner: devforge
> Related: `plans/fitness-functions-heartbeat-drift-guide.md`(해법), `plans/detection-remediation-architecture.md`, `reports/logic-recording-tracking-audit-20260923.md`, `reports/incident-issue-pr-loop-audit-20260923.md`
> 목적: 이번 세션에서 드러난 개별 문제들을 **하나의 시스템적 패턴**으로 정리(근본원인 + 증거).

---

## 0. 문제 (한 문장)

> **설계·문서·기대는 풍부한데 "배선(wiring)"이 끊겨 있다** — 자동 트리거·이식·완결이 검증되지 않아 **silent no-op / false-negative / stuck** 으로 나타난다.

## 1. 근본 원인 (5)

| # | 원인 | 설명 |
|---|---|---|
| 1 | **배선 미검증** | 있다고 가정한 트리거(타이머/훅)가 실제 없거나 잘못 참조 → 조용히 무동작 |
| 2 | **레거시↔v2 이식 갭** | 리팩터가 코드는 옮겼으나 glue(자동화·도구)를 안 옮김 |
| 3 | **다중 SSOT / 계약 분열** | 같은 개념의 출처가 둘 이상(text vs jsonb, prefix vs repeat, 문서 vs 실제) |
| 4 | **완결성 미검증** | 시작(claim·마이그레이션·활성)만 하고 **끝(PR·apply·enable)**을 확인 안 함 |
| 5 | **문서=실제 가정** | 문서/주석이 주장한 배선·경로를 실제와 대조하지 않음 |

## 2. 증거 매트릭스 (실측)

| 문제 | 원인 | 증거 |
|---|---|---|
| `phase_tracker` 무동작 | 1 | `docs/phases.md` 아카이브 → `scan_phases_md`가 `{}` 반환(silent no-op), 15분 호출이 무의미 |
| 핸드오버 자동기록 없음 | 1, 5 | `handover-gen.timer` **부재**(전역 0), SessionEnd 훅=`slack_notify.py`(update_handover 아님) |
| error-record 기록 미활성 | 4 | 라이브 `watchdog_incidents`에 `context_jsonb` **없음**(마이그레이션 미적용) |
| incident→이슈→PR 정체 | 3, 4 | #8/#9 OPEN, `dev_pipeline` state `pr_created={}` |
| v2 감지 오탐·이슈 미이식 | 2 | v2 컨테이너 도구 부재(WATCHDOG-CONTAINER-TOOLS); `src/devforge`에 issue 생성 없음 |
| 문서 드리프트(다수) | 3, 5 | phase 표기·유닛 22↔30·code-structure 미등재 등 |
| daily-structure가 작성물 흡수 | 3 | `git add -A`에 docs 필터 없음(2026-09-24 수정) |

## 3. 시스템적 해법 (개별 수정이 아니라)

| 원인 | 표준 해법 |
|---|---|
| 1 배선 미검증 | **Fitness function**(배선 실재·참조) + **dead-man's switch**(트리거 미발화 경보) |
| 2 이식 갭 | **Parity fitness function**(legacy 기능이 v2에 존재) |
| 3 다중 SSOT | **단일 desired state**(Git SSOT) + **contract fitness** |
| 4 완결성 | **Output assertion** + 완결 fitness(claim→PR, migration applied) |
| 5 문서=실제 | **Drift detection**(desired vs live) + 문서-실제 fitness |

> 구체 구현: `plans/fitness-functions-heartbeat-drift-guide.md`(프레임=Fitness Functions, 체크=heartbeat·drift). **비목표**: 구조 변경·GitOps 도구 도입(right-size).

## 4. 원칙

- **표준을 이 시스템에 맞춘다**(경량·증분), 시스템을 표준에 맞추지 않는다.
- **검증 계층을 추가**한다(구조 변경 아님).
- 기존 자산(`import-linter`·`test_code_structure`·`test_docs_index`·`sync-units --check`)을 **확장**한다.

## 5. 검증 명령 (재현)

```bash
find /etc/systemd ~/.config/systemd -iname "*handover*"           # 0 = 부재
podman exec postgres psql -U devforge -d devforge_app -c "\d watchdog_incidents" | grep context_jsonb  # 없음
python3 -c "import sys;sys.path.insert(0,'scripts');from lib.dev_pipeline import _load_state;print(_load_state()['pr_created'])"
grep -ciE "trivy|sbom|cosign" .github/workflows/ci.yml            # 0
```
