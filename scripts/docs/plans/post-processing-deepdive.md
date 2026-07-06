# Post-Processing Deep Dive Plan

## 1. Overview

**목표**: Chunk-based extraction(400자)의 한계를 post-processing으로 보완. LLM 호출 최소화, 기존 파이프라인 재사용.

**현재 pipeline** (`_normalize_freeform_pipeline`, `extract_llm.py:655`):
```
chunk extract → _group_entities → _normalize_predicate → _group_predicates → _dedup_post_norm
```
→ 여기에 3단계 post-processing 추가

---

## 2. 상세 설계

### Step 1: Multi-value Expansion (`_expand_multi_value`)

**목적**: 한 SPO object에 `and`/`,`로 연결된 복수 값을 별도 fact로 분리

**적용 대상 패턴** (실제 파이프라인 출력 기반):
| 원본 object | 분리 결과 |
|---|---|
| `"increased to 8GB and tuned to G1GC"` | fact1: `heap → increased_to → 8GB`<br>fact2: `heap → tuned_to → G1GC` |
| `"ran every 2 seconds with 4GB heap and 3.8GB live set"` | fact1: `Java GC → ran_every → 2 seconds`<br>fact2: `Java GC → had → 4GB heap`<br>fact3: `Java GC → had → 3.8GB live set` |

**알고리즘**:

```
입력: fact dict
1. object에 " and " 또는 ", " 포함?
2. YES → 문장 구조 분석
   a. "increased X and Y" 패턴 → predicate는 동일, object만 변경
   b. "ran every X with Y and Z" 패턴 → 각각 별도 predicate 추정
3. 규칙 기반 분할 (LLM 불필요):
   - "increased/promoted/scaled X and Y" → 동일 predicate, object 교체
   - 일반 "X and Y" → predicate 추정 필요시 LLM 사용
4. 새 fact 생성: subject/evidence/qualifiers 복사, object/predicate만 변경
5. _dedup_post_norm 재실행
```

**구현 위치**: `_normalize_freeform_pipeline()` 내, `_group_predicates` 후, `_dedup_post_norm` 전

**의사코드**:
```python
def _expand_multi_value(facts: list[dict]) -> list[dict]:
    expanded = []
    for f in facts:
        obj = f.get("object", "")
        parts = _split_conjunctive(obj)
        if len(parts) <= 1:
            expanded.append(f)
            continue
        for i, part in enumerate(parts):
            new_f = dict(f)
            new_f["object"] = part.strip()
            if i > 0:
                new_f["predicate"] = _infer_predicate(f.get("predicate", ""), part)
            expanded.append(new_f)
    return expanded
```

**Edge cases**:
- `"and"`가 단순 나열이 아닌 경우: `"research and development"` → 분리하지 않음 (고정 구문)
- `","`가 리스트 구분자가 아닌 경우: `"New York, New York"` → 분리하지 않음
- 분리된 fact의 predicate 추정 실패 시 → 원본 predicate 유지, dedup에서 중복 제거

---

### Step 2: Qualifier Splitting (`_split_qualifiers`)

**목적**: Object에 내장된 qualifier 정보를 별도 필드로 분리

**적용 대상 패턴**:
| 원본 object | 분리 결과 |
|---|---|
| `"503 errors for 12% of requests"` | object: `"503 errors"`, qualifier: `{"percentage":"12% of requests"}` |
| `"peaked at 87% during peak hours"` | object: `"87%"`, qualifier: `{"time":"peak hours"}` |
| `"exhausted at 50 connections"` | object: `"50"`, qualifier: `{"count":"connections"}` |
| `"increased from 45 minutes to 3 hours"` | object: `"3 hours"`, qualifier: `{"range":"from 45 minutes"}` |

**알고리즘**:

```python
_QUALIFIER_PATTERNS = [
    (r",?\s*for\s+(.+)$", "target"),
    (r",?\s*during\s+(.+)$", "context"),
    (r",?\s*at\s+(\d+)\s+(\w+)$", "count"),
    (r",?\s*from\s+(.+?)\s+to\s+(.+)$", "range_from", "range_to"),
    (r",?\s*with\s+(.+)$", "condition"),
]
```

**구현 위치**: `_normalize_freeform_pipeline()` 내, `_expand_multi_value` 직후

