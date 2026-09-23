# Python 런타임 버전 전략 — 현황 매트릭스와 이관 개선안

**Status:** active · **Date:** 2026-09-22 · **Owner:** devforge
**관련:** `REFACTORING_PLAN.md`, `docs/plans/phase1-plan.md`, handover `PY-RUNTIME-SPLIT-2026-09-22`
**Deep Dive:** `dp-20260922-python-runtime-version-strategy` (Yggdrasil)
**원칙:** **지금은 설정을 바꾸지 않는다**(검증 선행). 이 문서는 현황 고정 + 이관 계획이다.

---

## 0. 결정 (2026-09-22)

**devforge 기준 = Python 3.12** (사용자 결정). 근거: 실행 컨테이너가 이미 3.12.13이라
**재빌드 불필요**, 주요 의존성 3.12 지원(Context7). legacy `scripts/`와 대다수 user unit은
당분간 3.9 유지.

## 1. 런타임 매트릭스

### 이행 후 (2026-09-22)
| 영역 | 버전 | 상태 |
|------|------|------|
| devforge 기준 / 호스트 dev·test | **3.12** (3.12.14) | python3.12 설치 + `pip install --user -e ".[dev]"` 완료 |
| `pyproject.toml` | `requires-python>=3.12`, ruff `py312`, mypy `3.12` | ✅ 변경 |
| `Dockerfile` | `python:3.12-slim` (builder/runtime) | ✅ 변경(실행 이미지와 일치) |
| CI `.github/workflows/ci.yml` | `PYTHON_VERSION: "3.12"` | ✅ 변경 |
| 실행 컨테이너 (devforge-base/fastapi/mcp/worker) | 3.12.13 | 변경 없음(이미 3.12) |
| 호스트 기본 `python3` | 3.9.25 | **미변경**(legacy scripts용) |
| user unit | **8×3.12 (검증 완료) / 나머지 3.9·3.11** | **부분 전환**(2026-09-22) — 아래 §4 참조 |
| legacy `scripts/` | 3.9 | 당분간 유지 |

### 이행 전 (참고)
호스트 기본 3.9.25 · devforge는 python3.9 editable · Dockerfile 3.11-slim(실행 이미지 3.12와
불일치) · pyproject `>=3.9`/ruff py39/mypy 3.9 · user unit 27×3.9·6×3.11.

### 3.9 호환 텍스트
- ✅ **해소:** `pyproject.toml` 5항목(11/24/86/97/106 → 3.12), `mcp/server.py:11`(주석 갱신)
- ⏳ **잔존(의도적):** `messenger.py:57,190`(legacy 3.9 유지), `timetable-calendar-sync.md:63`
  (별개 서비스), `model_comparison_test.py:148`(fixture 데이터)

### 검증 결과 (Python 3.12, 2026-09-22)
`pytest tests/unit tests/characterization` **68 passed** · `ruff` pass · `mypy src/devforge`
41 files success · `lint-imports` 4 KEPT · `import devforge` OK.
- 3.12에서 발견·수정: `mcp/server.py` mypy 3건(`_StepBudget` TypedDict + `_as_aware` cast),
  `pyproject [dev]`에 레거시 테스트 의존성(`tiktoken`, `langdetect`) 추가.
- 주의: `mypy python_version=3.9`로 3.12를 검사하면 anyio의 `match`에서 실패 → 3.12 상향이 필수였음.

---

## 2. Context7 검증 — 의존성의 Python 3.11/3.12 지원

`cli.py research docs`(Context7)로 확인. **키 주의:** `CONTEXT7_API_KEYS`가 셸에 없어
KV 개별 키(`CONTEXT7_*_API_KEY`)를 `label:value`로 조합해야 동작한다.

| 라이브러리 | 설치 버전 | Context7 근거 | 3.11 | 3.12 |
|-----------|-----------|---------------|------|------|
| SQLAlchemy | 2.0.52 | 2.0 min 3.7; 3.12 지원은 2.0.44+ (PEP 695) | ✅ | ✅ |
| asyncpg | 0.31.0 | "requires Python 3.9 or later" | ✅ | ✅ |
| pydantic | 2.13.4 | `requires-python >= 3.9` | ✅ | ✅ |
| alembic | 1.16.5 | 1.17부터 "Python 3.10 and newer" | ✅ | ✅ |
| sse-starlette | 3.3.0 | 3.4.x는 `requires-python >= 3.10`, 3.10–3.13 테스트 | ✅ | ✅ |
| PyYAML | — | 3.8–3.13 | ✅ | ✅ |
| oci | — | 3.12 지원 | ✅ | ✅ |
| structlog / apprise | 25.5.0 / — | Context7 문서 없음(확인 불가) | 추정 ✅ | 추정 ✅ |

**결론:** 주요 의존성은 **3.11·3.12 모두 지원**. 다만 최신 릴리스가 3.9를 **점차 탈락**시킨다
(sse-starlette 3.4 = 3.10+, alembic 1.17 = 3.10+). 즉 3.9 잔류는 향후 `pip` 해석에서
의존성 상향이 막히는 압력으로 작용한다.

---

## 3. 위험

| # | 위험 | 등급 |
|---|------|------|
| R1 | `Dockerfile`(3.11) ≠ 실행 이미지(3.12) — 빌드 재현성 불일치 | 중 |
| R2 | `requires-python>=3.9` 선언 vs 일부 의존성 3.10+ 신호 → 선언이 실제와 어긋남 | 중 |
| R3 | pyproject만 3.11로 올리면 **호스트 3.9에서 `pip install -e .` 실패** | 높음 |
| R4 | 테스트가 어느 버전 기준인지 불명확(현재 3.9에서 실행) | 중 |
| R5 | 혼재(3.9/3.11/3.12)로 재현성·온보딩 저하 | 중 |

