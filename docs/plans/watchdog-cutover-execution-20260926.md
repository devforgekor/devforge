# Watchdog v2 컷오버 실행 계획 (2026-09-26, 창 종료 후)

> Status: active · Date: 2026-09-26 · Owner: devforge · Related: `plans/watchdog-standard-compliance.md`, `plans/2026-standard-gap-remediation.md`, `plans/execution-plan-2026-09-24.md`, `plans/fitness-functions-heartbeat-drift-guide.md`, `refactoring/REFACTORING_STATUS.yaml`

## 1. 목적

watchdog v2.1(hexagonal) 24h shadow 관측 창 종료 직후, **legacy 중단(P2.6) + 복구 ON(`dry_run` 해제)** 컷오버를 충돌·오류 없이 수행하기 위한 실행 절차를 확정한다. 본 문서는 창 종료 후 작업의 **단일 실행 순서(SSOT)**이며, 충돌 분석과 사용자 결정(즉시 컷오버 / A-B-C 연기 / Slack 배선+문서)을 반영한다.

## 2. 배경 / 현황 (실측 2026-09-26T01:09Z)

- 창 = `2026-09-25T04:10:17Z` ~ `2026-09-26T04:10:17Z` (KST 13:10). v2 host unit `devforge-watchdog-v2.service`, `WATCHDOG_DRY_RUN=1`.
- legacy `devforge-watchdog.service` 동시 active — 비교 오라클이며 **실제 복구를 수행**한다.
- 무결성: v2 `NRestarts=0`, `ExecMainStart=2026-09-25 04:10:17 GMT`, active/running → 창 리셋 없음.
- 중간 패리티(01:09Z, 21h 시점): `detection_gaps=0`·`legacy_only=0`, v2_only 3건 전부 alert_only → 게이트 충족.
- dry_run 스위치: `src/devforge/cli.py:85` (`WATCHDOG_DRY_RUN=="1"`), systemd env로 제어.

## 3. 범위

### 3.1 포함
- P0 게이트(자동), P1 원자적 컷오버, P2 컷오버 직후 정리, P3 v2 유지보수 재기동, P4 잔여 위임.

### 3.2 비목표 (사용자 결정)
- **A/B/C(F4)** — 컷오버 후 별도 검증 배치로 연기 (`plans/detection-remediation-implementation-guide.md`).
- **Slack 재활성** — 배선만 수행하고 전달 불가(`account_inactive`)는 known_issue로 문서화.
- **legacy 코드 수정** — legacy는 P1에서 비활성 보존(롤백용)만, 개서하지 않음.

## 4. 최소 조건 (게이트)

| ID | 조건 | 확인 |
|---|---|---|
| G1 | v2 무결성: `NRestarts=0` & `ExecMainStart=2026-09-25 04:10:17 GMT` | `systemctl --user show` |
| G2 | 최종 패리티: `detection_gaps=0` & `legacy_only=0`(alert_only 허용), exit 0 | `scripts/watchdog_parity.py` |
| G3 | 사전 정합: open incident 스냅샷 + `watchdog_state.v2.json` 백업 | psql / cp |
| G4 | 롤백 준비: legacy·v2 유닛 원본 보관 | cp `.bak` |

## 5. 충돌·오류 분석 (해소 반영)

| # | 유형 | 내용 | 해소 |
|---|---|---|---|
| C1 | 순서 | v2 재기동 = 창 리셋. 패리티 전 재기동 시 증거 무효 | P0를 P1보다 먼저, P0~P1 사이 v2 동결 |
| C2 | 이중 리더 | legacy가 실복구 중. v2 `dry_run=0`만 켜면 **이중 복구·중복 incident** | P1에서 **legacy stop → v2 restart** 순서 강제 |
| C3 | 계획 모순 | `#493`(창 종료 직후 해제) vs `plans/execution-plan-2026-09-24.md`(컷오버 최후) | **즉시 컷오버**로 확정(사용자) |
| C4 | 관측 교란 | ebooklib·통합검색·8080·F2 대상유닛·PY이관이 감시대상 재기동 | 전부 P2/P3 이후(창 밖) |
| C5 | 가정오류(정정) | §16-7이 `/var/tmp` ReadWritePaths를 필수라 했으나 **실측 결과 `ProtectSystem=strict`에서 `/var/tmp`·`/run/user`·`/tmp`는 기본 쓰기 가능** | state 경로(`/opt/ai_data/scripts`)만 ReadWritePaths 필요 (§11 실측) |
| C6 | 알림 무효 | Slack `account_inactive` → OnFailure/F2 경보 미전달 | 배선만 + known_issue 문서화(사용자) |
| C7 | SSOT stale | known_issue 창=01:05:12Z, `refactoring/REFACTORING_STATUS.yaml` phase 2.5, `execution-plan §3.2` | P2에서 정정 |
| C8 | 배포 방식 | mcp-inventory는 v2 `oneshot_result_targets` → OnFailure는 daemon-reload만 | 재기동 없이 P2 |
| C9 | 자원 결합 | 8080 리랭커 ↔ 8082 day-extract 동일 추론 컨테이너 | `#495`를 P2(창 밖)로 |
| C10 | mirror drift | `devforge-openrouter-free-models.service` live에 rerank/reasoning ExecStart 2줄 추가, mirror 미반영 | **`sync-units.sh` 전체 실행 금지**(live 롤백됨) — 미러 선정정 후 개별 배포 |

