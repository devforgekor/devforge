# DataImpulse 경로 감시 — 와치독 위임 계획 (Deep Dive, 재정의판)

> Status: implemented (dry-run, disabled by default) · 2026-09-23 · Deep Dive `dp-20260923-dataimpulse-monitoring-delegation`
> **구현**: `adapters/driven/health/dataimpulse_path.py`(읽기 전용·fails-open) + `ports/types.py:PathStatus` + `core/config.py`(WATCHDOG_DATAIMPULSE_*) + `watchdog_service` 등록(활성 시) + `strategies.py`(`dataimpulse:` alert-only) + `tests/unit/adapters/driven/health/test_dataimpulse_path.py`.
> **기본 비활성**(`WATCHDOG_DATAIMPULSE_ENABLED=0`), P2.6(단일 리더) 이후 활성. **신호 실측 주의**: 현재 `status.json.sources={}`(비어 있음) → top-level `phase/updated_at` 폴백으로 동작.
> **[한계] top-level 폴백의 의미**: 폴백 시 `active`는 "**pipeline loop가 살아 있음**"을 뜻하며 "toki31 수집 중"과 동일하지 않다(현재 `phase=loop`, `source=bookto31`). 따라서 loop만 살아 있고 toki31이 정체된 경우를 **놓칠 수 있다(false negative)**. §8-1(정상 vs 이상 판정) 확정 전에는 이 한계를 인지하고, 확정 후 `sources.toki31.phase=="collect"` 신호가 실제로 기록되도록 ebooklib 측 확인이 필요.
> **역할 경계(사용자 확정)**: 와치독은 **감지(detect)·기록(record)·알림(alert)만**. 트래픽 안전망·사용량 제어·비교는 **프로그램(ebooklib) 내부 책임**.
> 배치: `watchdog-standard-compliance.md` P2(호스트 유닛) 이후, P2.6(단일 리더) 이후 활성화.

---

## 0. 결정 요약 (이전 계획 대비 변경)

| 항목 | 이전(폐기) | **재정의(확정)** |
|------|-----------|------------------|
| 감시 대상 | DataImpulse **대시보드 사용량 vs 내부 추적 비교** | **DataImpulse 경로(toki31)가 시작/작동 중인가** |
| 트래픽 안전망 | 와치독이 확인 | **프로그램 내부**(`traffic_guard`) — 와치독 관여 안 함 |
| 대시보드 조회 | 와치독이 Playwright/HTTP로 조회 | **하지 않음** (프로그램 내부 `_log_comparison`이 담당) |
| 와치독 산출 | 사용량 델타·드리프트 경보 | **경로 활성/중단 감지 → 기록 + 알림** |
| Playwright | 신규 도입 검토 | **불필요** (읽기 신호만 사용) |

**근거**: 대시보드 비교는 이미 ebooklib `_log_comparison()`이 보유(As-Is §1). 와치독은 그 결과를 **중복 계산하지 않고**, "로직이 돌고 있는지"만 본다.

---

## 1. As-Is (검증된 사실)

### 1.1 toki31(DataImpulse 경로) 구조
- 수집기 별칭: `toki31 → _collect_newtoki` (`pipeline.py:466`).
- 유료 프록시 플래그: `traffic_limited=true` **오직 toki31** (`sources.json`, `sources.py:277-283`).
- 프록시: **DataImpulse 우선 → MaskProxy 폴백** (`toki31_playwright.py:20,51,249-299`).

### 1.2 실제 실행 프로세스 (실측 01:00 UTC)
| 프로세스 | PID | 출력 | 비고 |
|----------|-----|------|------|
| `pipeline.py collect --source toki31` | 2552550 | `collect_toki31.log` | **detached, session-2.scope** (ppid=1) |
| `pipeline.py collect --source bookto31` | 2552549 | (자체 로그) | 분리 실행 |
| `pipeline.py loop` (서비스) | 2639404 | **journal** | toki31 락 선점으로 `처리: 0` |

**핵심 함정**: toki31 작업은 **detached 프로세스**라 `ebook-watcher` journal에 **toki31/DataImpulse 로그가 0건**. 기존 `check_ebook_pipeline`(journal 기반)으로는 **감지 불가**.

