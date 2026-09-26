# Watchdog 컷오버 세션 핸드오버 (2026-09-26)

> Status: record · Date: 2026-09-26 · Owner: devforge · Related: `plans/watchdog-cutover-execution-20260926.md`, `plans/watchdog-standard-compliance.md`, `refactoring/REFACTORING_STATUS.yaml`

> 실행 계획 정본은 `plans/watchdog-cutover-execution-20260926.md`다. 본 문서는 **다음 LLM이 이어받기 위한 세션 스냅샷**이며, 계획 변경 시 정본을 갱신한다.

## 0. 다음 LLM 첫 행동 (TL;DR)

1. `cat /opt/projects/server/logs/watchdog-shadow-final-parity.log` — P0 GATE PASS/FAIL 확인.
2. 무결성: `systemctl --user show devforge-watchdog-v2 -p NRestarts -p ExecMainStartTimestamp`
   → `NRestarts=0` & `ExecMainStart=2026-09-25 04:10:17 GMT` 이어야 창 유효.
3. **G1·G2 통과 + 사용자 승인**이면 `plans/watchdog-cutover-execution-20260926.md` §6 **P1** 실행(legacy stop 먼저).
4. FAIL/불명이면 컷오버 보류 → 원인 규명 후 새 24h 창.

## 1. 배경 (이 창이 왜 중요)

- watchdog v2.1(hexagonal) shadow 관측 창 = `2026-09-25T04:10:17Z` ~ `2026-09-26T04:10:17Z` (KST 13:10).
- v2 host unit `devforge-watchdog-v2.service`, `WATCHDOG_DRY_RUN=1`(복구 OFF). legacy `devforge-watchdog.service` 동시 active(오라클 + **실복구 수행**).
- 이 24h 대조가 **컷오버(P2.6) 증거**다. 창 중 v2·감시대상 재기동 = 창 리셋.

## 2. 이번 세션(2026-09-26) 완료 사항

1. 창 무결성·중간 패리티 확인(01:09Z, 21h 시점): `detection_gaps=0`·`legacy_only=0`, v2_only 3건 전부 alert_only.
   - `heartbeat:day_enrich`(1824s ≥ 1800s), `heartbeat:liveness_embed_batch`(1866s ≥ 1800s), `llm:day-extract`(HTTP 503).
2. 컷오버 재계획 + 충돌 분석(C1~C10). 사용자 결정: **즉시 컷오버 / A-B-C 연기 / Slack 배선만+문서**.
3. P0 게이트 자동화 등록(아래 §3).
4. 계획서·핸드오버 문서 작성, INDEX 등록, 테스트/린트 통과.

## 3. 자동 실행 (P0 게이트)

- 스크립트: `scripts/deploy/watchdog-shadow-final-parity.sh` (read-only, v2·legacy 재기동 안 함).
- 타이머: `devforge-watchdog-shadow-final-parity.timer` — `OnCalendar=2026-09-26 04:12:00 UTC`, `RemainAfterElapse=no`.
- 로그: `/opt/projects/server/logs/watchdog-shadow-final-parity.log` (끝에 `P0 GATE: PASS|FAIL`).
- **주의**: transient `systemd-run --user` 유닛 → **리부트 시 소실**(Linger=yes로 로그아웃은 무관).
  - 수동 재현: `python3 /opt/projects/server/scripts/watchdog_parity.py --since 2026-09-25T04:10:17Z --json`

## 4. 다음 단계 (요약)

| 단계 | 내용 | 게이트/수락 | 정본 |
|---|---|---|---|
| P0 | 무결성 + 최종 패리티 | G1·G2 | 계획 §6 P0 |
| P1 | **원자적 컷오버**: legacy stop → `WATCHDOG_DRY_RUN=0` + v2 restart | 단일 리더·첫 실복구·중복 0 | 계획 §6 P1 |
| P2 | 컷오버 직후 비재기동: `#494a` mcp-inventory OnFailure, `#495` 검색/메타/8080, C7·C10 정정 | — | 계획 §6 P2 |
| P3 | v2 유지보수 재기동: `#494b` 하드닝(**`/var/tmp` ReadWritePaths**) + §16-6 + error-record §2, F2 별도 | READY·liveness·오탐 0 | 계획 §6 P3 |
| P4 | A/B/C(F4), F3, PY-3.12 잔여 | — | 계획 §6 P4 |

## 5. 리스크 / 주의 (반드시 준수)

- **C1**: P0 이전 어떤 v2 재기동도 금지(창 리셋).
- **C2(중대)**: legacy가 실복구 중 → `dry_run=0`만 켜면 **이중 복구·중복 incident**. legacy stop **먼저**.
- **C5(실버그)**: §16-7 하드닝은 `ReadWritePaths=/opt/ai_data/scripts /var/tmp` 필수(없으면 liveness 오경보).
- **C10(함정)**: `sync-units.sh` **전체 실행 금지** — `devforge-openrouter-free-models.service` live에만 rerank/reasoning ExecStart 2줄이 있어 전체 sync 시 롤백됨. 미러 선정정 후 개별 cp.
- **C6**: Slack `account_inactive` → OnFailure/F2 경보 미전달(배선만, 문서화 상태).
- 롤백: P1 = `stop v2` + `dry_run=1` + restart + `start legacy`.

## 6. 산출물

- `plans/watchdog-cutover-execution-20260926.md` (컷오버 실행 계획, active, INDEX §2 등록)
- `scripts/deploy/watchdog-shadow-final-parity.sh` (P0 게이트 스크립트)
- `logs/watchdog-shadow-final-parity.log` (P0 결과)
- CLI task: `#493`(패리티+dry_run 판정), `#494`(watchdog/MCP 배선), `#495`(검색/메타/8080)

## 7. 진입 명령 모음

```bash
cd /opt/projects/server
python3 scripts/cli.py status --json        # 라이브 상태 우선
python3 scripts/cli.py task list            # #493~#495
cat logs/watchdog-shadow-final-parity.log   # P0 결과
systemctl --user show devforge-watchdog-v2 -p NRestarts -p ExecMainStartTimestamp
systemctl --user list-timers devforge-watchdog-shadow-final-parity.timer --all
python3 scripts/watchdog_parity.py --since 2026-09-25T04:10:17Z --json
```
