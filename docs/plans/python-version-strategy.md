# Python 런타임 버전 전략 — 현황 매트릭스와 이관 개선안

**Status:** active · **Date:** 2026-09-22 · **Owner:** devforge
**관련:** `REFACTORING_PLAN.md`, `docs/plans/phase1-plan.md`, handover `PY-RUNTIME-SPLIT-2026-09-22`
**Deep Dive:** `dp-20260922-python-runtime-version-strategy` (Yggdrasil)
**원칙:** **지금은 설정을 바꾸지 않는다**(검증 선행). 이 문서는 현황 고정 + 이관 계획이다.

---

## 1. 현황 매트릭스 (실측 2026-09-22)

| 영역 | 현재 버전 | 근거 | 목표(제안) | 변경 |
|------|-----------|------|-----------|------|
| 호스트 기본 `python3` | **3.9.25** | `/usr/bin/python3` → `python3.9` | 3.11 (devforge 한정 venv) | 예정 |
| `python3.11` | 3.11.13 | 설치됨, devforge/deps **미설치** | — | — |
| `python3.12` | 호스트 미설치 | — | (대안 기준) | 선택 |
| devforge 패키지 설치 | **python3.9** editable | `.local/lib/python3.9/site-packages` | 3.11 venv | 예정 |
| 실행 컨테이너 (devforge-base/fastapi/mcp/worker) | **3.12.13** | `podman inspect` PYTHON_VERSION | 3.11 (또는 3.12 선언) | 결정 |
| 컨테이너 flaresolverr / inference | 3.11.15 / 3.12.3 | `podman exec` | 유지 | 아니오 |
| repo `Dockerfile` | **python:3.11-slim** | `FROM` 라인 | 실행 이미지와 일치 | 수정 |
| user unit (33) | **27× `/usr/bin/python3`(3.9)**, **6× `python3.11`** | 유닛 grep | devforge 관련만 3.11 | 단계적 |
| `pyproject.toml` | `requires-python>=3.9`, ruff `py39`, mypy `3.9` | grep | 3.11 | 예정 |

6개 3.11 유닛: `devforge-backup`, `devforge-openrouter-free-models`, `devforge-restore-test`,
`devforge-tg-webhook`, `openrouter-rr-proxy`, `or-rate-limiter`.

### 3.9 호환 텍스트 (정정 대상, 이관 후)
| 위치 | 내용 |
|------|------|
| `pyproject.toml:11` | `requires-python = ">=3.9"` |
| `pyproject.toml:24` | classifier `Python :: 3.9` |
| `pyproject.toml:86` | ruff `target-version = "py39"` |
| `pyproject.toml:97` | `"UP"` 비활성 주석 |
| `pyproject.toml:106` | mypy `python_version = "3.9"` |
| `scripts/lib/watchdog/messenger.py:57,190` | "Python 3.9: list[dict] not supported" |
| `src/devforge/adapters/driving/mcp/server.py:11` | "for Python 3.9 compatibility" |
| `docs/runbooks/timetable-calendar-sync.md:63` | "Python 3.9+" (별개 서비스) |
| `scripts/tests/model_comparison_test.py:148` | fixture 문구 "Stack: Python 3.9" |

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

## 4. 개선안 (단계적 · 즉시 변경 금지)

**영역 분리 원칙:** `devforge`(신규) / `legacy scripts`(기존) / `보조 서비스`(서비스별)를
각각 독립적으로 전환한다. 한 번에 올리지 않는다.

### Step 0 — 현황 고정 (완료)
- 본 문서 + handover `PY-RUNTIME-SPLIT-2026-09-22`. **설정 파일 무변경.**

### Step 1 — devforge를 3.11에서 검증 (변경 아님, 검증)
```bash
python3.11 -m venv /opt/projects/server/.venv311
. .venv311/bin/activate
pip install -U pip && pip install -e ".[dev]"
pytest tests/unit tests/characterization -q
ruff check src tests && mypy src/devforge && lint-imports
```
- 통과 시에만 다음 단계. 실패 시 원인 기록 후 보류.
- (대안) 3.12 기준이면 `python3.12` 설치 후 동일 검증.

### Step 2 — pyproject 상향 (검증 통과 후, 별도 커밋)
```toml
requires-python = ">=3.11"            # 또는 >=3.12
# classifier: 3.9 제거, 3.11(또는 3.12) 추가
# [tool.ruff] target-version = "py311" # 또는 py312
# [tool.mypy] python_version = "3.11"  # 또는 3.12
# "UP" 비활성 주석 정리(3.9 사유 제거)
```
+ §1의 3.9 텍스트 정정(주석/fixture). `docs/runbooks/timetable-*`는 별개 서비스이므로 분리 판단.

### Step 3 — Dockerfile ↔ 실행 이미지 일치
- 3.11 기준이면 이미지를 `python:3.11-slim`으로 재빌드(별도 창), 3.12 기준이면 `Dockerfile`을
  `python:3.12-slim`으로 수정. **둘 중 하나로 반드시 일치.**

### Step 4 — devforge 관련 user unit만 3.11로
- 현재 6개는 이미 3.11. 나머지 중 devforge 직접 관련(예: `devforge-turn-watcher`)부터 전환.
- 전환 전 `sync-units.sh`로 미러 정합 유지(미러가 SSOT).

### Step 5 — legacy scripts / 나머지 unit (별도 마이그레이션)
- 서비스별 검증 후 개별 전환. 일괄 금지.

### 롤백
- 3.9 환경/유닛 정의를 유지 → 문제 시 `git checkout`/unit 원복 + `daemon-reload`.

---

## 5. 하지 말 것 (금지)

- ❌ `pyproject`만 `>=3.11`로 일괄 상향 (호스트 3.9 `pip install` 실패, R3).
- ❌ legacy scripts와 user unit을 동시 전환.
- ❌ 검증 없이 컨테이너 재빌드/유닛 전환.
- ❌ `docs/architecture/`(자동/동결) 편집.

---

## 6. 결정 필요 (사용자)

1. **devforge 공식 기준 버전:** `3.11`(repo Dockerfile과 일치, 보수적) vs `3.12`(실행 컨테이너와 일치).
2. **legacy scripts 3.9 유지 기한** 및 서비스별 전환 우선순위.
3. 호스트 dev/test를 3.11/3.12 중 무엇으로 둘지(테스트 기준 단일화).