**의사코드**:
```python
def _split_qualifiers(facts: list[dict]) -> list[dict]:
    for f in facts:
        obj = f.get("object", "")
        quals = f.get("qualifiers", {}) or {}
        for pattern, qual_key in _QUALIFIER_PATTERNS:
            m = re.search(pattern, obj, re.IGNORECASE)
            if m:
                quals[qual_key] = m.group(1).strip()
                obj = obj[:m.start()].strip().rstrip(",").strip()
        f["object"] = obj
        f["qualifiers"] = quals
    return facts
```

**Edge cases**:
- `"for"`가 qualifier가 아닌 경우: `"search for answers"` → 전치사/qualifier 구분 필요
- 중복 qualifier: 여러 패턴이 동시 매칭될 수 있음 (예: `"for 12% during peak hours"`)
- Greedy matching 방지: `(.+)$` 대신 `([^,]+)$` 사용

---

### Step 3: Missing Fact Completion (`_complete_missing_facts`)

**목적**: Chunk 단위 추출에서 누락된 fact를 전체 텍스트 기반으로 보완

**설계 결정**: **Offline step** (별도 스크립트, day cycle re-embedding 직전 실행)
- 이유: inline 시 pipeline 타임아웃(900s)에 근접 → 분리하여 리스크 제거
- 서버 여유: load avg 0.31, 1.2GB available → offline 배치 처리 가능

**알고리즘**:

```python
def supplement_facts(turn_id, original_text, existing_facts):
    prompt = f"""You are a fact extraction auditor. Below is the original text and the facts already extracted from it.

ORIGINAL TEXT:
{original_text}

ALREADY EXTRACTED FACTS:
{_format_facts_for_prompt(existing_facts)}

TASK: Identify important factual statements in the ORIGINAL TEXT that are NOT captured by the ALREADY EXTRACTED FACTS. Focus on:
- Causal relationships (X caused Y, X led to Y)
- Secondary attributes (values, thresholds, measurements)
- Post-fix outcomes (results, improvements, regressions)
- Contextual details (timeframes, conditions, locations)

Output ONLY valid JSON. No markdown.
{{"supplementary_facts": [{{"evidence":"...","subject":"...","predicate":"snake_case","object":"..."}}]}}
Empty: {{"supplementary_facts":[]}}"""
    response = _call_llm_8082(prompt)
    new_facts = parse_response(response)
    verified = _nli_verify_batch(new_facts, original_text)
    return verified
```

**구현 세부사항**:
- `_call_llm_8082()`: 기존 `_call_with_8082_retry` 패턴 재사용
- `_format_facts_for_prompt()`: SPO 목록을 간결한 텍스트로 변환
- `_nli_verify_batch()`: 기존 `day_verify.py` verify 로직 재사용
- LLM max_tokens: 512, Timeout: 180s

**Edge cases**:
- LLM이 이미 추출된 fact를 중복 출력 → `_dedup_post_norm`에서 제거
- LLM이 hallucination 생성 → NLI verify에서 GROUNDED만 승인
- Full text가 5000자 초과 시 → `_split_atomic` 재사용, chunk별 수행

---

## 3. 구현 위치 상세

`extract_llm.py` 내 `_normalize_freeform_pipeline()`:

```python
def _normalize_freeform_pipeline(facts: list[dict]) -> list[dict]:
    if not facts:
        return facts
    before = len(facts)

    # 기존 EDC 파이프라인
    facts = _group_entities(facts, field="subject")
    facts = _group_entities(facts, field="object")
    for f in facts:
        _normalize_predicate(f)
    facts = _group_predicates(facts)
    facts = _dedup_post_norm(facts)

    # ── NEW: Post-processing ──
    facts = _expand_multi_value(facts)      # Step 1
    facts = _split_qualifiers(facts)        # Step 2
    facts = _dedup_post_norm(facts)         # 재중복제거
    # ──────────────────────────

    print(f"    Post-process: {len(facts)} facts (after expansion+qualifiers)")
    return facts
```

`post_extract_supplement.py` (별도 신규 파일):
```python
# day_cycle.sh에서 re-embedding 직전 호출
# python3 post_extract_supplement.py [--turn-id <uuid> | --batch]
```

---

## 4. 서버 리소스 검증

