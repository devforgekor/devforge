# Preflight Gate Deep Dive

**Date:** 2026-07-21
**Context:** Pipeline startup preflight gate — watchdog checker/recovery 통합

---

## 1. Current State

### 문제: 3개 pipeline이 각자 다른 방식으로 startup

| Pipeline | 순서 | 문제 |
|----------|------|------|
| `extract.py` | `ensure_model` → `_launch_reranker` → `preflight_checks` | ❌ model 띄운 후 stale kill (역전) |
| `day_verify.py` | `ensure_model` → `preflight_checks` | ❌ model 띄운 후 stale kill |
| `enrich.py` | `ensure_sequential_dual` → `preflight_checks` | ❌ model 띄운 후 stale kill |

### 중복 체크 로직

| 체크 | `preflight_checks()` | `watchdog/checker.py` | `_preflight_gate()` (신규) |
|------|---------------------|----------------------|---------------------------|
| Port health | 자체 구현 (urllib) | `check_health()` | 없음 |
| Memory | `free -h` 로깅 | `check_memory()` | `/proc/meminfo` 직접 파싱 |
| DB | 없음 | `check_postgres()` | `psql_ok()` |
| Model file | 없음 | 없음 | `os.path.exists()` |
| Stale process kill | 자체 구현 | `kill_stale_process()` | 없음 |

→ **3곳에서 비슷한 체크를 각자 구현**, SSOT 없음.

---

## 2. Target Architecture

```
K8s startupProbe 패턴 차용:

  _preflight_gate()          ← startupProbe (init 완료까지 blocking)
    ├── check (watchdog/checker.py)
    │   ├── check_model_file()     ← 신규
    │   ├── check_memory_budget()  ← 신규 (pressure가 아니라 예산)
    │   ├── check_health(port)     ← 기존
    │   └── check_postgres()       ← 기존
    │
    ├── auto-fix (watchdog/recovery.py + pod_manager)
    │   ├── memory low → _reclaim_memory() + GC
    │   ├── port 8082 down → ensure_model("day-extractor")
    │   └── port 8080 down → _launch_reranker()
    │
    └── fail (sys.exit(1))
        ├── model file missing → 복구 불가
        └── DB down → 복구 불가

  preflight_checks()         ← thin wrapper (기존 역할 유지)
    └── watchdog/checker.py 함수 호출로 대체

  ensure_model() / _launch_reranker()   ← model pod start
```

### 체크-복구 매트릭스

| 체크 | 복구 가능? | 복구 액션 | 복구 실패 시 |
|------|-----------|-----------|-------------|
| Model file 없음 | ❌ | — | `sys.exit(1)` |
| 메모리 부족 (<10GB) | ✅ | `_reclaim_memory()` + GC | `sys.exit(1)` |
| Port 8082 죽음 | ✅ | `ensure_model("day-extractor")` | `sys.exit(1)` |
| Port 8080 죽음 | ✅ | `_launch_reranker()` | 경고 후 진행 (fallback 존재) |
| DB 죽음 | ❌ | — | `sys.exit(1)` |

---

## 3. Proposed Changes

### 3a. `lib/watchdog/checker.py` — 2개 함수 추가

```python
def check_model_file(model_key: str) -> tuple[bool, str]:
    """GGUF model file 존재 확인. (ok, detail)."""

def check_memory_budget(required_gb: float) -> tuple[bool, str]:
    """MemAvailable >= required_gb 확인. (ok, detail)."""
```

→ watchdog daemon도 runtime에 model file 소실 감지 가능.

### 3b. `_preflight_gate()` — watchdog checker + recovery 통합

```python
def _preflight_gate() -> None:
    """Static checks → auto-fix → fail. K8s startupProbe 패턴."""
    # 1. Model file (복구 불가)
    ok, detail = check_model_file("day-extractor")
    if not ok: sys.exit(1)

    # 2. Memory (복구: reclaim + GC)
    ok, detail = check_memory_budget(10)
    if not ok:
        _reclaim_memory() + gc.collect()
        ok, detail = check_memory_budget(10)
        if not ok: sys.exit(1)

    # 3. Port 8082 (복구: ensure_model)
    ok, _ = check_health(8082)
    if not ok:
        ensure_model("day-extractor", skip_if_healthy=False)
        ok, _ = check_health(8082)
        if not ok: sys.exit(1)

    # 4. Port 8080 (복구: _launch_reranker, 실패 시 경고 후 진행)
    ok, _ = check_health(8080)
    if not ok:
        _launch_reranker()

    # 5. DB (복구 불가)
    ok, detail = check_postgres()
    if not ok: sys.exit(1)
```

### 3c. `main()` 순서 통일

```
모든 pipeline: _preflight_gate() → preflight_checks() → ensure_model() / ensure_dual()
```

### 3d. `preflight_checks()` — watchdog 함수 재사용

중복 제거:
- Port health: 자체 urllib → `check_health()` 호출
- Memory: `free -h` 로깅 → `check_memory()` 호출
- Stale kill: 유지 (watchdog `kill_stale_process`와 유사하나 PPID check 추가)

---

## 4. 비교

| Dimension | As-is | To-be |
|-----------|-------|-------|
| **SSOT** | 3곳 분산 | `checker.py` 1곳 |
| **복구** | 없음 (무조건 fail) | auto-fix 후 재시도 |
| **Watchdog 연계** | 없음 | 동일 함수 공유 |
| **Pipeline 일관성** | pipeline마다 다름 | 전 pipeline 동일 패턴 |
| **변경량** | baseline | checker.py +8 line, preflight_gate rewrite, pipeline 3개 순서 변경 |

---

## 5. Recommendation

**APPROVED** — watchdog checker를 SSOT로 삼아 preflight gate와 daemon이 동일 로직 공유.

### 구현 순서

1. `checker.py`: `check_model_file()`, `check_memory_budget()` 추가
2. `extract.py`: `_preflight_gate()` rewrite — watchdog checker/recovery 사용
3. `day_verify.py`: `preflight_checks()` → `ensure_model()` 순서 변경
4. `enrich.py`: `preflight_checks()` → `ensure_sequential_dual()` 순서 변경
5. `preflight_checks()`: watchdog 함수 재사용 (선택, 추후)
6. Syntax check + import test
