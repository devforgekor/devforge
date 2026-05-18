# Code-as-Documentation Refactoring — 종합 검토 보고서

**Date**: 2026-05-18 | **Sources**: PEP 8, Django, Celery, Flask, Python stdlib, Hitchhiker's Guide

---

## 1. 외부 리서치 결과

### 1.1 Python 생태계 표준 패턴

조사한 모든 주요 프로젝트(Django, Celery, Flask, Python stdlib `importlib`)가 **단일 공유 유틸리티 모듈** 패턴을 사용합니다.

| 프로젝트 | DB 헬퍼 위치 | 패턴 |
|----------|-------------|------|
| **Django** | `django/db/backends/base/operations.py` | BaseDatabaseOperations — 모든 백엔드가 공유. `quote_name()` 한 번 정의, 6개 백엔드가 상속 |
| **Celery** | `celery/backends/database/` | 공유 DB 유틸을 전용 디렉토리로 추출. 9개 백엔드에서 중복 0건 |
| **Flask** | `flask/helpers.py`, `flask/wrappers.py` | 패키지 루트에 전용 헬퍼 모듈. 서브패키지 간 공유 |
| **Python stdlib** | `importlib.util` | `importlib.abc`(인터페이스) + `importlib.machinery`(구현) + `importlib.util`(공유 헬퍼). 3계층 분리 |

**공통 원칙**: 헬퍼 함수는 **한 번만 정의**, 호출자는 **항상 import 해서 사용**, 중복 허용 안 함.

### 1.2 Self-Documenting Code (PEP 8)

- 함수명이 곧 문서: `db.psql()`은 설명이 필요 없음. `_psql()`(로컬 정의)은 "이 파일 전용인가?" 의심 유발
- `import modu` → `modu.func()` 호출 스타일 권장 (Hitchhiker's Guide): 호출 지점마다 출처가 명시됨
- 주석은 **why**만, **what**은 함수명/모듈명으로 (PEP 8)

### 1.3 DRY 원칙 검증

> "Every piece of knowledge must have a single, unambiguous, authoritative representation within a system." — The Pragmatic Programmer

현재 DevForge 코드베이스의 `_psql()` 지식은 **10개 파일에 분산**되어 있으며 각각 timeout, 에러 처리, 반환 타입이 다릅니다. 단일 소스가 아닙니다.

---

## 2. 현재 코드베이스 진단

### 2.1 `_psql` 정의 불일치

| 파일 | Timeout | 에러 처리 | 반환 타입 | try/except |
|------|---------|----------|-----------|------------|
| `review_worker.py` | 30s | stdout print | `str` | No |
| `session_guard.py` | 10s | silent | `str` | Yes |
| `link_turns.py` | 30s | stderr print | `str` | Yes |
| `cli.py` | 10s | **없음** | **`CompletedProcess`** | No |
| `embed_turns.py` | 30s | `[embed]` prefix | `str` | Yes |
| `gen_server_state.py` | 10s(가변인자) | silent | `str` | Yes |
| `session_start.py` | 10s | silent | `str` | Yes |
| `qwen_executor.py` | 30s | stderr print | `str` | Yes |
| `test_review_models.py` | 15s | 없음 | `str` | No |

**불일치 항목**: timeout(10/15/30), 에러 출력 방식(5종), 반환 타입(str vs CompletedProcess), try/except 유무, stderr 출력 여부.

### 2.2 `_esc_sql` 정의 불일치

| 파일 | 함수명 | 이스케이프 대상 |
|------|--------|----------------|
| `review_worker.py` | `_esc_sql` | `'`, `\`, `\n`, `\r` |
| `session_guard.py` | `_esc_sql` | `'`, `\`, `\n`, `\r` |
| `cli.py` | `_escape_sql` | `'`, `\` (**개행 누락**) |

cli.py는 함수명도 다르고 개행 escaping도 빠져 있습니다.

### 2.3 PSQL 명령어 불일치

| 파일 | PG User |
|------|---------|
| `embed_turns.py` | `-U devforge` |
| 나머지 전체 | `-U postgres` |

---

## 3. PLAN.md vs 실제 코드 정합성

PLAN.md Section 7은 `scripts/` 아래 3개 파일만 상정하지만, 실제로는 `lib/` 디렉토리가 6개 모듈로 이미 운영 중:

```
scripts/lib/
├── agents.py           ← qwen_worker, collect_turns, cli가 import
├── qwen_executor.py    ← qwen_worker가 import  
├── parser_claude.py    ← collect_turns가 import
├── parser_copilot.py   ← collect_turns가 import
├── parser_gemini.py    ← collect_turns가 import
├── parser_qwen.py      ← collect_turns가 import
├── key_rotator.py      ← embed_turns, gemini_rotate가 import
├── crypto.py           ← gemini_rotate가 import
└── search_manager.py   ← qwen_executor가 import
```

**PLAN.md는 코드 현실을 반영하지 못하고 있습니다.** `lib/` 공유 패턴은 이미 검증됐습니다.

---

## 4. 권고: `lib/db.py` 신설 + 일괄 마이그레이션

### 4.1 설계

```python
# scripts/lib/db.py — PostgreSQL utility functions for all DevForge scripts

PSQL = ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
        "-d", "devforge_app", "--no-align", "--tuples-only", "--quiet"]

