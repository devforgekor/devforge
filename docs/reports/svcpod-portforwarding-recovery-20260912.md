# svc pod 호스트 포트포워딩 장애 복구 · 재발방지 (2026-09-12)

> Status: record · Date: 2026-09-12 · Owner: devforge · Related: [`reports/watchdog-port-conflict-gap-analysis.md`](./watchdog-port-conflict-gap-analysis.md), [`reports/deep-dive-watchdog-port-conflict-design.md`](./deep-dive-watchdog-port-conflict-design.md), [`_archive/plans/azure-golden-image-rebuild-handover.md`](../_archive/plans/azure-golden-image-rebuild-handover.md)

## 1. 목적
Deep Dive 백엔드(devforge-mcp, HTTP :8000) 도달 불가로 다음 세션 진행이 차단된 원인을 정비하고, 동일 장애가 재발하지 않도록 watchdog에 **호스트 포트포워딩 감지·자동복구**를 추가한다.

## 2. 방법
1. **진단**: `podman ps`(컨테이너 healthy) vs `ss -ltn`(호스트 LISTEN 부재), 컨테이너 내부 `curl`(200)로 계층 분리. `rootlessport` 프로세스 부재 확인.
2. **재현/검증**: `rootlessport` 프로세스를 kill해 실제 장애를 주입하고, 구현한 복구 함수로 복구되는지 확인.
3. **구현**: 기존 watchdog 패턴(tracker/`incidents`/`graduated_recover`/`dry_run`)을 재사용해 감지·복구 함수 및 러너 추가.
4. **동반 수정**: 장애 창에서 발견된 Quadlet/코드 버그를 함께 수정.

## 3. 결과

### 3.1 증상
- 컨테이너는 정상(`devforge-mcp` healthy, 내부 `curl 127.0.0.1:8000/health` = 200).
- 그러나 **호스트에서 `127.0.0.1:8000/8002/8085/8191` 전부 connection refused**(`rootlessport`·호스트 LISTEN 부재).
- opencode 시작 시 `devforge-mcp` MCP 로드 실패(`server unavailable status=failed`) → `deepdive_step_*` 사용 불가. FlareSolverr(:8191)도 ebook 세션에서 불가.

### 3.2 근본 원인
- rootless bridge에서 발행 포트는 **`rootlessport`(userspace proxy)** 가 포워딩한다. 이 프로세스가 죽으면 컨테이너 healthcheck(컨테이너 **내부** 127.0.0.1만 검사)는 통과한다.
- svc pod 재기동 직후부터 전 포트가 미포워딩 → rootlessport가 재생성되지 않은 상태로 고착.
- 기존 watchdog은 `container-devforge-mcp`를 **ALERT_ONLY**로 `systemd 유닛 active`만 확인 → 컨테이너 healthy라 미탐지.

### 3.3 수정 내역
| # | 대상 | 내용 |
|---|---|---|
| 1 | svc pod | `systemctl --user restart svc-pod.service` → `rootlessport` 재생성, 4포트 복구, devforge-mcp 25툴/FlareSolverr 정상 |
| 2 | **watchdog (재발방지)** | `check_svcpod_ports`(TCP connect) + `recover_svcpod_forwarding`(svc-pod 재기동) + `_run_svcpod_forwarding`(60s 주기, `_run_common_checks` 통합). Deep Dive `dp-20260912-watchdog-svcpod-portforwarding`, task#32 |
| 3 | Quadlet | 주석뿐인 `container-flaresolverr.container` stub → `_disabled/*.disabled`. `podman-user-generator` 실패(`no Image or Rootfs key specified`) 제거 |
| 4 | 버그 수정 | `activity_summarizer.py` int.isdigit() `AttributeError`; `watchdog/checker.py` `MODE_FILE_INFERENCE` import 누락; `handover_db.py` `skip_checkpoint` 시 `VALUES (None,...)`로 조용히 실패 |

#### watchdog 구현 상세
- `lib/watchdog/config.py`: `SVCPOD_UNIT`, `SVCPOD_PUBLISHED_PORTS`(8000/8002/8085/8191) — SSOT는 `~/.config/containers/systemd/svc.pod`의 `PublishPort`.
- `lib/watchdog/checker.py`: `check_svcpod_ports()` — 각 포트 TCP connect, 다운 포트 목록 반환.
- `lib/watchdog/recovery.py`: `recover_svcpod_forwarding()` — `svc-pod.service` 재기동 후 최대 ~30s 재검사.
- `lib/watchdog/orchestrator.py`: `_run_svcpod_forwarding()` — backoff/circuit, `dry_run`·`_test_active`·experiment 존중.
- postgres는 `BindsTo=svc-pod`로 함께 재기동되나 데이터는 볼륨(`/mnt/lv_db`)이라 안전.

## 4. 검증
- `py_compile` OK · `cli.py lint` 위반 0 · LSP 재색인 후 신규 심볼 오류 0.
- `run_day_checks(dry_run=True)` → `svc-pod-forwarding: ok=True (4 ports forwarded)` (총 23 services).
- **실장애 주입**: `rootlessport` kill → 4포트 전부 down → `recover_svcpod_forwarding()` → **~26s 내 복구 True**, MCP/fastapi/flaresolverr 200.
- `devforge-watchdog.service` 재기동으로 신규 코드 라이브.

## 5. 결론
호스트 포워딩 장애는 컨테이너 healthcheck로 감지 불가한 계층이라 watchdog이 **호스트 도달성**을 별도로 감시해야 한다. 본 구현으로 60s 내 자동 감지·복구가 가능해졌고, 후속으로 `checker.py` 타입 경고 및 watchdog ruff 경고(기능 영향 없음) 정리가 남았다.

## 6. 출처
- 코드: `scripts/lib/watchdog/{config,checker,recovery,orchestrator,__init__}.py`, `scripts/activity_summarizer.py`, `scripts/lib/handover_db.py`.
- 설정: `~/.config/containers/systemd/svc.pod`, `_disabled/container-flaresolverr.container.disabled`.
- DB: `handover.yaml` cp#104(decisions `svcpod-01`/`svcpod-02`), task#32, obs 5건.
- Deep Dive plan: `dp-20260912-watchdog-svcpod-portforwarding` (A안 8.60/10).
- rootless 포워딩: Podman 공식 문서 `podman-run.1` (rootless bridge = `rootlessport` proxy).
