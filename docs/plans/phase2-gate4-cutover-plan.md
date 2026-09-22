# Phase 2 v2.1 — Gate 4 컷오버 실행 계획 (v2, 서버 정합성 반영)

- 작성: 2026-09-22 (KST)
- 기준: `docs/plans/phase2-detailed-guide-v2.md` (Gate 4 + Risk 1)
- 대체: `/tmp/phase2-v21-gate4-plan.md` (초안) — 아래 §1에서 오류 11건 정정
- 상태: 계획 확정 / 코드 선행(§2.1) 필요

---

## 0. 검증된 서버 사실 (계획 수정 근거)

| # | 사실 | 근거 |
|---|------|------|
| F1 | 앱 이미지 = `localhost/devforge-base:latest` (**Python 3.12.13**, `devforge` import 가능) | `podman images`, `podman run` |
| F2 | 네트워크 = `svc.pod`(`Network=devforge-net`). 컨테이너는 **`Pod=svc.pod`** 관례. pod 내 postgres = `127.0.0.1:5432` | `svc.pod`, quadlet들 |
| F3 | 시크릿 = **KV**(`kv-export-env.sh`/`kv-fetch-env.py`). **`~/.config/devforge/secrets.env`는 존재하지 않음** | `ls`, quadlet 관례 |
| F4 | KV 키: `DEVFORGE_POSTGRES_PASSWORD`, `SLACK_BOT_TOKEN_KEY`, `SLACK_CHANNEL`, `SLACK_SIGNING_SECRET_KEY`, `DEVFORGE_WATCHDOG_PING_SSH` (**`DEVFORGE_DATABASE_URL` 없음**) | `kv-fetch-env.py env` (키 이름) |
| F5 | 이미지에 **`systemd-notify`·`python3-systemd` 없음** | `podman run ... which` |
| F6 | `DatabaseGateway.from_config(config)` 존재 (SSOT) | `database_gateway.py:68` |
| F7 | v2.1에 **dry-run 플래그 없음** (legacy에는 있음: `orchestrator._run_*(dry_run)`) | `watchdog_service.py`, `orchestrator.py` |
| F8 | v2.1에 **liveness 파일 미지원** (legacy는 `/var/tmp/watchdog_last_cycle_ts`) | `orchestrator.py:312`, liveness timer |
| F9 | legacy 서비스명 = **`devforge-watchdog.service`** (`-legacy` 아님) | unit 확인 |
| F10 | 공유 상태: `watchdog_state.json` + `watchdog_incidents` 테이블 | legacy config, incidents.py |
| F11 | `create_watchdog_service(config)` · `run_cycle()`는 **async** | `watchdog_service.py:111,60` |
| F12 | base 이미지 `Entrypoint=null`, `Cmd=["python3"]` | `podman inspect` |

---

## 1. 초안(`/tmp/phase2-v21-gate4-plan.md`) 오류 정정

| # | 초안 | 정정 | 사유 |
|---|------|------|------|
| 1 | `EnvironmentFile=%h/.config/devforge/secrets.env` | **KV: `kv-export-env.sh` → `EnvironmentFile=/run/user/1000/kv-*.env`** | F3 (파일 없음) |
| 2 | `Exec=python3 -m src.devforge.cli_cmds.watchdog run-cycle --mode day` | **`Entrypoint=/bin/bash` + `Exec=/scripts/deploy/watchdog-v2-entrypoint.sh`** | 모듈 경로 오류(`src.` 불필요), `run-cycle`·`--mode` 미구현 |
| 3 | `Type=notify` + `daemon.notify(...)` | **`Type=simple`** | F5 (이미지에 notify 도구 없음) |
| 4 | `create_watchdog_service(mode)` | `create_watchdog_service(config)` | F11 |
| 5 | 동기 `while True: service.run_cycle()` | **`asyncio.run`/`await`** (async) | F11 |
| 6 | `RecoveryCoordinator.execute=True` / `WATCHDOG_RECOVERY_ENABLED=1` | **`dry_run` 모드 신규 구현**(legacy parity) | F7 (해당 플래그 없음) |
| 7 | `devforge-watchdog-legacy` | `devforge-watchdog` | F9 |
| 8 | shadow-run이 공유 상태를 그대로 씀 | **dry_run으로 incidents/state/Slack/recovery 차단** + 별도 state 파일 | F10 (오염) |
| 9 | (언급 없음) | **루프가 liveness 파일 기록** | F8 |
| 10 | (언급 없음) | `DatabaseGateway.from_config(config)` 사용 | F6 |
| 11 | DSN 미설정 | **entrypoint에서 `DEVFORGE_DATABASE_URL` 구성** (mcp 패턴) | F4 (KV에 URL 없음) |

