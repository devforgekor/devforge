# 감시→(수정|미실행 실행) 구현 가이드 (표준 기반)

> Status: proposed · Date: 2026-09-23 · Owner: devforge
> Related: `plans/detection-remediation-architecture.md`(설계·근거), `plans/error-record-analysis-design.md`, `plans/watchdog-standard-compliance.md`, `plans/control-plane-roadmap.md`
> 목적: 설계(§표준)를 **파일·시그니처·데이터 흐름 수준**으로 구체화. **기존 incident 데이터를 그대로 트리거**로 사용.

---

## 0. 전제

- **트리거 데이터는 이미 존재**: `watchdog_incidents`(component, dedup_key, status, symptom, context_jsonb, action, action_result, action_error, fail_count, reopen_count, detected_at, last_seen_at, resolved_at). 실측 prefix/event: `svc:down`, `syssvc:down`, `llm:down`, `pipeline:stuck`, `oneshot:failed`, `timer:delay`.
- **선행 = S0(P2)**: 와치독이 컨테이너라 감지 오탐 → **호스트 유닛 전환 후** 착수(창 만료 후). 그 전엔 코드/테스트만.
- **경계**: 와치독=감지+기록(조치 없음). A/B 컨트롤러=조치. 분석=후속.

---

## 1. 데이터 → 라우팅 (기존 자산 활용)

| incident(dedup_key) | prefix | event | logic | kind | impact |
|---|---|---|---|---|---|
| `svc:ebook-watcher:down` | svc | down | **A(fix)** | service | mutating |
| `svc:container-devforge-mcp:down` | svc | down | A | container | mutating |
| `syssvc:caddy:down` | syssvc | down | **alert** | — | non-mutating |
| `llm:day-extract:down` | llm | down | A | cascade | mutating |
| `pipeline:...:stuck` | pipeline | stuck | A | pipeline | mutating |
| `oneshot:devforge-backup.service:failed` | oneshot | failed | **B(catch-up)** | oneshot_run | mutating |
| `timer:devforge-system-sync.timer:delay` | timer | delay | **B** | timer_kick | mutating |
| `dataimpulse:toki31:*` | dataimpulse | * | alert | — | non-mutating |

> `component:event_type` = **subject**. A는 `svc.> llm.> pipeline.> infra.>` 구독, B는 `oneshot.> timer.>` 구독(서로소).

---

## 2. 신규/수정 파일

| 구분 | 파일 | 내용 |
|---|---|---|
| **신규** | `src/devforge/domain/watchdog/routing.py` | `RouteDecision` + `ROUTES` 표 + `route()` (기존 `strategies._PREFIX` 승격) |
| **신규** | `src/devforge/ports/catchup.py` | `CatchupPort` Protocol |
| **신규** | `src/devforge/adapters/driven/recovery/systemd_catchup.py` | `SystemdCatchupAdapter`(oneshot start / timer kick) |
| **신규** | `src/devforge/application/controllers.py` | `FixController`(A) · `CatchupController`(B) — level-based reconcile |
| 수정 | `src/devforge/domain/watchdog/recovery/strategies.py` | `_PREFIX` → `routing.ROUTES`로 위임(중복 제거) |
| 수정 | `src/devforge/application/watchdog_service.py` | 조치 단계에서 `routing.route()` → A/B 디스패치 |
| 수정 | `src/devforge/core/config.py` | `WATCHDOG_CATCHUP_ENABLED`(기본 off), 임계값 |
| 테스트 | `tests/unit/domain/watchdog/test_routing.py`, `tests/unit/application/test_controllers.py` | 라우팅·컨트롤러 |

---

## 3. `routing.py` (순수, 계층 라우팅)

```python
from dataclasses import dataclass
from typing import Literal, Optional

Logic = Literal["fix", "catchup", "alert"]
Impact = Literal["mutating", "non-mutating", "undetermined"]

@dataclass(frozen=True)
class RouteDecision:
    logic: Logic          # A | B | alert-only
    kind: Optional[str]   # 기존 recovery kind (service/container/cascade/pipeline/oneshot_run/timer_kick)
    impact: Impact
    terminal: bool = False  # True면 재시도 금지

# prefix -> (logic, kind, impact)  — 기존 strategies._PREFIX 승격
ROUTES: dict[str, tuple[Logic, Optional[str], Impact]] = {
    "svc:":       ("fix", "service", "mutating"),
    "llm:":       ("fix", "cascade", "mutating"),
    "infra:":     ("fix", "cascade", "mutating"),
    "pipeline:":  ("fix", "pipeline", "mutating"),
    "oneshot:":   ("catchup", "oneshot_run", "mutating"),
    "timer:":     ("catchup", "timer_kick", "mutating"),
    "syssvc:":    ("alert", None, "non-mutating"),
    "system:disk":("alert", None, "non-mutating"),
    "dataimpulse:":("alert", None, "non-mutating"),
}

# terminal 판정(재시도 무의미) — 예: 권한/구성 오류
_TERMINAL_MARKERS = ("permission denied", "not found", "no such file")

def route(component: str, event_type: str, detail: str = "") -> RouteDecision:
    for prefix, (logic, kind, impact) in ROUTES.items():
        if component.startswith(prefix):
            terminal = any(m in detail.lower() for m in _TERMINAL_MARKERS)
            return RouteDecision(logic, kind, impact, terminal)
    return RouteDecision("alert", None, "non-mutating")  # 미지 prefix = 감시만
```
- **A/B 서로소**: `logic`로 컨트롤러 분기 → 중복 0.
- **CloudEvents `type` 대응**: `f"{prefix}.{event_type}"`.

