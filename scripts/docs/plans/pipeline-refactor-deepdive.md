# Pipeline Refactor Deep Dive

**Date:** 2026-07-20
**Context:** Phase 3 `_refine_batch` in-place mutation → functional consistency

---

## 1. Current State

### Phase 3 코드 (extract.py:699-725)

```python
# ── Phase 3: Refine ── Parallel refine ──
if extractions_by_turn:
    _refine_batch(extractions_by_turn)       # ❌ in-place mutation

# Status fix
if extractions_by_turn:
    for tid in list(extractions_by_turn.keys()):
        ...
        extractions_by_turn[tid] = _fix_status_hallucination(   # ✅ return new
            extractions_by_turn[tid], src_text
        )
        extractions_by_turn[tid] = _quality_check_facts(        # ✅ return new
            extractions_by_turn[tid], source_text_qc
        )
```

### 불일치 함수

| 함수 | 시그니처 | 패턴 | 반환 |
|------|----------|------|------|
| `_refine_batch` | `(extractions_by_turn: Dict) -> None` | ❌ in-place | None |
| `_fix_status_hallucination` | `(facts: list, source_text: str) -> list` | ✅ functional | new list |
| `_quality_check_facts` | `(facts: list, source_text: str) -> list` | ✅ functional | new list |

### `_refine_batch` 내부 (extract_verify.py:370-416)

```python
def _refine_batch(extractions_by_turn: Dict[str, List[Dict]]) -> None:
    candidates = []
    for tid, extractions in extractions_by_turn.items():
        for i, ex in enumerate(extractions):
            if len(ex.get("source_context", "") or "") > 10:
                candidates.append((tid, i, ex["source_context"][:500], ex.get("evidence")[:300]))

    if not candidates:
        return

    def _refine_one(cand):
        # LLM call per candidate → returns (tid, idx, corrected)
        ...

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_refine_one, candidates))

    for tid, idx, corrected in results:
        if tid in extractions_by_turn and idx < len(extractions_by_turn[tid]):
            extractions_by_turn[tid][idx]["corrected_evidence"] = corrected  # ❌ mutate
```

---

## 2. Problem Analysis

### 문제 1: 반환값 불일치

- Phase 1~2: `result = func(data)` 체인 → 예측 가능
- Phase 3: `_refine_batch(dict)` → dict가 내부에서 수정됨 → **함수 호출만 보고 데이터 흐름을 알 수 없음**

### 문제 2: 추론 부담

```python
# 현재: _refine_batch가 extractions_by_turn을 mutate
_refine_batch(extractions_by_turn)              # 뭐가 바뀌었지?
```

vs

```python
# 통일 후: 명시적 할당
extractions_by_turn = _refine_batch(extractions_by_turn)  # 바뀐 dict를 명시적으로 받음
```

### 문제 3: 테스트 어려움

in-place mutation 함수는 호출 전후 상태를 비교해야 하지만, dict 자체가 바뀌어서 복사본 없이 비교 불가.

---

## 3. Proposed Change

### `_refine_batch` → return new dict

```python
def _refine_batch(
    extractions_by_turn: Dict[str, List[Dict]],
) -> Dict[str, List[Dict]]:
    """Returns a new dict with corrected_evidence applied."""
    candidates = []
    for tid, extractions in extractions_by_turn.items():
        for i, ex in enumerate(extractions):
            if len(ex.get("source_context", "") or "") > 10:
                candidates.append((tid, i, ex["source_context"][:500], ex.get("evidence")[:300]))

    if not candidates:
        return extractions_by_turn  # unchanged

    def _refine_one(cand):
        tid, idx, src, ev = cand
        try:
            reply = call_llm(
                [{"role": "user", "content": _REFINE_FACT_PROMPT.format(evidence=ev, source_context=src)}],
                model="day_extract", max_tokens=256, temperature=0.0, timeout=60,
            )
            refined = reply.strip().strip("\"'")
            if len(refined) > 10 and refined != ev:
                return (tid, idx, refined)
        except Exception:
            pass
        return (tid, idx, f"{ev} | {src}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_refine_one, candidates))

    # Build new dict with corrected evidence
    result = {}
    for tid, extractions in extractions_by_turn.items():
        result[tid] = [dict(ex) for ex in extractions]  # shallow copy

    for tid, idx, corrected in results:
        if tid in result and idx < len(result[tid]):
            result[tid][idx]["corrected_evidence"] = corrected
            print(f"      [refine] turn {tid[:8]} fact {idx}: refined ({len(corrected)}ch)", flush=True)

    return result
```

### Phase 3 호출부 변경

```python
# ── Phase 3: Refine ── Parallel refine ──
if extractions_by_turn:
    extractions_by_turn = _refine_batch(extractions_by_turn)  # ✅ 명시적 할당

# Status fix
if extractions_by_turn:
    for tid in list(extractions_by_turn.keys()):
        src_text = ...
        source_text_qc = ...
        if src_text:
            corrected = _fix_status_hallucination(
                extractions_by_turn[tid], src_text
            )
            extractions_by_turn[tid] = _quality_check_facts(
                corrected, source_text_qc
            )
        else:
            extractions_by_turn[tid] = _quality_check_facts(
                extractions_by_turn[tid], source_text_qc
            )
```

---

## 4. 비교

| Dimension | As-is (in-place) | To-be (functional) |
|-----------|------------------|-------------------|
| **데이터 흐름** | 함수 내부에서 추적 | 시그니처로 명확 |
| **디버깅** | 호출 전후 diff 필요 | 중간값 print() 가능 |
| **테스트** | 원본 훼손 방지 위해 deepcopy | 원본 불변, 비교 쉬움 |
| **성능** | zero-copy | shallow copy (extra list + dict per turn) |
| **변경량** | baseline | `_refine_batch` 1개 함수 + 호출부 1줄 |
| **회귀 위험** | 낮음 (변경 없음) | 낮음 (변경 범위 최소) |

### 성능 영향

`_refine_batch`가 turn당 extraction dict들을 shallow copy:
- 각 extraction dict는 평균 ~10 keys, ~200 bytes
- turn당 평균 10 facts → ~2KB/turn
- 50 turn batch → ~100KB → **무시 가능**

---

## 5. Recommendation

**APPROVED** — 변경 범위가 `_refine_batch` 단일 함수 + 호출부 1줄로 최소.

### 구현 순서

1. `_refine_batch` 시그니처 변경: `-> None` → `-> Dict[str, List[Dict]]`
2. 내부: deepcopy → dict comprehension + shallow copy per turn
3. `extract.py` Phase 3: `_refine_batch(extractions_by_turn)` → `extractions_by_turn = _refine_batch(extractions_by_turn)`
4. 동시에 `_fix_status_hallucination` QC2 호출 간소화 (중복 source_text 탐색 제거)
5. Syntax check + unit test
