# 에이전트 핸드오버 DB 설계 (Deep Dive)

> Status: proposed · 작성 2026-09-22 · 근거: Deep Dive(dp-20260922-agent-handover-db-design)
> 목적: 외부 "AI-optimized PostgreSQL agent-handover" 제안을 **현재 서버 구성/로직에 맞게 적응**한 설계.
> 배치: **리팩터링 로드맵(Phase 2 Gate 4 이후)** — 지금 즉시 적용 아님.

---

## 0. 결정 요약

- **채택 = 하이브리드(C안)**: 기존 4개 핸드오버 테이블을 폐기/중복 생성하지 않고 **hex-arch로 승격** + AI 친화 컬럼 추가.
- **기각**: A안(제자리 컬럼 추가만)은 근본 원인 미해결, B안(신규 `agent_sessions`/`agent_events`)은 진실 원천 중복 + 프로덕션 리스크 + AGENTS.md §5 위반.
- **외부 제안 채택 요소**: `state` jsonb + `schema_version`(Pydantic), 낙관적 락(`version`), 리스(`lease_owner`/`lease_expires_at`), `COMMENT ON COLUMN`, "raw SQL 금지" 규칙.
- **외부 제안 기각 요소**: psycopg3(sync), 전역 pool raw SQL, `.github/copilot-instructions.md`.

---

## 1. 배경

외부 제안은 `agent_sessions`/`agent_events` 신규 테이블 + psycopg3 + 전역 pool + copilot 지침 파일을 전제로 한다. 현재 서버와 4가지가 충돌한다.

| 항목 | 현재 서버 | 외부 제안 |
|------|-----------|-----------|
| DB 스택 | SQLAlchemy 2.0 + asyncpg (async) | psycopg3 + `psycopg_pool` (sync) |
| 아키텍처 | DDD/hexagonal, ports/adapters, import-linter 4 contracts | raw SQL + 전역 pool |
| 규칙 파일 | `AGENTS.md` 단일 (Option B, copilot 파일 금지) | `.github/copilot-instructions.md` 생성 |
| 핸드오버 | `session_checkpoints`(+3) 이미 존재 | 신규 `agent_sessions` |

→ 그대로 적용하면 스택 이중화 + 계층 위반 + 규칙 충돌 + 진실 원천 중복이 발생한다.

---

## 2. 현재 상태 (As-Is)

### 2.1 데이터 흐름

```
SessionEnd hook / 수동
  └─ scripts/update_handover.py            (스캐너 + 오케스트레이터)
       ├─ handover.yaml 쓰기  ← fcntl.flock (YAML만 보호)  update_handover.py:264-266
       └─ scripts/lib/handover_db.py
            ├─ session_checkpoints INSERT   handover_db.py:59-74
            ├─ decisions / known_issues / completed_log INSERT
            └─ regenerate_handover_yaml()   handover_db.py:162-215  (락 없음)
```

- 진실 원천 = **DB**(`handover_db.py:162-163`), `handover.yaml` = LLM 읽기용 평면 백업.
- DB 접근은 ORM이 아니라 `podman exec postgres psql` + **f-string SQL**(`lib/db.py` 경유).

### 2.2 스키마 (`docs/specs/handover-schema.sql:4-43`)

| 테이블 | 핵심 컬럼 | 현재 사용 |
|--------|-----------|-----------|
| `session_checkpoints` | `id IDENTITY`, `summary`, `total_files`, `recent_files jsonb`, `git_state jsonb`, `task`, `content_hash`, `replaced_by` | `git_state`/`content_hash`/`replaced_by` **미기록** |
| `decisions` | `decision_id`, `detail`, `status`, `archived_at`, `decision_text` | `archived_at` 미기록 |
| `known_issues` | `issue_id`, `detail`, `resolved`, `resolved_at`, `issue_text` | `resolved_at` 미기록 |
| `completed_log` | `log_text` | `log_text` UNIQUE 없음(앱에서 dedup) |

- 라이브 행수: `session_checkpoints=35`, `decisions=112`, `known_issues=63`, `completed_log=103` (2026-09-22).