| 항목 | Step 1 (Multi-value) | Step 2 (Qualifier) | Step 3 (Supplement) |
|---|---|---|---|
| **LLM 호출** | **0** | **0** | turn당 **1회** |
| **CPU 시간** | <1ms/fact | <1ms/fact | ~100s/turn |
| **추가 메모리** | <1MB | <1MB | ~500MB (LLM context) |
| **900s timeout 영향** | 없음 | 없음 | 별도 스크립트로 분리 |
| **서버 idle 활용** | 해당 없음 | 해당 없음 | ✅ 가능 (load 0.31) |

**Step 3 LLM 비용 상세**:
- Prompt: full text(~927ch=250tok) + facts(~200tok) + instruct(~100tok) = ~550tok
- Prompt processing: 550 / 13.4 tok/s = **41s**
- Generation: 3-5 facts × ~100tok = 300-500tok → 300/3.15 ~ 500/3.15 = **95-159s**
- Total: **136-200s** per turn

---

## 5. 데이터 흐름도

```
[원본 텍스트]
    ↓ _split_atomic(600)
[Chunk 1] [Chunk 2]
    ↓ LLM extract (각 chunk별 4 facts max)
[10 raw facts]
    ↓ _normalize_freeform_pipeline
    ├── _group_entities (subject)
    ├── _group_entities (object)
    ├── _normalize_predicate
    ├── _group_predicates
    ├── _dedup_post_norm
    ├── NEW: _expand_multi_value ──── ① "increased X and Y" → 2 facts
    ├── NEW: _split_qualifiers ─────── ② "503 errors for 12%" → qualifier 분리
    └── NEW: _dedup_post_norm (재실행)
[10~15 정규화된 facts]
    ↓ NLI verify
    ↓ NLI refine
[10~15 GROUNDED facts]
    ↓ [DB 저장]
    ↓ (day cycle: re-embedding 직전)
    ├── NEW: post_extract_supplement.py ──── ③ LLM 호출 1회, 누락 fact 보완
    └── NEW: NLI verify (신규 fact만)
[12~18 최종 facts]
    ↓ re-embedding
```

---

## 6. 구현 순서

| Phase | 내용 | 파일 | 예상 라인 |
|---|---|---|---|
| **Phase 1** | `_expand_multi_value()` 구현 | `extract_llm.py` | ~60줄 |
| **Phase 1** | `_split_qualifiers()` 구현 | `extract_llm.py` | ~50줄 |
| **Phase 1** | `_normalize_freeform_pipeline`에 통합 | `extract_llm.py` | ~5줄 |
| **Phase 2** | `post_extract_supplement.py` 신규 작성 | `scripts/pipelines/` | ~150줄 |
| **Phase 2** | `_nli_verify_batch()` 재사용 | `extract_llm.py` | ~20줄 |
| **Phase 2** | day_cycle.sh에 Step 3 통합 | `day_cycle.sh` | ~5줄 |
| **Test** | Unit test (multi-value, qualifier regex) | `tests/` | ~30줄 |
| **Test** | E2E pipeline integration (DB → extract → post-process → verify) | `test_edcr_e2e.py` 패턴 재사용 | 기존 활용 |

---

## 7. 리스크 및 완화

| 리스크 | 영향 | 완화 |
|---|---|---|
| `and` 분할 오류 (false positive) | 잘못된 fact 생성 | `_dedup_post_norm`에서 NLI GROUNDED만 유지 |
| Qualifier regex over-matching | Object 훼손 | Regex priority 순서 + 최소 매칭 |
| Step 3 LLM hallucination | 허위 fact 제안 | NLI verify 필터링 (기존 검증 재사용) |
| Step 3 LLM timeout | 누락 fact 미발견 | Timeout 180s + `_call_with_8082_retry` 재시도 |

---

## 8. Review — 2026-07-06

### Web 검증 결과

| Source | 내용 |
|--------|------|
| arXiv 2404.15604 (Hybrid LLM/Rule-based) | LLM 출력에 규칙 기반 post-processing 추가는 extraction pipeline의 **표준 패턴**으로 확인 |
| Nature Communications 2024 (Structured IE) | "LLMs can learn implicit normalization rules, but post-processing is still needed for consistency" |
| John Snow Labs (Precision Extraction) | Generative LLM의 limited auditability 문제 → regex post-processing이 auditability 제공 |
| PMC11398444 (LLM-AIx, Nature protocol) | 4-stage pipeline 정의 중 **output evaluation** 단계가 supplement와 개념적으로 일치. 단, evaluation은 별도 단계로 분리 (inline 아님) → 이 plan의 "offline step" 설계는 정합 |
| Inference 최적화 문헌 (다수) | CPU single-server ARM 환경에서 inference는 본질적으로 **sequential**. Port 경합은 실질적 리스크 |