## 6. 단계

### P0 — 게이트 (자동, 재기동 0) — 수락: G1+G2
- 자동 실행: `scripts/deploy/watchdog-shadow-final-parity.sh` (04:12:00 UTC 일회성 타이머).
- 산출: `logs/watchdog-shadow-final-parity.log` (P0 PASS/FAIL 라인 포함).
- 수동 재현:
```bash
systemctl --user show devforge-watchdog-v2 -p NRestarts -p ExecMainStartTimestamp
python3 scripts/watchdog_parity.py --since 2026-09-25T04:10:17Z --json
```
- FAIL 시: 컷오버 보류 → 원인 규명 후 새 창.

### P1 — 원자적 컷오버 (P2.6) — 수락: 단일 리더 · 첫 실복구 · 중복 0
1. 스냅샷 (G3/G4):
```bash
psql ... -c "select id,component,status from watchdog_incidents where status='open' order by id;"
cp /opt/ai_data/scripts/watchdog_state.v2.json /opt/ai_data/scripts/watchdog_state.v2.json.bak
cp ~/.config/systemd/user/devforge-watchdog-v2.service{,.bak}
```
2. 미러 유닛 수정: `systemd/user/devforge-watchdog-v2.service` → `WATCHDOG_DRY_RUN=0`.
3. 배포 (개별 cp — C10 때문에 전체 sync 금지):
```bash
cp -p systemd/user/devforge-watchdog-v2.service ~/.config/systemd/user/
systemctl --user daemon-reload
```
4. 순서 엄수 (C2):
```bash
systemctl --user stop devforge-watchdog.service
systemctl --user restart devforge-watchdog-v2.service
```
5. 검증:
```bash
systemctl --user is-active devforge-watchdog-v2     # active
journalctl --user -u devforge-watchdog-v2 -n 40 | grep "dry_run=False"
systemctl --user is-active devforge-watchdog        # inactive
cat /var/tmp/watchdog_last_cycle_ts                 # 갱신
psql ... -c "select id,component,status from watchdog_incidents where status='open';"   # 중복 0
```
6. 롤백: `stop v2` → 유닛 `WATCHDOG_DRY_RUN=1`+`daemon-reload`+`restart` → `start legacy`.

### P2 — 컷오버 직후 비재기동 (같은 날)
- `#494a` mcp-inventory 전용 OnFailure: 미러 수정 + `daemon-reload`(재기동 없음, C8). Slack 미전달 명시(C6).
- `#495` 검색/메타 배포 + 8080 경합: 관측창 없음(감시대상 재기동 정상, v2가 incident로 감지).
- C7 정정: known_issue `shadow-running-2026-09-25` 창 시각, `refactoring/REFACTORING_STATUS.yaml` phase(2.5→2.9), `plans/execution-plan-2026-09-24.md §3.2`(마이그레이션 적용 반영).
- C10: `systemd/user/devforge-openrouter-free-models.service` 미러 정정(rerank/reasoning 반영) 후 개별 배포.

### P3 — v2 유지보수 재기동 배치 (production watchdog, 공백 최소화)
- 1회 재기동 묶음: `#494b` 하드닝(**`ReadWritePaths=/opt/ai_data/scripts /var/tmp`**, C5) + §16-6 disk/SWAP(v2만) + error-record §2.
- F2 heartbeat는 별도 재기동(신규 경보) — alert_only로 grace 검증 (`plans/fitness-functions-heartbeat-drift-guide.md`).
- A/B/C 제외(비목표).
- 수락: 재기동 후 READY, liveness 파일 갱신, state write 오류 0, alert 오탐 0.
- 롤백: 직전 유닛/env 복귀 후 restart, 필요 시 legacy 재기동.

