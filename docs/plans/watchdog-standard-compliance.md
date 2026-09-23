# Watchdog 표준 준수 계획 (Phase 2 Gate 4 개정판)

> Status: active — P1 완료(2026-09-23), P2~P4 대기(shadow-run 창 만료 후) · 2026-09-23 · Deep Dive `dp-20260923-watchdog-standard-compliance`
> 목적: Gate 4 컷오버를 **업계/업스트림 표준**에 맞게 재설계. 3개 비표준 요소 교정 + 신뢰성 보강.
> 대체: `_archive/plans/phase2-gate4-cutover-plan.md` §2.1–2.7 (본 계획이 정본). 코드 선행분은 `phase2-gate4-code-prereq.md` 참조.

---

## 0. 요약

| 단계 | 내용 | 근거(검증) |
|------|------|-----------|
| **P1** | rootless/rootful graphroot **분리** | Podman docs: 저장소 분리 원칙 |
| **P2** | watchdog **호스트 user 유닛** 전환 + postgres **loopback publish** | Red Hat/systemd/Datadog: 호스트 감시 표준 |
| **P3** | `Type=notify` + `WatchdogSec` + **sd_notify** | context7 `systemd.daemon.notify` / `sd_notify(3)` |
| **P4** | **immutable tag + retention** | Podman 이미지 수명주기 표준 |

각 단계는 **독립 검증/롤백** 가능하며, shadow-run 유효성을 유지한다.

---

## 1. 표준 위반 진단 (As-Is)

### 1.1 graphroot 부모 공유 (P1)
```
/opt/ai_data/containers/{storage(rootless, opc), root-storage(rootful, root)}
```
- Podman/Oracle 문서: **rootless와 rootful은 서로 다른 저장소**를 써야 권한·동시성이 성립. 현재는 부모를 공유해 rootless가 형제 `root-storage/overlay/backingFsBlockDev`(root 600)에 접근 → `permission denied`, `exit 126`.

### 1.2 컨테이너에서 호스트 감시 (P2)
- 컨테이너에 `systemctl`/`journalctl`/`pgrep`/`free` **부재** → svc/timer/oneshot/ebook/system 체크 전부 오탐.
- 표준: 호스트 자원 감시 에이전트는 **호스트 systemd 유닛**으로 실행 (Red Hat: *"host-focused: run agents under systemd"*).

### 1.3 DB 도달 불가 (P2)
- postgres가 pod 내부 `127.0.0.1`만 리슨, host publish 없음 → 호스트 감시자는 DB 접근 불가.
- 표준: **loopback 한정 publish**(`127.0.0.1:5432:5432`)가 보안 정석 (외부 인터페이스 미노출).

### 1.4 신뢰성·배포 (P3/P4)
- 컨테이너 quadlet은 conmon이 `Type=notify`를 제공하나 **`WatchdogSec` 미설정**(`WatchdogUSec=0`) → 행(hang) 감지 불가. 표준은 `Type=notify`+`WatchdogSec`+`sd_notify`.
- `:latest` 이동 태그 → dangling 누적(109개/82GB). 표준은 immutable tag + retention.

---

## 2. P1 — graphroot 격리

**절차 전문**: `docs/runbooks/rootless-graphroot-isolation.md` (A안) 실행.

요지:
```
mv /opt/ai_data/containers/storage /opt/ai_data/rootless-storage
~/.config/containers/storage.conf : graphroot = "/opt/ai_data/rootless-storage"
sudo restorecon -R /opt/ai_data/rootless-storage     # SELinux (문서 요구)
```

**검증**
```bash
podman info --format '{{.Store.GraphRoot}}'        # /opt/ai_data/rootless-storage
sudo podman info --format '{{.Store.GraphRoot}}'   # /opt/ai_data/containers/root-storage
diff /tmp/pre_containers.txt <(podman ps --format '{{.Names}}'|sort)   # 비어야
journalctl --user -u devforge-watchdog-v2 | grep -c "permission denied"  # 0
```

**롤백**: runbook §8 (경로·설정 원복 후 재기동).