### 발견된 Gaps

| Gap | 상세 | 심각도 | 처리 방안 |
|-----|------|--------|-----------|
| `_infer_predicate()` 누락 | Phase 1 pseudocode에서 호출만 있고 정의 없음 | 중 | 별도 함수 말고 분리된 fact를 `_normalize_predicate` + `_group_predicates`에 재진입시키는 방식 채택 |
| Step 3 pipeline 위치 모호 | "re-embedding 직전"이라고 했으나, supplement 생성 fact는 verify → enrich를 다시 통과해야 함 | 중 | `enriched` → `embedded` 사이가 아니라 `extracted` 상태 turn에 대해 **verify 직전**으로 변경 |
| Step 3 생성 fact enrich 누락 | plan에서 verify만 수행, enrich 생략 | 중 | supplement 후 pipeline이 verify → enrich → embed를 자연스럽게 통과하도록 설계 |
| 8082 포트 경합 | Step 3가 8082 점유 시 day_verify/day_enrich 불가 (병렬 불가) | 중 | Dedicated port 분리 또는 skip-if-busy 로직. 전자가 구조적으로 깔끔 |
| 50 turn = 2-3h 총 소요 | day_cycle.sh budget gate (30분) 초과 | 중 | Batch당 10 turn 제한. budget gate 진입 전 supplement만 먼저 수행 |

### Golden Set 부적합 판정

**원래 안건**: regex 패턴 검증용 golden set unit test
**web 검증 결과 — 3개 소스 일관**:

| Source | 결론 |
|--------|------|
| Techment (7 Proven Strategies) | "Risk of overfitting — Models may perform well on curated tests but fail in broader contexts" |
| The Neural Base (Automated Regression Testing) | "The most damaging mistake: using your current extraction pipeline's output as the golden truth" |
| Heavy Thought (Golden Sets for Probabilistic Systems) | Golden set은 **CI gate용 회귀 감지** 도구 — 결정적 코드의 정확도 측정에 부적합 |

**이 코드베이스의 실제 경험** (`handover.yaml`):
```
Phase 1 test (14B Q4):
  A) no-GS:  GOOD, 5 facts, 216s, 499P+281C
  B) with-GS: MIXED, 5 facts (1 dup), 257s, 1489P+316C
  → GS 3x token cost, quality benefit 불명확
```

**결론**: post-processing은 **결정적 함수**(regex)라 golden set의 비결정적 LLM 회귀 탐지 목적과 부합하지 않음. golden set 유지보수비만 증가시킴. 대신 E2E pipeline test(`test_edcr_e2e.py` 패턴)로 NLI GROUNDED pass rate 비교로 대체.

### 최종 판단

**Phase 1 (regex post-processing)** — **APPROVED**
- `_expand_multi_value` + `_split_qualifiers` 구현 가능. LLM-free 검증 완료.
- `_infer_predicate()`는 신규 함수 금지. 분리된 fact를 `_normalize_predicate` + `_group_predicates` 재진입으로 해결.
- golden set 금지. E2E pipeline integration test (`test_edcr_e2e.py` 패턴 재사용)로 NLI GROUNDED pass rate 비교.
- regex 자체는 결정적 함수이므로 unit test 2-3개 (edge case만) + 코드 리뷰로 충분.

**Phase 2 (LLM supplement)** — **CONDITIONAL APPROVED**
해결 조건:
1. `post_extract_supplement.py` 실행 시점을 `enriched→embedded` 사이 → `extracted→verify` 직전으로 변경
2. **Batch 제한**: 10 turn/회, budget gate 진입 전 조기 수행
3. **Port 경합 해소**: 전용 inference port 할당 또는 supplementary 전용 `_call_with_8082_retry`에서 8082 사용 시 verify가 우선이면 skip
4. supplement가 생성한 fact는 `extracted` 상태로 저장 → verify → enrich → embed pipeline 정상 통과