### P4 — 잔여 (컷오버 후)
- A/B/C(F4) 별도 배치, F3(drift/fitness), PY-3.12 이관 잔여 (`plans/python-version-strategy.md`), `refactoring/REFACTORING_STATUS.yaml` 단계 승격.

## 7. 롤백 요약

| 단계 | 롤백 |
|---|---|
| P1 | v2 stop + `dry_run=1` + restart, legacy start |
| P2 | OnFailure 유닛 제거 + daemon-reload; 검색/ebooklib 이전 설정 복귀 |
| P3 | 직전 유닛/env 복귀 + restart; 이상 시 legacy 재기동 |

## 8. 자동화

- 등록(완료): transient 일회성 타이머.
```bash
systemd-run --user --on-calendar="2026-09-26 04:12:00 UTC" \
  --unit=devforge-watchdog-shadow-final-parity -p Type=oneshot \
  /opt/projects/server/scripts/deploy/watchdog-shadow-final-parity.sh
```
- 특성: `RemainAfterElapse=no`(1회 실행), `Linger=yes`(로그아웃 후에도 실행). **transient이므로 리부트 시 소실** → 리부트 시 수동 재현 명령 사용.
- 산출 로그: `logs/watchdog-shadow-final-parity.log`.

## 9. 미결·승인

1. P1 컷오버 실행은 G1·G2 통과 + 사용자 승인 필요.
2. P3 신규 파일(F2 ping/registry, routing/catchup) 승인.
3. C10 미러 정정 방향(mirror를 live에 맞출지) 확인.
4. Slack 재활성 시점(보류) — 재개 시 `#494a`/F2 경보 실전 검증.

## 10. 근거

- `plans/watchdog-standard-compliance.md` §3.1·§3.2·§9 (P2/P2.6 단일 리더)
- `plans/2026-standard-gap-remediation.md` §16-6·§16-7 (하드닝·disk/SWAP·OnFailure)
- `plans/execution-plan-2026-09-24.md` §3 (창 이후 시퀀스)
- 코드: `src/devforge/cli.py:85`, `adapters/driving/cli_cmds/watchdog.py:23`, `application/watchdog_service.py:46`
- CLI task: `#493`·`#494`·`#495`

## 11. 실행 이력

- **2026-09-26T04:12:00Z — P0 자동 실행: PASS.**
  무결성 `NRestarts=0`/`ExecMainStart=2026-09-25 04:10:17 GMT`; 패리티 `detection_gaps=0`·`legacy_only=0`(v2_only 3건 전부 alert_only). 로그 `logs/watchdog-shadow-final-parity.log`.
- **2026-09-26T05:06:31Z — P1 컷오버 실행 완료.**
  `stop devforge-watchdog.service` → v2 `WATCHDOG_DRY_RUN=0` + restart. 검증: `dry_run=False`, `watchdog_state.v2.json` 생성, liveness 갱신, open incident 0, `NRestarts=0`, journal 오류 0. legacy inactive(롤백용 유닛 보존), 유닛 백업 `~/.config/systemd/user/devforge-watchdog-v2.service.bak`.
- **2026-09-26T05:27:03Z — P3(부분) v2 하드닝 + §16-6(b) 적용.**
  v2 유닛: `NoNewPrivileges=yes` + `ProtectSystem=strict` + `ReadWritePaths=/opt/ai_data/scripts`, 재기동. 실측: `strict`에서도 `/var/tmp`·`/run/user`·`/tmp`는 쓰기 가능 → **C5의 "/var/tmp ReadWritePaths 필수"는 오가정으로 정정**(state 경로만 필요). 검증: `dry_run=False`, state/liveness/token cache 정상, journal 오류 0, incident 0.
  §16-6(b): `SWAP_CRIT_MB` 9000(총량 4095 초과=도달 불가)→**3500**, `SWAP_WARN_MB` 6000→**2500** (legacy `lib/watchdog/config.py` + v2 `system_health.py` 동일, parity test green). §16-6(a) disk: v2는 alert-only 90% 유지(legacy DISK_WARN/CRIT 85/92는 미사용) — 의도된 v2 동작으로 문서화.
- **미실행(대기)**: error-record §2(분석 계층 **미구현** — 설계만, 코드 선행 필요), F2 heartbeat(신규 파일·설계 필요), P4(A/B/C/F3/PY-3.12).