---

## 4. `CatchupPort` + 어댑터 (B의 실행 수단)

```python
# ports/catchup.py
from typing import Protocol, Optional
class CatchupPort(Protocol):
    async def run_oneshot(self, unit: str) -> bool: ...   # systemctl --user start <oneshot>
    async def kick_timer(self, timer: str) -> bool: ...    # systemctl --user start <timer.service>

# adapters/driven/recovery/systemd_catchup.py
class SystemdCatchupAdapter:  # CatchupPort
    async def run_oneshot(self, unit):  return await _systemctl("start", unit)
    async def kick_timer(self, timer):  return await _systemctl("start", timer.replace(".timer", ".service"))
```
- oneshot은 `systemctl --user start <svc>`(실행). timer는 대응 `.service` start(kick). **실행 이력 확인은 컨트롤러 책임**(§5).

---

## 5. 컨트롤러 (level-based reconcile)

```python
# application/controllers.py
class FixController:            # A
    def __init__(self, recovery_port, health_ports, incidents): ...
    async def reconcile(self, inc: Incident) -> str:
        d = route(inc.component, _event(inc), inc.symptom or "")
        if d.logic != "fix" or d.terminal: return "skip"
        if await _is_healthy(inc.component):  # level-based: 현재 상태 재확인
            await self.incidents.resolve_if_open(inc.component); return "already-ok"
        if inc.fail_count >= MAX_ATTEMPTS: return "escalate"   # counter→HITL
        ok = await self.recovery_port.execute_recovery(RecoveryAction(inc.component, d.kind, inc.symptom, 0))
        return "recovered" if ok else "retry"

class CatchupController:        # B
    def __init__(self, catchup_port, incidents, run_log): ...
    async def reconcile(self, inc: Incident) -> str:
        d = route(inc.component, _event(inc), inc.symptom or "")
        if d.logic != "catchup": return "skip"
        unit = inc.component.split(":", 1)[1]
        if self.run_log.ran_recently(unit, window_sec=600): return "already-ran"  # 중복 방지
        ok = await self.catchup_port.run_oneshot(unit) if d.kind == "oneshot_run" else await self.catchup_port.kick_timer(unit)
        self.run_log.mark(unit); return "ran" if ok else "retry"
```
- **level-based**: `_is_healthy()`로 현재 상태 재확인(이벤트 신뢰 금지).
- **staleness**: 진입 시 `inc.last_seen_at`가 임계 초과/`status==resolved`면 skip.
- **counter/escalation**: `fail_count >= MAX_ATTEMPTS` → 조치 중단 + 알림(HITL).
- **중복 방지(B)**: `run_log`(실행 이력) 확인 후에만 실행.

---

## 6. 데이터 흐름

```
[watchdog 감시] → incident 기록(watchdog_incidents)
      → (라우팅) routing.route(component, event, detail)
           ├─ logic=fix     → FixController.reconcile → RecoveryPort → verify → incident resolve/retry
           ├─ logic=catchup → CatchupController.reconcile → CatchupPort → run_log 기록
           └─ logic=alert   → (조치 없음) 알림만
      → 결과를 incident(action/action_result/action_error)에 기록(감사)
```
- 모든 조치 결과를 `record_action(..., error=...)`로 남김(불변 감사).

---

## 7. 테스트 계획

| 테스트 | 검증 |
|---|---|
| `test_routing.py` | prefix→logic/kind/impact, terminal 마커, 미지 prefix→alert |
| `test_controllers.py::fix` | healthy면 skip, fail_count 임계→escalate, 복구 성공/실패 |
| `test_controllers.py::catchup` | 실행 이력 있으면 skip(중복 0), oneshot/timer 분기 |
| staleness | 오래된/resolved incident skip |
| 계층 | `lint-imports` 4 KEPT(신규 ports/catchup는 ports 계층) |
| 회귀 | `pytest -x --tb=short`, `ruff`, `mypy` |

---

## 8. 롤아웃 (게이트)

| 단계 | 내용 | 게이트 |
|---|---|---|
| **S0** | P2(와치독 호스트 유닛) | shadow-run 창 만료(09-24 13:32 UTC) |
| S1 | `routing.py` + 테스트(순수) | lint/mypy/test green, 동작 무변경 |
| S2 | **A** 배선(기존 recovery) — **dry-run** | shadow에서 오탐 0 확인 후 enable |
| S3 | **B** `CatchupPort` — **dry-run** | 실행 이력/중복 0, 한도·큐소진 skip 확인 |
| S4 | 거버넌스(staleness/counter/impact/audit) | 감사 커버리지 100% |

> S2/S3는 **`WATCHDOG_DRY_RUN=1`** 유지로 관찰 → enable은 별도 승인.

---

## 9. 미해결 / 승인 필요

1. **신규 파일 3~4개**(routing/catchup port/adapter/controllers) — AGENTS "신규 파일 승인" 필요.
2. **`run_log`(B 실행 이력)**: 인메모리 vs DB(`observations` 재사용). 1차 인메모리 권장.
3. **terminal 마커**: 초기 목록(§3)은 휴리스틱 — 실측 후 확정.
4. **S0 선행 필수**: P2 없이는 감지가 오탐 → A/B 무의미.
5. **impact 게이트**: mutating 자동 실행 vs 승인 — 정책 확정 필요.