> 근거: context7 `Podman` — rootless storage는 `$HOME/.config/containers/storage.conf`로 override, NFS 등 비지원 FS는 로컬 경로 지정. Oracle 공식: 표준 사용자/루트 저장소 분리.

---

## 3. P2 — 호스트 유닛 전환 + loopback publish

### 3.1 postgres loopback publish
`~/.config/containers/systemd/container-postgres.container`에 추가:
```ini
PublishPort=127.0.0.1:5432:5432
```
```bash
systemctl --user daemon-reload
systemctl --user restart container-postgres.service
ss -ltnp | grep 5432            # 127.0.0.1:5432 만, 0.0.0.0 아님
```
> 주의: pod 멤버는 publish를 **pod 생성 시** 지정해야 하는 경우가 있음. svc.pod에 이미 publish된 포트(8000/8002/8085)가 있으므로 pod 수준 publish로 처리 가능. 미동작 시 `svc-pod` 재생성 검토.
> 근거: context7 `Podman --publish` — hostIP 미지정 시 all-adresses, `127.0.0.1` 지정 시 loopback 한정.

### 3.2 watchdog 호스트 유닛 (이전 — 신규 추가 아님)
현재 신규 와치독(v2)은 **컨테이너 quadlet**(`~/.config/containers/systemd/devforge-watchdog-v2.container`)으로 shadow 실행 중이다. P2는 **동일한 이름의 서비스를 컨테이너→호스트 실행으로 이전**하는 것이며, **3번째 와치독을 만드는 것이 아니다**.
- 최종 서비스 수는 2개 유지: `devforge-watchdog`(legacy) + `devforge-watchdog-v2`(신규).
- 동일 이름에 정의 파일 두 종류가 공존할 수 없으므로, **quadlet `.container`를 제거하고 표준 `.service`로 대체**한다.
- shadow 비교가 끝나면 legacy를 중단(P2.6)하므로 운영 와치독은 최종 1개가 된다.

`~/.config/systemd/user/devforge-watchdog-v2.service` (신규 파일, 기존 quadlet 대체):
```ini
[Unit]
Description=DevForge Watchdog v2.1 (hexagonal, shadow)
After=container-postgres.service
Wants=container-postgres.service

[Service]
Type=notify
NotifyAccess=main
Environment=PYTHONPATH=/opt/projects/server/src:/opt/projects/server/scripts
Environment=WATCHDOG_DRY_RUN=1
Environment=WATCHDOG_STATE_FILE=/opt/ai_data/scripts/watchdog_state.v2.json
Environment=WATCHDOG_CHECK_INTERVAL_SEC=60
ExecStartPre=/opt/projects/server/scripts/deploy/kv-export-env.sh %t/kv-devforge-watchdog.env DEVFORGE-DATABASE-URL,DEVFORGE-POSTGRES-PASSWORD,SLACK-BOT-TOKEN-KEY,SLACK-CHANNEL
EnvironmentFile=%t/kv-devforge-watchdog.env
ExecStart=/usr/bin/python3.12 -m devforge.cli watchdog serve
ExecStopPost=/bin/rm -f %t/kv-devforge-watchdog.env
WatchdogSec=180
Restart=on-watchdog
RestartSec=30
TimeoutStopSec=30

[Install]
WantedBy=default.target
```
- KV 주입: `ExecStartPre`(`kv-export-env.sh`)가 `%t/kv-devforge-watchdog.env` 생성 → `EnvironmentFile`. DSN 소스 = KV `DEVFORGE-DATABASE-URL`(2026-09-23 등록).
- **`_sd_notify`는 raw AF_UNIX 소켓 구현**(`cli_cmds/watchdog.py:34`) → **libsystemd/`systemd.daemon` 불필요**, python3.12에서 `Type=notify`/`WatchdogSec` 그대로 동작(실측 확인).
- `devforge`는 python3.12에 editable 설치 완료(`python-version-strategy.md`). 미설치 환경이면 `pip install --user -e .` 선행.