### 2.3 문제점 (설계 동기)

1. **계층 위반**: 핸드오버는 `src/devforge` **밖**(`grep handover src/` = 0건). ORM/Alembic/import-linter 미적용.
2. **스키마 드리프트**: `git_state`는 `update_handover.py:222,245`에서 계산되나 `_insert_checkpoint`(`handover_db.py:65`)가 **버림**. `content_hash`/`replaced_by`/`archived_at`/`resolved_at` 영구 미기록.
3. **동시성 없음**: `flock`은 YAML만 보호(`update_handover.py:265`), DB 쓰기(`:269`)와 YAML 재생성(`handover_db.py:212`)은 무보호. `action_queue.py:61-114` claim은 docstring과 달리 `FOR UPDATE`/`SKIP LOCKED` 없음(중복 실행 가능).
4. **문서 불일치**: `update_handover.py:3`·`CLAUDE.yaml:139`는 `handover-gen.timer`를 명시하나 **해당 유닛 없음**(라이브는 SessionEnd hook 경로). **(정정 2026-09-24)**: `update_handover.py` 헤더/docstring·`CLAUDE.yaml`을 **manual·타이머 없음**으로 갱신(SessionEnd hook은 `slack_notify.py`).
5. **SQL 주입 표면**: 전 경로 f-string + `esc_sql`(바인드 파라미터 없음).
6. **미등록 테이블**: `tasks`(세션 컨텍스트 핵심), `watchdog_pulses`, `pipeline_*`도 `models.py`/`schema.sql` 밖.

### 2.4 재사용 가능한 자산