### 1.3 관찰 가능한 권위 신호 (import 없이)
| 순위 | 신호 | 경로/명령 | 판정 |
|------|------|-----------|------|
| 1 | toki31 수집 로그 최신 활동 | `collect_toki31.log` mtime / 마지막 `source=toki31` | **"지금 작동"** |
| 2 | 상태 파일 구조화 신호 | `status.json` → `sources.toki31.phase=="collect"` + `updated_at` | **"지금 작동"(구조화)** |
| 3 | 프로세스 존재 | `pgrep -f "pipeline.py collect --source toki31"` | **"시작됨"** |
| 4 | 트래픽 누적 | `traffic_state.json` bytes/chapters delta | "트래픽 발생" (프록시 구분 불가) |
| — | journal `ebook-watcher` | toki31 마커 **0건** | ❌ 부적합 |
| — | DB | toki31 전용 테이블 없음 | ❌ 부적합 |

실측값(01:00:38): `status.json.sources.toki31.phase="collect"`, `updated_at=01:00:38`, `processed=127`, `remaining=98`; `traffic_state.json` bytes=25,184,546 / chapters=128.

### 1.4 기존 자산
- 억제/오탐 방지 선례: `check_ebook_pipeline`(journal hang, `checker.py:162-213`) — **패턴은 재사용, 신호원만 교체**.
- 상태 파일 이미 존재: `status.json`, `traffic_state.json`(ebooklib 소유, 읽기 전용).

---

## 2. 설계 (To-Be)

### 2.1 역할 (명확한 경계)
```
[ebooklib 내부]                          [와치독 (devforge)]
 traffic_guard: 한도/중단/재개            DataImpulsePathPort.get_status()
 _log_comparison: 대시보드 vs 추적          → PathStatus(active, last_seen, processed, phase)
 （= 트래픽 안전망, 와치독 무관）          → 비활성/정지 감지 시 incident 기록 + 알림
                                          → 변동(진행 정체·중단) 감지
```
- 와치독은 **읽기만**. 상태 파일/로그/프로세스를 신호로 사용. **Playwright·대시보드·traffic_guard import 없음**.
- **설계 원칙(표준)**: 와치독은 **읽기 전용·단순·무상태에 가깝게** 유지 — 감시 대상보다 단순해야 신뢰 가능(arc42 Watchdog Supervision). 브라우저/대시보드/PG 조회를 넣지 않는다. 복구·제어 권한 없음(OWASP Agentic A03: 감지/복구 역할 분리).

### 2.2 포트 (ports, 최하위 계층)
```python
@dataclass(frozen=True)
class PathStatus:
    name: str                 # "dataimpulse:toki31"
    active: bool              # 지금 작동 중인가
    last_seen: datetime | None
    processed: int | None
    phase: str | None         # status.json phase
    state: str = "unknown"    # "active"|"stalled"|"absent"|"unknown"
    detail: str = ""

class DataImpulsePathPort(Protocol):
    async def get_status(self) -> PathStatus: ...
```

### 2.3 어댑터 (adapters/driven/health/ 또는 신규)
- `DataImpulsePathHealthChecker` — **부작용 없는 읽기**:
  1. `status.json` → `sources.toki31` 파싱: `phase=="collect"`이고 `updated_at` 최신이면 active.
  2. 폴백: `collect_toki31.log` mtime/마지막 `source=toki31` 라인.
  3. 보조: `pgrep -f "pipeline.py collect --source toki31"` (시작 여부).
- **신호가 config로 주어짐**(하드코딩 금지): 경로·staleness 임계(기본 1800s, 수집 딜레이 300s의 6배).
- 시그니처: `HealthCheckPort.check_health()` 재사용(신규 포트 불필요) → `health_ports["dataimpulse"]` 등록.
- **`unknown` 처리(표준: fails-open)**: 신호 파일 부재/파싱 실패/스키마 불일치는 **추측하지 않는다**. `state="unknown"`으로 두고 **경보하지 않음**(기록만). "없는 파일은 문제가 아니라 부재" — 잘못된 확신(false confident) 방지.
  근거: `status.json` 스키마 버전 불일치 시 `unknown` 반환(pi-agi 선례), Sentinel File Pattern(fails-open).

### 2.4 감지·기록·알림 (와치독 본연 기능 재사용)
| 상황 | 와치독 반응 |
|------|-------------|
| 경로 **비활성/정지**(마지막 활동 > 임계, 프로세스 없음) | `watchdog_incidents`에 `dedup_key="dataimpulse:toki31:stalled"` 기록 + 알림 |
| 경로가 **한 번도 시작 안 됨**(toki31 큐/설정 기대되는데 신호 없음) | 동일 incident(dedup) — "오류 기록 → 추후 기능개선" 입력 |
| **unknown**(신호 부재/파싱 실패) | **경보 없음**, 관찰 기록만(fails-open) |
| 정상 작동 | healthy, incident resolve |
- **복구(recovery) 없음**: alert-only(전략 `_PREFIX`에 `dataimpulse:` → `""` 추가). 와치독이 재시작/제어하지 않음(프로그램 책임).

