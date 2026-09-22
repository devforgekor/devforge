# Phase 2 v2.1 — Gate 4 코드 선행 스펙 (§2.1 상세)

- 작성: 2026-09-22 (KST)
- 상위: `docs/plans/phase2-gate4-cutover-plan.md` §2.1
- 목적: 컷오버 전 **반드시 선행**해야 하는 코드 변경(dry_run·serve 루프·liveness·factory)을 코드 레벨로 확정
- 원칙: legacy parity 유지 (`orchestrator._run_*(dry_run)` 의미와 동일)

---

## 1. `WatchdogService` — `dry_run` 모드

**파일**: `src/devforge/application/watchdog_service.py`

### 1.1 생성자
```python
def __init__(self, config, registry, check_coordinator, recovery_coordinator,
             recovery_port, notification_ports, incident_repo,
             state_storage=None, dry_run: bool = False) -> None:
    ...
    self._dry_run = dry_run
```

### 1.2 `run_cycle` — 부수효과 차단
`dry_run=True`이면 **incidents 쓰기 · recovery 실행 · 알림 · state 저장을 모두 건너뛴다**.
체크 결과 기록(CheckCoordinator)과 in-memory tracker 갱신은 **유지**(shadow 비교 목적).

```python
async def run_cycle(self) -> dict[str, Any]:
    checks = await self._checks.run()
    failed = self._checks.failed(checks)

    # 1. healthy → resolve incidents
    for c in checks:
        if c.is_healthy and not self._dry_run:
            await self._incidents.resolve_if_open(c.component)

    # 2. failed → incident → recovery → alert
    for c in checks:
        if c.is_healthy:
            continue
        t = self._registry.get(c.component)
        action = self._recovery.plan(c.component, c.detail)   # no mutation

        if self._dry_run:
            log.info("[dry-run] %s failed: %s (would %s)",
                     c.component, c.detail,
                     action.kind if action is not None else "alert-only")
            continue

        inc_id = await self._incidents.record_detect(
            c.component, self._event_type(c.component), c.detail)
        if action is not None and t.can_attempt_recovery():
            t.schedule_next_attempt(action.backoff_sec)
            ok = await self._recovery_port.execute_recovery(action)
            await self._incidents.record_action(inc_id, action.kind, ok)
            if ok:
                self._recovery.record_result(c.component, True)
                for n in self._notifiers:
                    await n.send_recovery(c.component, f"{action.kind} ok")
        if t.is_degraded() and t.can_alert():
            for n in self._notifiers:
                await n.send_alert(c.component, t.state.value, c.detail)

    if not self._dry_run:
        self._persist()
    return {"checks": len(checks), "failed": len(failed), "dry_run": self._dry_run,
            "timestamp": datetime.now(timezone.utc).isoformat()}
```

**legacy parity**: legacy dry_run은 `elif not dry_run and not is_experiment_active():`로
복구 브랜치 전체를 건너뛰고 `else`에서 실패만 기록한다. 위 구현은 체크 기록(1회) 후
복구/알림/incidents를 건너뛰므로 동일 의미다.

---

## 2. `create_watchdog_service` — dry_run + from_config

**파일**: 동일. **변경 3건**:
1. `DatabaseGateway(get_config().db_url_async)` → **`DatabaseGateway.from_config(get_config())`** (F6, SSOT)
2. **Slack 토큰 env 이름 정정** (검토 R1): `os.environ.get("SLACK_BOT_TOKEN")` → **`SLACK_BOT_TOKEN_KEY`**
   (KV 키·`mcp/server.py:515`와 일치. 현 코드는 토큰을 못 찾아 **Slack 알림이 조용히 비활성화**됨)
3. `dry_run` 파라미터 추가 → `WatchdogService(...)`로 전달

```python
def create_watchdog_service(config: WatchdogConfig, dry_run: bool = False) -> WatchdogService:
    ...
    cfg = get_config()
    if not cfg.db_url_async:
        raise ConfigurationError("DEVFORGE_DATABASE_URL is not set (run inside devforge-net)")
    gateway = DatabaseGateway.from_config(cfg)   # 검토 R4: 빈 DSN이면 cryptic URL 오류 → 명시적 에러
    ...
    slack_token = os.environ.get("SLACK_BOT_TOKEN_KEY", "")   # 검토 R1
    ...
    service = WatchdogService(config, registry, check_coordinator, recovery_coordinator,
                              recovery_port, notifiers, incident_repo, state_storage,
                              dry_run=dry_run)
    service.load_state()
    return service
```