---

## 2. 수정된 실행 계획

### 2.0. Task 등록 (추적)
```bash
python3 scripts/cli.py task add "Phase 2 v2.1 Gate 4: watchdog 컷오버" \
  -p P1 -d "dry_run+serve 루프 → quadlet → shadow 24h → recovery on → legacy off"
```
> `task add`는 **위치 인자 title** + `-p {P0,P1,P2}` + `-d` (초안의 `--title/--priority high`는 오류).

### 2.1. 코드 선행 (P0, 지금 가능)

**(a) `dry_run` 모드** — `WatchdogService.__init__(..., dry_run: bool = False)`.
`run_cycle`에서 `dry_run=True`이면: **incidents 쓰기·recovery 실행·알림·state 저장을 모두 건너뛰고** 로그만 남긴다.
(legacy `_run_*(dry_run)`과 동일 의미 → parity)

**(b) `serve` 루프 명령** — `adapters/driving/cli_cmds/watchdog.py`:
```python
@app.command("serve")
def serve() -> None:
    """Watchdog loop entrypoint (systemd unit). Reads env: WATCHDOG_*."""
    asyncio.run(_serve_loop())

async def _serve_loop() -> None:
    import os
    svc = _build_service()                      # injected factory (reads env)
    interval = int(os.environ.get("WATCHDOG_CHECK_INTERVAL_SEC", "60"))
    while True:
        await svc.run_cycle()
        _write_liveness()
        await asyncio.sleep(interval)
```

**(c) liveness 파일** — `/var/tmp/watchdog_last_cycle_ts`에 epoch 기록(legacy `_write_liveness`와 동일) → 기존 `devforge-watchdog-liveness.timer`가 계속 유효.

**(d) factory 확장** — `create_watchdog_service(config, dry_run=False)` + `DatabaseGateway.from_config(config)` (F6). CLI `cli.py`의 `_watchdog_service_factory`가 env `WATCHDOG_DRY_RUN`을 읽어 전달.

**검증**: `pytest -x --tb=short`, `lint-imports`, `mypy src/devforge` (모두 green 유지).

### 2.2. entrypoint (컨테이너 DSN 구성, F4/F11)
`scripts/deploy/watchdog-v2-entrypoint.sh` (신규):
```bash
#!/bin/bash
set -euo pipefail
if [ -z "${DEVFORGE_DATABASE_URL:-}" ] && [ -n "${DEVFORGE_POSTGRES_PASSWORD:-}" ]; then
  export DEVFORGE_DATABASE_URL="postgresql+asyncpg://postgres:${DEVFORGE_POSTGRES_PASSWORD}@127.0.0.1:5432/devforge_app"
fi
exec python3 -m devforge.cli watchdog serve
```
> pod 내부이므로 host는 `127.0.0.1:5432`(F2), user는 `postgres` (mcp entrypoint와 동일).

### 2.3. Quadlet (수정판)
`~/.config/containers/systemd/devforge-watchdog-v2.container`:
```ini
[Unit]
Description=DevForge Watchdog v2.1 (hexagonal, shadow)
After=svc-pod.service
Wants=svc-pod.service

[Container]
ContainerName=devforge-watchdog-v2
Image=localhost/devforge-base:latest
Pull=never
Pod=svc.pod
Entrypoint=/bin/bash
Exec=/scripts/deploy/watchdog-v2-entrypoint.sh
Volume=/opt/projects/server/src:/src:Z
Volume=/opt/projects/server/scripts:/scripts:Z
Volume=/opt/ai_data:/opt/ai_data:Z
Environment=PYTHONPATH=/src
Environment=WATCHDOG_DRY_RUN=1
Environment=WATCHDOG_STATE_FILE=/opt/ai_data/scripts/watchdog_state.v2.json
Environment=WATCHDOG_CHECK_INTERVAL_SEC=60
EnvironmentFile=/run/user/1000/kv-devforge-watchdog.env

[Service]
ExecStartPre=/opt/projects/server/scripts/deploy/kv-export-env.sh /run/user/1000/kv-devforge-watchdog.env DEVFORGE-POSTGRES-PASSWORD,SLACK-BOT-TOKEN-KEY,SLACK-CHANNEL
ExecStopPost=/bin/rm -f /run/user/1000/kv-devforge-watchdog.env
Restart=always
RestartSec=60
TimeoutStopSec=30

[Install]
WantedBy=default.target
```
**핵심**: `Pod=svc.pod`(DB 도달) · `Type` 미지정(=simple) · `WATCHDOG_DRY_RUN=1`(shadow) · **별도 state 파일**(F10 오염 방지) · KV 시크릿.