### 2.5 변경 감지(2계층: 얕은/깊은)
표준: *"shallow checks are safe to run often; deep checks should be rate-limited/cached"*(Karol Broda). "200 OK from a static handler is worse than nothing" — 존재 확인만으론 불충분.

| 계층 | 검사 | 주기 | 판정 |
|------|------|------|------|
| **얕은(Shallow)** | `status.json` 신선도 + 프로세스 존재 | 60s cycle | 비활성/부재 → 즉시 기록 |
| **깊은(Deep)** | `processed` **델타 정체**(active인데 진행 없음) | 별도 ≥300s | **연속 N회**(기본 3) 정체 → 경보 |

- 깊은 검사는 rate-limit: 매 cycle마다 하지 않고 별도 주기로(부하·오탐 억제).
- 현재 프레임워크엔 일반 baseline이 없으므로(`task 조사 §10`), **인메모리 직전값**만 유지(최소 구현). DB 영속은 후속.
- 단회 스파이크 아닌 **연속 N회**(기본 3)에서 경보 — 표준 드리프트 원칙.

---

## 3. 구현 단계

| Step | 내용 | 파일(승인 필요 표시) |
|------|------|---------------------|
| 1 | 신호 확정: `status.json.sources.toki31` + 로그 mtime 파싱 규칙 문서화 | (조사 노트) |
| 2 | `PathStatus` dataclass | `ports/types.py`(확장) |
| 3 | `DataImpulsePathHealthChecker` (읽기 전용) | **신규** `adapters/driven/health/dataimpulse_path.py` |
| 4 | config 필드(신호 경로·staleness·N연속) | `core/config.py`(확장) |
| 5 | `health_ports` 등록 + alert-only 전략(`dataimpulse:`→`""`) | `application/watchdog_service.py`, `domain/.../strategies.py`(확장) |
| 6 | 테스트(정상/정지/미시작/락선점) + 게이트 | `tests/unit/adapters/driven/health/` |

**신규 파일 1개**(어댑터). 나머지는 기존 파일 확장 → AGENTS.md §5(확장 우선) 준수.

---

## 4. 검증

| 게이트 | 방법 |
|--------|------|
| 부작용 없음 | 어댑터가 읽기/`pgrep`만 수행(단위 테스트로 검증) |
| 정상 감지 | status.json fixture(phase=collect, 최신 updated_at) → healthy |
| 정지 감지 | updated_at 노화 → unhealthy + incident dedup |
| 미시작 감지 | status.json 없음 + pgrep 없음 → unhealthy |
| 락 선점 오탐 없음 | detached 프로세스가 락 보유 중에도 **active로 판정**(journal 아님) |
| 계층 | `lint-imports` 4 KEPT (ebooklib import 0) |
| 회귀 | `pytest -x --tb=short`, `ruff`, `mypy` |
| 실증 | `journalctl -u devforge-watchdog-v2 | grep -i dataimpulse` |

---

## 5. 장애 폴백 계층 (L1–L4)

와치독이 감시를 못 하는 상황에서도 **수집기는 자기 일을 계속**한다(프로그램 내부 독립). 와치독은 감시만 하며, 제어·복구는 하지 않는다.

| 계층 | 트리거 | 동작 | 근거/비고 |
|------|--------|------|-----------|
| **L1** | 와치독 크래시/hang | systemd `WatchdogSec=180` 자동 재기동 | `watchdog-standard-compliance.md` P3(sd_notify) |
| **L2** | 와치독 감시 공백(신호 120s 정체) | **수집기는 영향 없음** — 프로그램 내부 체크(10화마다 `_check_dataimpulse_usage`, `pipeline.py:54,1325-1326`)는 원래 독립 동작(와치독 의존 0). 와치독만 감시 재개 | **env 불필요**. 신규 flag·헬스비트 파일 없음 |
| **L3** | 감시 이상 3회 연속 | Slack 알림(멱등, `watchdog_incidents` dedup) → 인간 개입 | 기존 `NotificationPort` 재사용, **연속 N회** 원칙 |
| **L4** | 완전 불가 | 트래픽 가드(로컬) 보호 지속 + `DATAIMPULSE_IP_WHITELIST` 전환 검토 | `traffic_guard`는 애초 로컬(`pipeline.py:1177`), `dataimpulse_monitor.py:96` |

**핵심 정합**:
- L2는 "재개"가 아니라 **"원래 독립"** 서술 — `WATCHDOG_DATAIMPULSE_FALLBACK` env는 **삭제**(불필요, 시크릿 아님·제어 대상 없음).
- 헬스비트는 **신규 파일 만들지 않고 기존 신호 재사용**(§2.3): `status.json.sources.toki31.updated_at`, `collect_toki31.log` mtime, `pgrep`.
- 트래픽 안전망은 폴백 대상이 **아님**(수집기 로컬, 와치독 비관여) — 명시.