> shadow에서 `config.state_file`은 env `WATCHDOG_STATE_FILE`로 **별도 파일**을 가리킨다(오염 방지).
> **검토 R5**: shadow 중에는 `WATCHDOG_LIVENESS_FILE=/var/tmp/watchdog_v2_last_cycle_ts`로 legacy와 분리한다.

---

## 3. `serve` 루프 + liveness

**파일**: `src/devforge/adapters/driving/cli_cmds/watchdog.py`

```python
import os
import time
from pathlib import Path

LIVENESS_FILE = Path(os.environ.get("WATCHDOG_LIVENESS_FILE", "/var/tmp/watchdog_last_cycle_ts"))

def _write_liveness() -> None:
    """[WHY] legacy orchestrator._write_liveness parity — keeps devforge-watchdog-liveness.timer valid."""
    try:
        LIVENESS_FILE.write_text(str(int(time.time())))
    except OSError as e:
        log.warning("liveness write failed: %s", e)

@app.command("serve")
def serve() -> None:
    """Watchdog loop entrypoint for the systemd unit (reads WATCHDOG_* env)."""
    asyncio.run(_serve_loop())

async def _serve_loop() -> None:
    # [WARNING] 검토 R3: _build_service()는 내부에서 asyncio.run을 호출하므로
    #           실행 중 루프(async) 안에서 부르면 RuntimeError가 난다.
    #           반드시 주입된 factory를 직접 await 한다.
    if _factory is None:
        raise RuntimeError("watchdog.init() not called from composition root")
    svc = await _factory()                      # single event loop
    interval = svc.check_interval_sec           # 검토 R7: config SSOT 사용
    log.info("watchdog serve: interval=%ss dry_run=%s", interval, svc.dry_run)
    while True:
        await svc.run_cycle()
        _write_liveness()
        await asyncio.sleep(interval)
```

- **async**: `run_cycle()`는 코루틴 → `await` (F11)
- **single loop**: `await _factory()` — `asyncio.run` 중첩 금지 (검토 R3)
- **liveness**: 기존 `devforge-watchdog-liveness.timer`가 이 파일을 검사 → **타이머 무변경**(F8)
- `KeyboardInterrupt`는 asyncio.run이 전파 → systemd가 종료 처리

**`WatchdogService` 읽기 전용 프로퍼티** (검토 R6/R7):
```python
@property
def dry_run(self) -> bool: return self._dry_run
@property
def check_interval_sec(self) -> int: return self._config.check_interval_sec
```

---

## 4. 컴포지션 루트 — env 전달

**파일**: `src/devforge/cli.py` (`_watchdog_service_factory`)

```python
def _watchdog_service_factory() -> Any:
    import os
    from devforge.application.watchdog_service import create_watchdog_service
    from devforge.core.config import WatchdogConfig
    dry_run = os.environ.get("WATCHDOG_DRY_RUN", "") == "1"
    return create_watchdog_service(WatchdogConfig.from_env(), dry_run=dry_run)
```

> CLI는 여전히 application을 import하지 않는다(layering 유지).

---

## 5. 컨테이너 entrypoint (DSN 구성)

**신규**: `scripts/deploy/watchdog-v2-entrypoint.sh`
```bash
#!/bin/bash
set -euo pipefail
# [WHY] KV에 DEVFORGE_DATABASE_URL이 없다(F4). mcp entrypoint와 동일하게 pod 내부 DSN을 구성.
if [ -z "${DEVFORGE_DATABASE_URL:-}" ] && [ -n "${DEVFORGE_POSTGRES_PASSWORD:-}" ]; then
  export DEVFORGE_DATABASE_URL="postgresql+asyncpg://postgres:${DEVFORGE_POSTGRES_PASSWORD}@127.0.0.1:5432/devforge_app"
fi
exec python3 -m devforge.cli watchdog serve
```
```bash
chmod +x scripts/deploy/watchdog-v2-entrypoint.sh
```

---

## 6. 테스트 (신규)

**파일**: `tests/unit/application/test_watchdog_service.py` (추가)
- `test_dry_run_does_not_write_incidents` — `record_detect`/`record_action`/`resolve_if_open` 호출 0
- `test_dry_run_does_not_notify` — `send_alert`/`send_recovery` 0
- `test_dry_run_does_not_persist` — `state.save` 0
- `test_dry_run_does_not_execute_recovery` — `recovery_port.actions == []`
- `test_dry_run_still_records_check` — tracker에 DEGRADED 반영
- `test_dry_run_flag_in_result` — `result["dry_run"] is True`