### 2.4. 백업
```bash
TS=$(date +%Y%m%d)
cp -r scripts/lib/watchdog scripts/lib/watchdog.backup.$TS
cp /opt/ai_data/scripts/watchdog_state.json /opt/ai_data/scripts/watchdog_state.json.backup.$TS
cp ~/.config/systemd/user/devforge-watchdog.service ~/.config/systemd/user/devforge-watchdog.service.bak.$TS
```

### 2.5. 배포 + shadow-run (24h, recovery off)
```bash
systemctl --user daemon-reload
systemctl --user start devforge-watchdog-v2
journalctl --user -u devforge-watchdog-v2 -f
```
**비교**: v2.1 로그(dry-run) vs legacy(`devforge-watchdog`) journal. DB `watchdog_incidents`는 v2.1이 **쓰지 않음**(F10).
**확인**: check 결과 일치 · dedup 동작 · **incidents/state/Slack 무변경** · ETA 정확도 · liveness 파일 갱신.

### 2.6. Recovery 활성화 (shadow → prod)
```bash
# dry_run 해제 + state 파일을 legacy와 동일하게 전환
systemctl --user edit devforge-watchdog-v2   # WATCHDOG_DRY_RUN 제거, WATCHDOG_STATE_FILE=/opt/ai_data/scripts/watchdog_state.json
systemctl --user restart devforge-watchdog-v2
```

### 2.7. Legacy 중단
```bash
systemctl --user stop devforge-watchdog.service
systemctl --user disable devforge-watchdog.service
# 이후 scripts/lib/watchdog 아카이브(별도 task)
```

---

## 3. 선결 체크리스트

- [x] §2.1 코드(dry_run/serve/liveness/factory) 구현 + green (2026-09-22)
- [x] `scripts/deploy/watchdog-v2-entrypoint.sh` 생성 + 실행권한
- [x] `kv-export-env.sh`에 `SLACK-BOT-TOKEN-KEY`,`SLACK-CHANNEL` 포함 확인
- [x] quadlet에서 `devforge` import 확인: `python3 -c "import devforge"` (PYTHONPATH=/src)
- [x] pod 내 DB 도달: entrypoint DSN으로 `SELECT 1` 성공
- [x] 백업(§2.4) 완료 (20260922)
- [x] shadow 중 `watchdog_state.json`·`watchdog_incidents`·Slack **무변경** 확인 (dry-run 검증)
- [x] liveness 파일 갱신 확인 (`/var/tmp/watchdog_last_cycle_ts`)

---

## 4. 롤백

| 단계 | 롤백 |
|------|------|
| 2.5 | `systemctl --user stop devforge-watchdog-v2` (legacy 계속 동작) |
| 2.6 | `WATCHDOG_DRY_RUN=1` 재설정 + restart |
| 2.7 | `systemctl --user enable --now devforge-watchdog.service` (legacy 복귀) |
| 데이터 | `watchdog_state.json.backup.$TS` 복원 |

---

## 5. 예상 시간

| 단계 | 시간 |
|------|------|
| 2.0 Task | 1분 |
| 2.1 코드(dry_run/serve/liveness) | 45–60분 |
| 2.2 entrypoint | 10분 |
| 2.3 quadlet | 15분 |
| 2.4 백업 | 2분 |
| 2.5 shadow-run | 24h(대기) |
| 2.6–2.7 전환 | 15분 |

---

## 6. 초안 대비 개선 요약

1. **시크릿**: `secrets.env`(없음) → **KV**(F3)
2. **배포**: 잘못된 모듈 경로/`Type=notify` → **entrypoint + simple**(F1/F2/F5)
3. **안전**: shadow-run이 공유 상태를 오염하지 않도록 **`dry_run` 구현**(F7/F10) — 초안의 최대 결함
4. **연속성**: **liveness 파일** 기록으로 기존 타이머 유지(F8)
5. **정확성**: `from_config`(F6), async 루프(F11), 서비스명(F9), task CLI(F-명령)