> 롤백용 제어는 와치독 쪽 단일 flag **`WATCHDOG_DATAIMPULSE_ENABLED`**(기본 on)만 사용. 시크릿은 전부 KV에서 주입(코드/env에 비밀 없음).

---

## 6. 리스크 / 완화

| 리스크 | 완화 |
|--------|------|
| **공유 실패 도메인(호스트 SPOF)** | 와치독(호스트 유닛)과 수집기(ebooklib venv)가 **같은 호스트** → 호스트 다운 시 L1–L4가 **모두 붕괴**. 완전 독립이 아님을 **정직하게 명시**. 실현 가능 범위의 독립만 보장: 다른 프로세스·다른 런타임(py3.12 vs py3.11)·다른 신호원(파일 vs journal). 호스트 다운은 별도 외부 감시(dead-man's switch) 영역 |
| detached 프로세스라 journal에 없음 | journal 대신 `status.json`+`collect_toki31.log` 사용(§1.3) |
| 정상 정지(큐 소진, 한도 초과)를 장애로 오탐 | 한도 초과·큐 소진을 **정상 상태**로 판정(프로그램 내부 신호 참조) |
| 얕은 검사만으로 정체 못 잡음 | §2.5 **2계층**(얕은=신선도 60s / 깊은=processed 델타 ≥300s, N연속) |
| 신호 파싱 실패를 장애로 오탐 | `unknown`은 **경보 없음**, 기록만(fails-open) — §2.3 |
| 신호 경로 하드코딩 | config로 주입(`WATCHDOG_DATAIMPULSE_*`) |
| shadow 중 중복 알림 | P2.6 이후 활성화, dry-run 선행 |
| 프록시 식별 불가(DataImpulse vs MaskProxy) | 감시 범위는 **경로 활성 여부**로 한정(프록시 종류는 프로그램 책임) |
| 와치독 공백이 수집기를 방해 | L2 — 수집기는 원래 독립(와치독 의존 0), 신규 헬스비트/flag 없음 |
| 와치독이 감시 대상보다 복잡해짐 | §2.1 원칙: 읽기 전용·단순·무상태 우선(arc42) |

---

## 7. 성공 기준

- [ ] 와치독이 **읽기 신호만**으로 DataImpulse 경로 활성/정지/미시작을 판정(부작용 0)
- [ ] 정지·미시작 시 `watchdog_incidents`에 **기록** + 알림, **복구(recovery) 없음**(alert-only)
- [ ] **unknown은 경보 없이 기록만**(fails-open), 잘못된 확신 금지
- [ ] **2계층 검사**: 얕은(60s 신선도) + 깊은(processed 델타, ≥300s, N연속)
- [ ] detached/journal 함정 회피: `status.json`·`collect_toki31.log` 기반
- [ ] 오탐 억제: 한도 초과·큐 소진은 정상으로 분류, **연속 N회**에서만 경보
- [ ] 계층 준수(ebooklib import 0, lint-imports 4 KEPT), `pytest/ruff/mypy` green
- [ ] **트래픽 안전망·사용량 비교는 프로그램 내부 유지**(와치독 비관여)
- [ ] L1–L4 폴백 계층 문서화, **신규 env·헬스비트 파일 0**(기존 신호 재사용)
- [ ] **공유 실패 도메인 한계 명시**(호스트 SPOF, 완전 독립 아님)
- [ ] 와치독은 감시 대상보다 **단순**(읽기 전용, 브라우저/PG 조회 없음)

---

## 8. 미해결 / 선결

1. **정상 vs 이상 판정 정의**: "한도 초과로 자정 대기"(정상) / "로그인 실패·프록시 자격증명 없음"(이상) 구분 — `status.json`·로그 문구 기준 합의 필요.
2. `status.json` 포맷(ebooklib 소유) 변경 시 어댑터 파서 취약 → 파싱 실패는 **미판정(unknown)** 으로 두고 **경보 없이 기록만**(fails-open).
3. DB 영속(baseline) 도입 여부: 1차는 인메모리, 후속 검토.
4. 활성화 시점 = P2.6 이후(단일 리더). 그 전엔 dry-run 관찰만.

---

## 9. 관련 문서

- **오류 상세 기록 + 분석 로직(별도)**: `docs/plans/error-record-analysis-design.md`
  (L1 요약 / L2 `context_jsonb` / L3 `observations` 3계층 기록, 별도 오류 분석 로직의 decision packet·가드레일)