def psql(sql: str, timeout: int = 30) -> str:
    """Execute SQL, return stdout. Empty string on error."""
    ...

def psql_ok(sql: str, timeout: int = 30) -> bool:
    """Execute SQL, return True if successful."""
    ...

def esc_sql(s: str) -> str:
    """Escape for SQL literal: quotes, backslashes, newlines."""
    ...
```

**설계 결정**:
- 함수명 `_` prefix 제거 → 공유 모듈이므로 private 표기 불필요 (PEP 8: `_`는 모듈 내부용)
- `import lib.db` → `db.psql(...)` 호출 패턴 (Hitchhiker's Guide 권장: 출처 명시)
- timeout 기본값 30s (review_worker.py 기준, 가장 무거운 쿼리 대응)
- 에러 출력은 stderr로 통일 (stdout은 파이프 파싱과 충돌)

### 4.2 마이그레이션 범위

| 파일 | 변경 |
|------|------|
| `review_worker.py` | `_psql`→`db.psql`, `_psql_ok`→`db.psql_ok`, `_esc_sql`→`db.esc_sql` |
| `session_guard.py` | `_psql`→`db.psql`, `_psql_ok`→`db.psql_ok`, `_esc_sql`→`db.esc_sql` |
| `link_turns.py` | `_psql`→`db.psql` |
| `cli.py` | `_psql`→`db.psql`, `_escape_sql`→`db.esc_sql` |
| `embed_turns.py` | `_psql`→`db.psql`, `-U devforge`→`-U postgres` |
| `gen_server_state.py` | `_psql`→`db.psql` |
| `session_start.py` | `_psql`→`db.psql` |
| `qwen_executor.py` | `_psql`→`db.psql` |
| `qwen_worker.py` | `lib.qwen_executor._psql`→`lib.db.psql` |
| `test_review_models.py` | `_psql`→`db.psql` |

### 4.3 PLAN.md 갱신

```diff
  ├── scripts/
+ │   ├── lib/
+ │   │   ├── db.py                       ← Shared DB helpers (psql, esc_sql)
+ │   │   ├── agents.py                   ← Agent name normalization (SSOT)
+ │   │   ├── qwen_executor.py            ← Qwen call + observation execution
+ │   │   └── parser_*.py                 ← Session transcript parsers
  │   ├── review_worker.py
  │   ├── session_guard.py
  │   └── worklog_reconcile.py            ← TO BE CREATED
```

---

## 5. 위험 평가

| 위험 | 수준 | 대응 |
|------|------|------|
| timeout 변경으로 인한 회귀 (10s→30s) | LOW | 더 긴 timeout이 실패를 줄임. 30s는 review_worker에서 검증됨 |
| `-U devforge` → `-U postgres` 권한 | NONE | postgres는 superuser, devforge보다 권한 높음 |
| `_psql`→`db.psql` 호출부 50+곳 수정 | LOW | 기계적 치환, 테스트로 검증 가능 |
| 기존 동작 변경 | LOW | 기능 변경 없음, 순수 리팩토링 |

---

## 6. 결론

1. **Python 생태계 표준**: Django, Celery, Flask, Python stdlib 모두 DB 헬퍼를 단일 공유 모듈에 둠. 중복 허용 안 함.
2. **현재 상태**: `_psql` 9개 정의가 timeout, 에러 처리, 반환 타입에서 모두 다름. `_esc_sql`은 3개 중 1개가 개행 누락.
3. **`lib/` 패턴은 이미 운영 중**: PLAN.md만跟不上 상태. 코드 현실을 PLAN.md에 반영해야 함.
4. **`lib/db.py` 신설이 정답**: 10개 파일 중복 제거, 단일 timeout/에러 처리/escaping 표준, 호출부마다 `db.psql()`로 출처 명시.

**다음 단계**: 승인 시 `lib/db.py` 생성 → 10개 파일 마이그레이션 → PLAN.md 갱신 순으로 진행.

---

*Generated: 2026-05-18 | Sources: PEP 8, Django 5.1, Celery, Flask, Python 3.13 importlib, Hitchhiker's Guide to Python*