**P2 착수 체크리스트 (shadow-run 창 만료 후)**
- [ ] shadow-run 창 만료(≥2026-09-24 13:32 UTC) + 비교 유효
- [ ] `devforge-watchdog-v2.container` → `_disabled/` 이동(동일 이름 `.service` 충돌 방지)
- [ ] 위 `.service`를 미러(`systemd/user/`)에 두고 `sync-units.sh`로 배포
- [ ] `daemon-reload` → `container-devforge-watchdog-v2.service` stop → `devforge-watchdog-v2.service` start
- [ ] `systemctl --user show devforge-watchdog-v2 -p Type -p WatchdogUSec` (=notify, 3min)
- [ ] 오탐 0(svc/timer/oneshot/ebook/system) + host→DB `select 1`
- [ ] legacy 중단(P2.6) — shadow 비교 종료 후
- P2 실행 시 기존 컨테이너 quadlet `devforge-watchdog-v2.container`를 **제거**(동일 이름 `.service`로 대체) — 이름 충돌 방지. **현재(2026-09-23)는 컨테이너 quadlet으로 shadow 실행 중이며 P2 미실행.**
- `PYTHONPATH`에 `scripts` 포함: health adapter가 `lib.watchdog.config` 등 레거시 설정을 import할 수 있음(패리티 테스트 기준).

**검증**
```bash
systemctl --user is-active devforge-watchdog-v2.service
journalctl --user -u devforge-watchdog-v2 -n 40 | grep -cE "No such file|mount not found"   # 0
journalctl --user -u devforge-watchdog-v2 | grep -E "svc:|timer:|ebook:" | tail   # 유효 결과
PGPASSWORD=... psql -h 127.0.0.1 -U postgres -d devforge_app -c 'select 1'          # host 도달
```

> 근거: Red Hat *"Podman is fork/exec; systemd has an easy time monitoring"*, TeckSite 2026 *"host-level recovery: run critical agents under systemd with WatchdogSec"*, Datadog *"host monitoring = host agent"*.

---

## 4. P3 — sd_notify + Type=notify + WatchdogSec

### 4.1 코드 (선행 필요)
`src/devforge/adapters/driving/cli_cmds/watchdog.py`에 sd_notify 추가:
```python
# [WHY] Type=notify + WatchdogSec 표준 — systemd가 행(hang)을 감지해 재시작.
def _sd_notify(state: str) -> None:
    sock_path = os.environ.get("NOTIFY_SOCKET", "")
    if not sock_path:
        return
    try:
        import socket
        addr = "\0" + sock_path[1:] if sock_path.startswith("@") else sock_path
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(state.encode())
    except OSError as e:
        import logging
        logging.getLogger(__name__).warning("sd_notify failed: %s", e)

async def _serve_loop() -> None:
    ...
    _sd_notify("READY=1")
    while True:
        await svc.run_cycle()
        _write_liveness()
        _sd_notify("WATCHDOG=1")
        await asyncio.sleep(interval)
```
- 외부 의존성 없이 **소켓 프로토콜 직접 구현**(context7: *프로토콜은 libsystemd 없이 재구현 가능*, 안정 인터페이스).