- `DatabaseGateway`(`adapters/driven/storage/database_gateway.py:25-105`): async 엔진/세션, `NullPool`, `normalize_async_dsn`.
- `FOR UPDATE SKIP LOCKED` 선례: `pipelines/extract.py:286-308`, `enrich.py:680-704`, `raw_consumer.py:26-43`.
- `protection.py`(파일 기반 dead-man's switch), `action_queue`(watchdog 실행) — 중복 실행 방지 접점.
- import-linter 4 contracts(`pyproject.toml:194-236`): layers / hexagonal / domain-subpackage-independence / domain-agnostic-of-adapters.
- Alembic: `alembic/versions/20260913_initial.py`, `20260914_fix_initial_schema.py`; `models.py:3-4` = 스키마 SSOT, 마이그레이션은 모델에서 생성.

---

## 3. 요구사항

**기능**
- R1 세션 컨텍스트(체크포인트·결정·이슈·완료로그) 저장/조회.
- R2 `handover.yaml` 재생성(현행 의미 보존).
- R3 `state`를 Pydantic 스키마 + `schema_version`으로 검증(환각/드리프트 방지).

**비기능**
- R4 단일 진실 원천(테이블 중복 금지).
- R5 계층 준수(ports < domain < adapters < application, 4 contracts KEPT).
- R6 동시성 안전(중복 실행/갱신 유실 방지).
- R7 프로덕션 무중단(현행 핸드오버 경로 보존).
- R8 규칙은 `AGENTS.md`에만.

---

## 4. 설계 (To-Be)

### 4.1 원칙
- **스택 고정**: SQLAlchemy 2.0 + asyncpg 유지. psycopg3 도입 금지.
- **확장 우선**(AGENTS.md §5): 신규 테이블 대신 기존 4개 테이블 확장.
- **경계 분리**: 스키마 = ORM SSOT, 접근 = 포트/어댑터, 산출 = read projection.

### 4.2 테이블 변경 (additive-only)

```sql
ALTER TABLE session_checkpoints
  ADD COLUMN state           JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN schema_version  INTEGER NOT NULL DEFAULT 1,
  ADD COLUMN version         INTEGER NOT NULL DEFAULT 1,
  ADD COLUMN lease_owner     TEXT,
  ADD COLUMN lease_expires_at TIMESTAMPTZ;

COMMENT ON COLUMN session_checkpoints.state IS
  'Pydantic HandoverState (schema_version 포함). AI 읽기용 구조화 컨텍스트.';
COMMENT ON COLUMN session_checkpoints.version IS
  '낙관적 락. UPDATE ... WHERE id=? AND version=? 형태로만 증가.';
COMMENT ON COLUMN session_checkpoints.lease_owner IS
  '핸드오버 리스 보유자(session id). lease_expires_at 경과 시 탈취 가능.';
```

- 기존 컬럼 **의미 복구**: `git_state`, `content_hash`, `replaced_by`를 실제 기록.
- `decisions.archived_at`, `known_issues.resolved_at`도 상태 전이 시 기록.
- `docs/specs/handover-schema.sql`를 함께 갱신(SSOT 정합).

### 4.3 `state` 스키마 (Pydantic)

`ports/types.py`(최하위 계층, `types.py:6-8` 주석 근거)에 배치해 계층 역전 방지.

```python
class HandoverState(BaseModel):
    schema_version: int = 1          # 마이그레이션/검증 기준
    summary: str = ""
    recent_files: dict[str, list[str]] = {}   # 카테고리 → 파일
    git: dict[str, Any] = {}         # branch/shortstat/untracked
    task: str | None = None
    open_issues: list[str] = []
    decisions: list[dict[str, Any]] = []
```

- 읽기 시 `schema_version`이 낮으면 경고(또는 변환), 높으면 거부 → AI 환각 방지.

### 4.4 포트 / 어댑터

```
ports/handover.py          HandoverRepository(Protocol)
adapters/driven/storage/handover_pg.py   HandoverPgAdapter(SQLAlchemy/asyncpg)
application/handover_service.py          오케스트레이션(체크포인트 저장 + YAML 재생성)
```

- 포트 메서드: `acquire_lease`, `save_checkpoint(state)`, `release_lease`, `load_latest`, `list_open_issues`, `regenerate_projection`.
- 어댑터는 `DatabaseGateway`(세션/엔진) 재사용, **바인드 파라미터**로 SQL 주입 제거.
- 포트는 watchdog 계열과 동일하게 `Protocol` 채택(`ports/heartbeat.py`, `incident_repository.py` 선례).

### 4.5 낙관적 락 + 리스 (+ 펜싱 토큰)

- **낙관적 락**: SQLAlchemy 내장 `version_id_col` 사용 권장(context7 검증). `__mapper_args__ = {"version_id_col": version}` → 불일치 시 `StaleDataError` 자동 발생. 수동 CAS(`WHERE version=:expected`)도 동일 의미.
- **리스**: 진입 시 `UPDATE ... SET lease_owner=:sid, lease_expires_at=now()+interval '5 min' WHERE id=:id AND (lease_owner IS NULL OR lease_expires_at < now()) RETURNING id`. 미획득 시 종료(중복 실행 방지).
- **펜싱 토큰(필수)**: exa 검증 결과 *리스만으로는 불충분* — 느린 워커가 리스 만료 후 깨어나 last-write-wins로 덮어쓴다. `version`을 펜싱 토큰으로 삼아 **커밋 시점에 현재 토큰과 일치할 때만 반영**(`AND version = :my_token`). 즉 `version`(펜싱) + `lease`(소유권)를 함께 써야 안전.
- **클레임**: 신규/동시 진입은 `SELECT ... FOR UPDATE SKIP LOCKED`(SQLAlchemy `with_for_update(skip_locked=True)`, context7 검증)로 정렬.
- **주의**: 리스는 `DatabaseGateway`(NullPool, 직결)에서만 신뢰. 트랜잭션 풀러(PgBouncer transaction mode)에서는 세션 스코프 락이 깨져 리더가 둘이 될 수 있음(exa 경고).
- 리스는 `protection.py`(dead-man's switch)와 역할 분담: 리스=DB 쓰기 상호배제, protection=추론 포트/프로세스 보호.
- `action_queue`의 비원자 claim도 동일 방식(`FOR UPDATE SKIP LOCKED`)으로 정렬(별도 후속).

### 4.6 호출 경로

- 신규 CLI: `python3 scripts/cli.py handover`(포트 경유). 기존 `task`/`status` 패턴(`cli.py:987-1104`) 준수.
- `update_handover.py`는 리팩터 컷오버까지 **브리지**로 유지하되, 내부 쓰기를 포트 호출로 전환(동작 보존).
- SessionEnd hook(`scripts/hooks/session_context.py`)은 CLI/서비스만 호출.

### 4.7 `handover.yaml` = read projection

- YAML은 **DB에서 파생**되는 읽기 전용 투영으로 격하(`regenerate_projection`).
- 쓰기 순서: DB 커밋 → YAML 재생성(단일 리스 하에). 현행 `flock`은 제거 가능(리스로 대체).

---

## 5. 대안 평가

| 대안 | Feasibility | Completeness | Coherence | Risk(낮을수록↑) | 점수 | 판정 |
|------|:---:|:---:|:---:|:---:|:---:|------|
| A. 제자리 컬럼 추가(raw SQL 유지) | 9 | 4 | 5 | 2 | 6.55 | refine |
| B. 신규 `agent_sessions`(제안 원안) | 4 | 8 | 7 | 8 | 5.35 | abandon |
| **C. 하이브리드 승격 + AI 컬럼** | **7** | **9** | **9** | **5** | **7.60** | **pursue** |

- A는 데이터 갭만 메우고 계층/동시성/주입 문제를 남긴다.
- B는 최상 스키마지만 진실 원천을 둘로 쪼개고 라이브 경로를 위협한다.
- C는 단일 원천 + 제안의 장점 + 근본 원인 동시 해결. 리스크는 단계화로 통제.

---

## 6. 마이그레이션 단계

| Step | 내용 | 검증 |
|------|------|------|
| 1 | **특성화 테스트**: 현행 DB 행 + `handover.yaml` 산출 고정 | `pytest -x` |
| 2 | ORM 모델 4종을 `domain/models.py`에 추가 + Alembic(additive) + `COMMENT ON COLUMN` + 드리프트 컬럼 기록 | `alembic check`, 행수 대조 |
| 3 | `ports/handover.py` + `HandoverState`(ports/types.py) | `lint-imports` |
| 4 | `HandoverPgAdapter`(낙관적 락/리스, 바인드 파라미터) | 단위 + 동시성 테스트 |
| 5 | `application/handover_service.py` + `cli.py handover` + `update_handover.py` 브리지 | YAML 의미 동등성 |
| 6 | `AGENTS.md`에 "핸드오버 쓰기는 HandoverRepository만" 규칙, 레거시 raw SQL 제거 | 4 contracts KEPT |

**롤아웃**: 컬럼 추가(무중단) → 섀도우 쓰기(신·구 병행) → 파리티 확인 → 구경로 제거.

---

## 7. 리스크 / 완화

| 리스크 | 완화 |
|--------|------|
| 라이브 핸드오버 중단 | Step 1 특성화 테스트 + 섀도우 쓰기 + 파리티 후 전환 |
| 백필/마이그레이션 손상(35/112/63/103행) | additive-only `DEFAULT` 마이그레이션, 전후 행수·표본 대조 |
| 동시 쓰기(hook + 수동) | 리스 + 낙관적 락 CAS, 단일 리스 하 YAML 재생성 |
| Alembic이 미등록 테이블(tasks 등) 자동 포착 | `env.py`를 ORM metadata로 스코프, 스크립트 전용 테이블 명시 제외 |
| 계층 역전 | 타입을 `ports/types.py`(최하위)에 배치, `lint-imports` 게이트 |
| 컷오버 전 이중 경로 혼선 | 브리지는 컷오버까지만, 이후 단일 포트 |

---

## 8. 성공 기준

- [ ] 4개 핸드오버 테이블이 `domain/models.py`에 모델링되고 `alembic check` + import-linter(4 KEPT) 통과.
- [ ] 쓰기 경로에 raw f-string SQL 0건(전부 포트 경유).
- [ ] `state.schema_version` + `version`(낙관적 락) + 리스로 갱신 유실/중복 실행 0건.
- [ ] `git_state`/`content_hash`/`replaced_by` 실제 기록(드리프트 0).
- [ ] `update_handover.py`/`cli.py handover` 산출 `handover.yaml` 의미가 현행과 동일(특성화 테스트).
- [ ] `pytest -x --tb=short`, `ruff check src/devforge`, `mypy src/devforge`, `lint-imports` 통과.

---

## 9. 로드맵 배치 / 후속

- **배치**: Phase 2 **Gate 4 컷오버 이후** 리팩터링 Phase. 선행: `plans/watchdog-standard-compliance.md`(정본), `phase2-gate4-code-prereq.md`.
- **후속(별건)**: `tasks`/`watchdog_pulses` 등 스크립트 전용 테이블의 ORM 편입, `action_queue` claim 원자화, `handover-gen.timer` 문서-실체 불일치 정정(**완료 2026-09-24**: `update_handover.py`·`CLAUDE.yaml` → manual 명시).

---

## 10. 검증 (context7 / exa, 2026-09-22)

외부 제안 적응의 근거를 문서/웹으로 검증했다. (`scripts/deploy/kv-fetch-env.py ... --keys 'CONTEXT7-*,EXA-*'`로 키 주입)

| # | 검증 항목 | 출처 | 결과 |
|---|-----------|------|------|
| 1 | SQLAlchemy 2.0 낙관적 락 내장 | context7 `SQLAlchemy` (`orm/versioning`, `mapping_api`) | `version_id_col` + `StaleDataError` **지원** → §4.5 채택 |
| 2 | `FOR UPDATE SKIP LOCKED` | context7 `SQLAlchemy` (`selectable.with_for_update`) | `with_for_update(skip_locked=True, nowait=...)` **지원** → §4.5 채택 |
| 3 | psycopg3 동기 풀의 한계 | context7 `psycopg` (`api/pool`) | `ConnectionPool`=sync 멀티스레드, asyncio는 `AsyncConnectionPool` 필요 → **제안의 sync 전역 풀은 async 앱과 불일치** (§2.1·4.1 기각 근거 확증) |
| 4 | Pydantic 스키마 버전 | context7 `Pydantic` + exa | **내장 기능 아님**(관례). 필요 시 3rd-party `pydantic-versions`로 전이 관리 → §4.3은 관례로 명시 |
| 5 | 리스만으로 충분한가 | exa (Prisma, ipuau, master.dev, Fjord) | **불충분** — 만료 후 깨어난 워커가 last-write-wins로 덮어씀 → **펜싱 토큰(`version`) 필수** (§4.5 보강) |
| 6 | `SKIP LOCKED` = 표준 클레임 | exa (codenotes, prisma) | PG 9.5+ 큐 클레임의 기본 패턴 확증 |
| 7 | 어드바이저리 락 + 트랜잭션 풀러 | exa (Fjord) | 트랜잭션 풀러에서 세션 락이 깨져 **리더 2개** 가능 → §4.5 주의사항 |
| 8 | `gen_random_uuid()` / `uuid-ossp` | exa (PG 13/18 docs) | PG13+ **코어 내장**, `uuid-ossp`는 특수 알고리즘 전용 → 제안의 `uuid-ossp`+`uuid_generate_v4()` **불필요**(현행 `IDENTITY`/`pgcrypto` 유지 정당) |

**검증 결론**: 적응 설계(§4)의 핵심 4가지 — ① SQLAlchemy/asyncpg 유지, ② `version_id_col` 낙관적 락, ③ 리스+펜싱 토큰, ④ `SKIP LOCKED` 클레임 — 는 모두 문서로 뒷받침된다. 제안의 psycopg3 sync 풀·`uuid-ossp`·(내장으로 오인된) Pydantic 버전 기능은 기각/보정 대상이다.

> 발견된 부수 버그(설계 무관, 수정 완료): `scripts/lib/research/exa.py:59,99`가 미정의 `KEYS_ENV`를 참조해 키 부재 시 `NameError` → `"EXA API keys not configured"`로 수정.