---

## 4. 이행 (실행 완료 2026-09-22)

**영역 분리 원칙:** `devforge`(신규) / `legacy scripts`(기존) / `보조 서비스`(서비스별)를
각각 독립적으로 전환한다.

### 완료
- **Step 0 현황 고정** ✅ 본 문서 + handover `PY-RUNTIME-SPLIT-2026-09-22`.
- **Step 1 검증(3.12)** ✅ `uv venv --python 3.12` + `pip install -e ".[dev]"` →
  pytest 68 passed, ruff/mypy/lint-imports green (검증 중 발견한 `mcp/server.py` mypy 3건과
  레거시 테스트 의존성 `tiktoken`/`langdetect`도 함께 정리).
- **Step 2 pyproject 상향** ✅ `requires-python>=3.12`, classifier 3.12, ruff `py312`,
  mypy `3.12`, `[dev]`에 tiktoken/langdetect.
- **Step 3 Dockerfile/CI** ✅ `python:3.12-slim`(builder+runtime), ci `PYTHON_VERSION=3.12`.
- **호스트 dev/test** ✅ python3.12(3.12.14) 설치 + `pip install --user -e ".[dev]"`.

### 남음 (컷오버 이월)
- **Step 4 정정:** 호스트 user unit 중 **`devforge` 패키지를 실행하는 것은 0개**(전부 legacy
  `scripts/*.py`). 따라서 "devforge 관련 유닛 전환"은 대상이 없고, 유닛을 3.12로 바꾸는 것은
  곧 **legacy 스크립트를 3.12에서 실행**하는 일 → **컷오버(Phase A~I)** 에서 legacy 호환성 검증과
  함께 처리. `sync-units.sh`로 미러 정합 유지(미러=SSOT).
- **Step 5** legacy `scripts/`와 나머지 unit 별도 마이그레이션(서비스별, 컷오버).
- 호스트 기본 `python3`(3.9)는 legacy용으로 유지.
- **우선순위:** Phase 1(devforge 포트/어댑터)을 먼저 진행. Step 4/5는 컷오버로 이월(Phase 1과 무관).

### 부분 전환 완료 (2026-09-22) — 검증된 8개만
이번 세션에서 키 로더 이관·`--keys` 필터를 적용하며 **함께 검증된 8개 서비스**만 3.12로 전환
(§5 "legacy scripts와 user unit 동시 전환 금지"의 예외 — **검증 선행** 조건 충족):

| 서비스 | 스크립트 | 3.12 검증 |
|--------|----------|-----------|
| anthropic-openrouter-proxy, anthropic-proxy, anthropic-gudokpin-proxy | `proxies/anthropic*.py` | active |
| openrouter-rr-proxy | `proxies/openrouter_rr_proxy.py` | active + `/v1/models` 응답 |
| or-rate-limiter | `or_rate_limiter.py` | active |
| devforge-watchdog | `watchdog.py` + `lib/watchdog/*` | active + "Watchdog started" |
| devforge-news, devforge-summary-retry | `news/collector.py`, `summary_retry.py` | summary_retry `--dry-run` exit 0; collector import OK (피드 fetch hang은 3.9와 동일 = 네트워크) |

- `tenacity`, `feedparser`를 3.12에 설치.
- **나머지 17개 유닛은 롤백**(3.9/3.11 유지) → 컷오버로 이월. 커밋 `7d6ac60`.
- 검증 없이 일괄 이관했다가 되돌린 이력: `2bcc515`(일괄) → `7d6ac60`(17개 롤백).

### 다음 배치 (사전 준비 — shadow-run 창 이후)

전환 대상(legacy 3.9 유지 유닛)은 실행 시점에 재열거한다:

```bash
# ExecStart에 python3.12 미지정 user 유닛 (legacy 3.9 후보)
grep -L "python3.12" ~/.config/systemd/user/*.service
```

절차(검증 선행, 서비스별):
1. 대상 스크립트를 `python3.12 -c "import ..."` 또는 `--dry-run`으로 import/동작 검증.
2. `ExecStart`의 `/usr/bin/python3` → `/usr/bin/python3.12` 변경(미러=SSOT → `sync-units.sh`).
3. `daemon-reload` + restart + health 확인. 실패 시 즉시 원복.
4. 검증 통과분만 배치, 실패분은 3.9 유지.

> **제약**: watchdog v2 shadow-run 창(09-23 13:32 ~ 09-24 13:32 UTC) 동안은 모니터링 대상 서비스 재기동이
> 비교를 교란하므로 **창 이후** 착수. `devforge` 패키지 실행 유닛은 0개이므로 Step 4는 컷오버에서 처리.

### 롤백
- `git checkout` pyproject/Dockerfile/ci + `daemon-reload`. 3.9 환경은 그대로 남아 있음.

---

## 5. 주의 (검증 선행)

- pyproject 상향은 **3.12 검증 통과 후**에만 (완료). 검증 없이 올리면 호스트 3.9 `pip install` 실패(R3).
- legacy scripts와 user unit 동시 전환 금지.
- `docs/architecture/`(자동/동결) 편집 금지.

---

## 6. 결정 (2026-09-22 확정)

1. **devforge 공식 기준 = Python 3.12** ✅ (실행 컨테이너와 일치, 재빌드 불필요).
2. **legacy scripts = 3.9 유지** (기한 미정), 서비스별 전환.
3. **호스트 dev/test = 3.12** (`python3.12`); 기본 `python3`(3.9)는 legacy용으로 유지.
