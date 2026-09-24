# 로직 기록·추적 감사 (2026-09-23)

> Status: record · Date: 2026-09-23 · Owner: devforge
> Related: `plans/detection-remediation-architecture.md`, `plans/error-record-analysis-design.md`, `agent_docs/session.md`, `scripts/update_handover.py`
> 목적: "로직 변경이 어떻게 기록·추적되는가"를 실측 검증하고, 끊긴 고리를 식별.

---

## 0. 결론 (요약)

**기록은 되지만 자동 트리거가 끊겨 있다.**
- Handover DB(decisions/known_issues/completed_log)는 정상 작동하나, **자동 생성 트리거(`handover-gen.timer`·SessionEnd 훅)가 실제로는 없어** 세션 중 **수동 호출**에만 의존.
- error-record **구조화 기록은 미활성**(마이그레이션 미적용).
- Git 커밋/푸시 추적은 정상(자동커미터 필터 일관).
- 감지→수정/미실행 실행은 **설계만**(코드 없음).

---

## 1. 기록 계층 (recording) — 실측

| 계층 | 상태 | 근거 |
|---|---|---|
| Handover DB (decisions/known_issues/completed_log/session_checkpoints) | ✅ 작동 | 누적 121/71/139/47, **오늘 completed_log 36·decisions 9** |
| error-record 구조화 (`context_jsonb`/`action_error`) | ❌ **미활성** | 라이브 `watchdog_incidents`에 `context`(text)만 — **마이그레이션 미적용** |
| watchdog incidents | ⚠️ **legacy만** | 최근 기록 전부 legacy(11:12); v2는 dry-run이라 미기록 |

---

## 2. 추적 계층 (tracking) — 실측

| 계층 | 상태 | 근거 |
|---|---|---|
| Git 커밋/푸시 | ✅ 정상 | daily-structure·system_sync(작성물 필터) + open-newhand(설명형). 규칙 일관 |
| 문서 추적 (REFACTORING_STATUS/INDEX) | ✅ 현행 | current_phase 2.5, INDEX living/record |
| alembic 마이그레이션 추적 | ⚠️ **확인 불가** | `alembic current`가 DSN env 없이 `Could not parse SQLAlchemy URL` 실패 |

---

## 3. 핵심 발견 — 자동 기록 트리거 부재 ⚠️

`scripts/update_handover.py` docstring: `Path: systemd:handover-gen.timer`, "Triggered by SessionEnd hook AND 10-min checkpoint timer".
**실측은 불일치**:

| 문서 주장 | 실측 |
|---|---|
| `systemd:handover-gen.timer`(10분) | **존재하지 않음**(전역 systemd 검색 0) |
| SessionEnd hook → update_handover | SessionEnd 훅 = **`slack_notify.py`**(handover 미기록) |
| — | opencode.json에 **plugin/hooks 없음** |

**영향**: 핸드오버는 **에이전트가 세션 중 수동 호출**할 때만 생성됨(cp 142~144도 수동 추정). 자동 캡처 없음 → 세션을 빠뜨리면 기록 유실.

---

## 4. 미구현 (설계만)

- **감지→수정/미실행 실행**: `plans/detection-remediation-architecture.md` + `-implementation-guide.md` (문서만, 코드 0). 트리거 데이터(`watchdog_incidents`)는 이미 존재.

---

## 5. 권고

| # | 조치 | 우선 |
|---|---|---|
| 1 | **핸드오버 자동 트리거 복구** — ① `handover-gen.timer`(10분) 신설(문서대로) 또는 ② SessionEnd 훅에 `update_handover.py` 추가. **문서-실제 불일치 해소** | **P1** |
| 2 | **error-record 마이그레이션 적용**(승인) → 구조화 기록 활성 | P1 |
| 3 | **alembic 추적** — DSN 주입 래퍼(`kv-fetch-env.py`) 또는 env 설정 | P2 |
| 4 | 감지→수정/미실행 실행 구현(S0=P2 선행) | P2(창 이후) |

> 문서 정합: 1번은 docstring/`agent_docs/session.md`와 실제를 일치시켜야 함(코드=SSOT, 실제 배선에 맞춤).

---

## 6. 검증 명령 (재현)

```bash
# 기록량
podman exec postgres psql -U devforge -d devforge_app -c \
  "SELECT count(*) FROM completed_log; SELECT count(*) FROM session_checkpoints;"
# error-record 컬럼
podman exec postgres psql -U devforge -d devforge_app -c \
  "\d watchdog_incidents" | grep -E "context|action_error"
# 자동 트리거 부재 확인
find /etc/systemd ~/.config/systemd -iname "*handover*"; grep -rl update_handover /etc/systemd ~/.config/systemd
```