**파일**: `tests/unit/adapters/driving/test_watchdog_cli.py` (추가)
- `test_serve_writes_liveness_and_loops` — `_write_liveness` 경로 monkeypatch, 1회 루프 후 취소(타임아웃)

---

## 7. 검증

```bash
pytest -x --tb=short                      # green (기존 209 + 신규)
ruff check src/devforge
mypy src/devforge
lint-imports                              # 4 KEPT
# 컨테이너 내부 import 확인
podman run --rm -v /opt/projects/server/src:/src:Z -e PYTHONPATH=/src \
  localhost/devforge-base:latest python3 -m devforge.cli watchdog --help
```

---

## 8. 체크리스트

- [ ] `WatchdogService.dry_run` + `run_cycle` 가드 + `dry_run`/`check_interval_sec` 프로퍼티
- [ ] `create_watchdog_service(config, dry_run=False)` + `from_config` + **빈 DSN 명시적 에러**
- [ ] **Slack 토큰 env `SLACK_BOT_TOKEN_KEY`** 정정 (R1)
- [ ] CLI `serve` — **`await _factory()`** (R3) + `_write_liveness`
- [ ] `cli.py` factory env(`WATCHDOG_DRY_RUN`)
- [ ] `scripts/deploy/watchdog-v2-entrypoint.sh`
- [ ] 신규 테스트 7건 green
- [ ] `mypy`/`ruff`/`lint-imports` 유지

---

## 9. 추가 검토에서 발견한 결함 (R1–R7)

코드·서버 대조로 확인한 추가 결함. §1–§7에 반영 완료.

| # | 결함 | 심각도 | 확인 | 조치 |
|---|------|--------|------|------|
| **R1** | **Slack 토큰 env 이름 불일치** — 코드는 `SLACK_BOT_TOKEN`, 실제 키는 `SLACK_BOT_TOKEN_KEY` | 🔴 **높음** | `watchdog_service.py:146` vs `mcp/server.py:515` | §2에서 `SLACK_BOT_TOKEN_KEY`로 정정 → 알림 복구 |
| **R2** | CLI `check`/`resolve`가 `asyncio.run` **2회** (엔진 생성 루프 ≠ 사용 루프) | 🟡 중간 | `watchdog.py:39,47` | 엔진이 lazy 연결이라 현실 동작은 하나, **단일 루프로 정리 권장** |
| **R3** | §2.1 `serve` 스펙이 async 함수에서 `_build_service()`(=내부 `asyncio.run`) 호출 → **`RuntimeError: asyncio.run() cannot be called from a running event loop`** | 🔴 **높음** | 스펙 자체 | §3에서 **`await _factory()`** 로 수정 |
| **R4** | 빈 DSN → **`ArgumentError: Could not parse SQLAlchemy URL`** (cryptic) | 🟡 중간 | 호스트 `devforge watchdog check` 재현 | §2에서 `ConfigurationError` 명시 |
| **R5** | shadow 중 legacy와 **liveness 파일 공유** → 한쪽 실패를 가림 | 🟡 중간 | F8 | `WATCHDOG_LIVENESS_FILE` 분리 |
| **R6** | CLI가 `svc._dry_run` private 접근 | 🟢 낮음 | §3 스펙 | `dry_run` 프로퍼티 |
| **R7** | 루프 주기를 env에서 직접 읽음 (config와 이중화) | 🟢 낮음 | §3 스펙 | `check_interval_sec` 프로퍼티 |

### 검증 근거 (재현 명령)
```bash
# R1: 토큰 이름
grep -rn "SLACK_BOT_TOKEN" src/devforge/ | head
#   watchdog_service.py:146 → SLACK_BOT_TOKEN   (버그)
#   mcp/server.py:515     → SLACK_BOT_TOKEN_KEY (정답)

# R4: 빈 DSN cryptic 오류 (호스트)
python3.12 -m devforge.cli watchdog check
#   → ArgumentError: Could not parse SQLAlchemy URL from given URL string

# R2: asyncio.run 2회
grep -n "asyncio.run" src/devforge/adapters/driving/cli_cmds/watchdog.py
```

### 결론
- **R1·R3는 컷오버 실패를 유발**하므로 §2.1에 **반드시 포함**해야 한다.
- R2·R4는 즉시 실패는 아니나 **운영 안정성**을 위해 정리 권장.
- R5–R7은 정합성/가독성 개선.
- **미해결로 남긴 것 없음** — 모두 §1–§7에 반영됨.