### 4.2 유닛
P2(호스트 유닛) 전환 후에만 유효하다. **현행 컨테이너 quadlet에는 `WatchdogSec`을 설정하지 않는다** — `--sdnotify=conmon` 모드에서 컨테이너 PID1에 `NOTIFY_SOCKET`이 전달되지 않아(`/proc/1/environ` 확인, 2026-09-23) 코드의 `_sd_notify("WATCHDOG=1")`가 no-op이고, `WatchdogSec`을 켜면 WATCHDOG 미수신으로 **오탐 재시작**이 발생한다.
- 현행 행(hang) 감지는 **`devforge-watchdog-liveness.timer`**(5분 dead-man's switch, active)가 담당한다.
- P2로 호스트 유닛(`~/.config/systemd/user/devforge-watchdog-v2.service`) 전환 시 `Type=notify`/`WatchdogSec=180`/`Restart=on-watchdog`가 실제로 동작한다(ping 60s ≤ WatchdogSec/3).

**검증**
```bash
systemctl --user show devforge-watchdog-v2 -p Type -p WatchdogUSec   # notify / 0 (현행, 의도적)
systemctl --user is-active devforge-watchdog-liveness.timer          # active (행 감지 대체)
# P2 전환 후:
# systemctl --user show devforge-watchdog-v2 -p Type -p WatchdogUSec # notify / 3min
# kill -STOP $(systemctl --user show -p MainPID --value devforge-watchdog-v2); sleep 200
# systemctl --user is-active devforge-watchdog-v2                    # 재시작 확인(timeout kill)
```

> 근거: context7 `systemd.daemon.notify`(`READY=1`, `WATCHDOG=1`, `STATUS=`), systemd `sd_notify(3)` 안정 인터페이스.

---

## 5. P4 — immutable tag + retention

- 앱 이미지: `localhost/devforge-fastapi:<YYYYMMDD>-<gitshort>` + `latest` alias. llama: digest pin 또는 날짜 태그.
- 배포 후 old 제거, 골든 재빌드 직후 prune.
- retention: 태그 N개(예: 최근 5) 유지, `podman-prune.timer`(48h dangling)와 정합.
- 검증: `podman images -f dangling=true | wc -l` 추세 0 근접, `/opt/ai_data` 여유 유지.

> 근거: Podman 이미지 수명주기 표준(immutable tag), 본 세션 dangling 분석(109개/82GB, 고유 레이어 ~50GB).

---

## 6. 단계별 검증 게이트

- [x] P1: graphroot 부모 분리 + 인벤토리 diff 0 + `permission denied` 0 — **완료 2026-09-23** (rootless→`/opt/ai_data/rootless-storage`; runbook §4.5 DB 마이그레이션 포함)
- [ ] P2: 호스트 유닛에서 svc/timer/oneshot/ebook/system 오탐 0, host→DB `select 1` 성공
- [ ] P3: `Type=notify`/`WatchdogSec` 동작 + 강제 hang 시 자동 재시작 — **현행은 `WatchdogSec` 미적용(§4.2), liveness 타이머가 대체; P2 전환 후 활성**
- [ ] P4: immutable tag + retention 문서화, dangling 0 근접
- [ ] 전 단계: `pytest -x --tb=short`, `ruff check src/devforge`, `mypy src/devforge`, `lint-imports` (4 KEPT) green
- [ ] shadow-run 24h 유효(비교 대상 유효)

> **P2 착수 게이트(shadow-run)**: graphroot 이동으로 watchdog v2가 재기동되어 Phase 2.5 창이 리셋됨 →
> **현재 창 = 2026-09-23 13:32 ~ 09-24 13:32 UTC**. P2(및 컷오버)는 이 창 만료 후 착수한다(조기 전환 시 비교 무효).
> 착수 전 §3 P2 절차의 `devforge-watchdog-v2.container` 제거 → 동일 이름 호스트 유닛 대체를 재확인.

---

## 7. 롤백

| 단계 | 롤백 |
|------|------|
| P1 | runbook §8 (경로/설정 원복, 재기동) |
| P2 | `systemctl --user start` 컨테이너 quadlet 복귀 + postgres publish 제거(재기동) |
| P3 | 유닛 `Type=simple`로 되돌리고 재기동 |
| P4 | 이전 태그로 `podman run` 롤백(immutable tag 보존) |

---

## 8. 표준 근거 대조표

| 결정 | 표준 근거 | 출처 |
|------|-----------|------|
| rootless/rootful graphroot 분리 | 저장소 분리 원칙 | Oracle Podman Storage, context7 Podman |
| 호스트 유닛 감시 | host-focused systemd agent | Red Hat, TeckSite 2026, Datadog |
| loopback publish | `-p 127.0.0.1:...` 보안 형태 | context7 Podman `--publish` |
| `Type=notify`+sd_notify | 안정 인터페이스 | context7 `systemd.daemon.notify`, sd_notify(3) |
| immutable tag | 이미지 수명주기 | Podman storage, 본 세션 분석 |

**기각**: 컨테이너 유지 + `nsenter`/`--privileged`/host mount = 안티패턴(격리 상실, 공격면 확대, 취약).

---

## 9. 미해결/후속

- `svc.pod` publish를 pod 수준 vs 컨테이너 수준 중 어느 것으로 할지 (기존 8000/8002/8085 방식과 통일).
- 호스트 유닛의 legacy 동시 실행 시 중복 복구 방지: shadow는 `WATCHDOG_DRY_RUN=1` 유지, 24h 비교 후 P2.6(복구 ON).
- `ebook-watcher` 등 사용자 유닛 3.12 이관(별건, `python-version-strategy.md`).

### 9.1 P2.3 후보 — DataImpulse 사용량 주기 검증 (검증 필요, 미채택)

**제안 요지**: 와치독 호스트 유닛(P2)이 주기적으로 DataImpulse 대시보드 사용량과 내부 추적값을 비교해 불일치를 조기 감지.

| 항목 | 제안 | 검증 결과(2026-09-23) |
|------|------|----------------------|
| import | `from lib.dataimpulse_monitor import ...` | ❌ devforge에 없음 — 실체는 `/opt/workspace/minihome/apps/ebooklib/apps/backend/lib/` (ebooklib 소유) |
| 단일 실행 락 | "내부 파일 락 보장" | ❌ `dataimpulse_monitor.py`에 flock 없음(검색 0). 락은 `traffic_guard._state_lock`만 존재 |
| 실행 비용 | CHECK_INTERVAL(60s) | ⚠️ `check_dataimpulse_sync()`가 Playwright chromium을 **매번 launch**(timeout 60s) → 60s 주기 비현실적, ≥300s 필요 |
| 비교 기준 | traffic_guard.summary() vs dashboard | ⚠️ summary=오늘/로컬(bytes·chapters), dashboard=월 누적(GB) → **단위·기간 불일치**로 오탐 |
| 리더 단일성 | "와치독 호스트 단일 리더" | ⚠️ shadow 기간엔 legacy+v2 동시 실행(P2.6 이후 단일) → 그 전 중복 접속 가능 |

**경계 문제**: DataImpulse/traffic_guard 모두 **ebooklib 도메인** 자산. devforge 와치독이 직접 import하면 bounded context 위반(AGENTS.md §5).
**현황**: 대시보드-vs-추적 비교 로직은 이미 `dataimpulse_monitor._log_comparison()` 내부에 존재 — 소유 도메인 안에 두는 것이 경계상 타당.

**채택 조건(권고)**:
1. devforge측은 **`DataImpulseUsagePort`**(ports) + 어댑터로 분리, ebooklib 모듈 직접 import 금지 (승인 필요 신규 파일).
2. 락은 **`/opt/ai_data/scripts/dataimpulse.monitor.lock` 신설** 후 그 락을 명시적으로 획득(기존 락 오해 금지).
3. 비교 기준을 **동일 기간·단위로 정규화**(예: 월 누적 GB vs 월 누적 GB) 후 임계값(±50%) 재산정.
4. 실행 주기 별도 env `WATCHDOG_DATAIMPULSE_INTERVAL_SEC`(기본 300s 이상), cycle과 분리.
5. P2.6(legacy 중단) 이후 활성화 → 단일 리더 보장.

> 상태: **미채택(설계 검증만)**. P2.3으로 확정하려면 위 5개 조건 충족 + 사용자 승인 필요.

---

## 9.2 P2.3 폴백 계획 — **DataImpulse 10화 대시보드 체크** 중앙화 시 실패/무응답

와치독이 **10화마다 DataImpulse 대시보드 접속·비교**를 중앙화할 때, 수집기(ebooklib)의 기존 로직(`_check_dataimpulse_usage()` 10화마다 호출)을 와치독으로 이관하는 경우의 **폴백 계층**:

| 계층 | 트리거 | 폴백 동작 | 복구 조건 |
|------|--------|-----------|-----------|
| **L1: systemd 자동 재시작** | 와치독 프로세스 크래시 / hang(>WatchdogSec) | `Type=notify` + `WatchdogSec=180` + `Restart=on-watchdog` → systemd 즉시 재기동 | 재기동 후 `READY=1` + 정상 `WATCHDOG=1` 3회 연속 |
| **L2: 헬스비트 timeout → 수집기 폴백** | 와치독이 `/opt/ai_data/scripts/watchdog_dataimpulse_heartbeat.ts` 갱신 중단 ≥ 120s(2주기) | 수집기(ebooklib) 내장 로직 `_check_dataimpulse_usage()`가 **10화마다 자동 재개** — 환경변수 `WATCHDOG_DATAIMPULSE_FALLBACK=1` 설정 시 강제 활성화 | 와치독 헬스비트 재개 감지(age < 60s) → 수집기 폴백 자동 비활성화 |
| **L3: 수집기 폴백도 실패(파싱 오류 등) 3회 연속** | 수집기 내장 대시보드 접속/파싱 연속 3회 실패 | Slack 알림 `"DataImpulse 대시보드 확인 중단: 수집기 폴백도 실패, 수동 확인 요망"` (인시던트 멱등) | 운영자 수동 확인 후 와치독/수집기 재기동 |
| **L4: 수동 비상 모드** | 완전 불가 | 1. `traffic_guard` 로컬 한도(일 200MB)로만 안전 운용 2. `DATAIMPULSE_IP_WHITELIST=1` 전환 검토 3. 다음 배포 시 모니터링 재설계 | 근본 원인 해결 후 단계적 복구 |

### 구현 포인트

1. **헬스비트 파일**: 와치독이 DataImpulse 체크 성공 시 `/opt/ai_data/scripts/watchdog_dataimpulse_heartbeat.ts`에 `time.time()` 기록(원자적 write). 수집기 시작 시·10화마다 `age = now - mtime` 확인.
2. **폴백 플래그**: `WATCHDOG_DATAIMPULSE_FALLBACK` 환경변수 — 수집기 시작 시 와치독 헬스비트 age > 120s면 자동 `export WATCHDOG_DATAIMPULSE_FALLBACK=1` 후 10화마다 `_check_dataimpulse_usage()` 실행.
3. **단일 실행 락**: 와치독·수집기 모두 `/opt/ai_data/scripts/dataimpulse.monitor.lock`(`fcntl.flock` + `LOCK_NB`)로 보호 — 동시 접속 방지.
4. **상태 공유**: `traffic_state.json`(이미 공유) + 헬스비트 파일만 사용. DB/ebooklib import 금지(Port/Adapter).
5. **알림 멱등성**: 인시던트 ID 기반(`lib.watchdog.incidents` 재사용) — 동일 사유 중복 알림 차단.

### 검증 시나리오

| 시나리오 | 예상 경로 | 통과 기준 |
|----------|-----------|-----------|
| 와치독 OOM kill | L1 → 30초 내 재기동 | `READY=1` + `WATCHDOG=1` 3회 |
| 와치독 수동 stop | L1 재기동 실패 → L2(120s 후) | 수집기 로그에 `DataImpulse 폴백 활성화` + 10화마다 대시보드 접속 |
| DataImpulse 대시보드 포맷 변경(파싱 실패) | L2(수집기 폴백) → 파싱 3회 실패 → L3 | Slack 알림 1회, 수집 지속 |
| 와치독 재기동 후 헬스비트 재개 | L2 → 자동 비활성화 | 수집기 로그에 `DataImpulse 폴백 비활성화` |

### 경계·책임 분리 (핵심)

| 주체 | 책임 | 비고 |
|------|------|------|
| **와치독(devforge)** | 주기적(≥300s) 대시보드 접속·비교 + 헬스비트 발행 + 알림 | ebooklib 모듈 직접 import 금지 |
| **수집기(ebooklib)** | 10화마다 폴백 대시보드 접속(헬스비트 timeout 시만) + `traffic_guard` 로컬 집계(항시) | 와치독 의존도 0 — 헬스비트 파일만 감시 |
| **공유 상태** | `traffic_state.json` + `watchdog_dataimpulse_heartbeat.ts` + `dataimpulse.monitor.lock` | 파일 기반만 |

> **트래픽 가드(`traffic_guard.add_bytes/is_exceeded`)는 원래부터 수집기 로컬**이므로 와치독 이관 대상이 아님 — L2에 명시해 둔 것은 "와치독이 없어도 트래픽 한도 보호는 계속됨"을 확인하기 위함.

