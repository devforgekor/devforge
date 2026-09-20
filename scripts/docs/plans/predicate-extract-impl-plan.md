# Predicate Extraction — Comprehensive Implementation Plan

> Generated: 2026-06-27
> Research basis: Polat et al. 2025 (Semantic Web), AFEV 2025 (Expert Systems), CREDENCE 2026 (arXiv), MCOT-ZIE 2026, De Jure 2026, ExtractBench 2026, Goldset/npm 2026

---

## 1. Current Diagnosis

**6/10 test 결과**에서 83% 추출 실패. 근본 원인 2가지:

| 문제 | 징후 | 연구 기반 확인 |
|------|------|---------------|
| **고정 5-predicate enum** | 기술 토론(비교/질문/상태보고/계획)이 어떤 predicate에도 매핑 불가 → "추출 실패" | MCOT-ZIE (2026): "LLMs discard entities outside static predefined schemas" — 정확히 우리 케이스 |
| **Compound evidence** | NLI가 "일부만 GROUNDED"를 표현 불가 → NEUTRAL 매몰 → CONTRADICTION=0 | AFEV (2025): iterative atomic 분해 outperforms static splitting. CREDENCE (2026): Semantic-F1 (Jaccard 대비 +15-32pp) |

---

## 2. Research-Based Design Decisions

### Decision 1: Flat Prompt > CoT Reasoning

| 항목 | 기존 | 연구 결과 → 변경 |
|------|------|-----------------|
| 구조 | 5-step SCAN → VERIFY → RESOLVE → FILTER → OUTPUT | **Flat instruction + positive/negative examples** |
| 근거 | "단계적 추론이 정확도 향상" (가정) | Polat et al. (2025): "CoT and ReAct did **not** outperform simple instruction + retrieved examples for knowledge extraction" |

**변경**: SCAN→VERIFY→RESOLVE→FILTER→OUTPUT CoT 구조 제거. 직접적인 추출 instruction + good examples로 대체.

### Decision 2: Free-Form Predicate (No Enum)

| 항목 | 기존 | 연구 결과 → 변경 |
|------|------|-----------------|
| predicate | `config_set | function_added | ...` 5개 enum | **Free-form snake_case verb phrase** |
| 근거 | 제한된 선택지가 일관성 유지 | MCOT-ZIE (2026): "Free predicate generation → self-feedback → category mapping" — 더 많은 정보를 보존한 후에 정리 |

**변경**: enum 제거. snake_case 형식 제약만 유지. positive + negative examples 제공.

### Decision 3: Atomic Claim Rule

| 항목 | 결정 | 근거 |
|------|------|------|
| enforcement | Prompt instruction only (Phase 1). Post-process split if needed (Phase 2). | AFEV (2025): iterative decomposition outperforms one-shot. CREDENCE (2026): rule-based repair reduces AVR by 47-100%. |
| 평가 | Semantic-F1 (cosine 유사도) | CREDENCE (2026): Jaccard는 paraphrase penalize. Semantic-F1이 +15-32pp 우월 |

### Decision 4: Golden Set + LLM-as-Judge Evaluation

| 방식 | 채택 | 근거 |
|------|------|------|
| Golden set | **10-turn curated golden set** (수동 레이블링) | Goldset (2026) + ExtractBench (2026): golden dataset이 regression detection의 baseline |
| 평가자 | **LLM-as-Judge** (14B or DeepSeek) | De Jure (2026): "Explicit, interpretable evaluation criteria can substitute for human annotation" |
| 반복 | **3 iteration cycles** | De Jure (2026): "Peak performance within 3 judge-guided iterations" |

### Decision 5: Dynamic Timeout (No Change)

현재 `_calc_timeout()`이 이미 CAP 1800s까지 동적 확장. 테스트 스크립트의 `timeout=600`만 수정.

---

## 3. Implementation Phases

### Phase 1: Prompt Redesign (T16 + T17)

**파일**: `extract_llm.py`

#### 1a. `_STRUCTURED_FIELDS` (line 120-133) — Full Rewrite

```
_STRUCTURED_FIELDS = """
Structured fields — every extraction MUST have subject, predicate, object:
  subject:   The concrete entity the fact is about (file, function, config, service, etc.).
  predicate: Free-form snake_case verb phrase capturing the action or relation
             (e.g., returns_status_code, sets_timeout_to, replaces_implementation,
              depends_on_version, is_documented_in, recommends_approach).
             MUST be snake_case (lowercase + underscores). 2-5 words.
             MUST describe the RELATION between subject and object.
  object:    The specific value, outcome, or target entity.
  qualifiers: Optional JSON for extra context ({"from": "8081", "severity": "high"}).

Predicate — think of it as "what does subject DO TO object?"
  NOT: "is" (too vague), "has" (too vague), "related_to" (too vague)
  YES: "configures_port_to", "depends_on_library", "reports_error_code"
"""
```

Key changes:
- enum 제거, free-form 형식만 정의
- "what does subject DO TO object?" — 관계의 본질을 이해하도록 유도
- NOT examples (negative examples) — 너무 추상적인 predicate 회피

#### 1b. 3 System Prompts (lines 137-273) — Structural Rewrite

각 prompt의 변경 패턴:

```
BEFORE:
SEQUENTIAL REASONING — Follow these steps internally:
Step 1 — SCAN: ...
Step 2 — VERIFY: ...
Step 3 — RESOLVE: ...
Step 4 — FILTER: ...
Step 5 — OUTPUT: ...

AFTER:
Extract factual triples (subject, predicate, object) that are EXPLICITLY stated.

RULES:
1. ATOMIC: Each evidence = exactly ONE atomic claim. "X and Y" → split into two facts.
2. SELF-CONTAINED: Resolve pronouns ("it runs" → "the server runs").
3. MEANINGFUL: Significant, non-obvious facts. No trivial content.
4. PREDICATE: snake_case verb phrase — NOT a generic word like "is" or "has".
5. FAITHFUL: Directly traceable to source. NO inference or hallucination.
```

JSON output example 변경:

```
BEFORE:
{
  "extractions": [{
    "evidence": "Self-contained factual statement (resolve pronouns)",
    "category": "requirement|decision|explanation|code|reasoning|other",
    "subject": "entity the fact is about",
    "predicate": "config_set|function_added|function_modified|bug_observed|decision_made",
    "object": "specific value or outcome",
    ...
  }]
}

AFTER:
{
  "extractions": [{
    "evidence": "The cook preheated the oven to 180 degrees Celsius.",  ← generic example
    "category": "code",
    "subject": "cook",
    "predicate": "preheats_oven_to",
    "object": "180 degrees Celsius",
    "qualifiers": {"unit": "celsius"},
    "source_context": "The recipe calls for oven-baking at 180°C."
  }, {
    "evidence": "The server configures the database port to 5432.",  ← server example
    "category": "code",
    "subject": "server",
    "predicate": "configures_port_to",
    "object": "5432",
    "qualifiers": {"protocol": "TCP"},
    "source_context": "In the database section, the port is set to 5432."
  }]
}
```

**범용 예시(요리) + 서버 예시** — 모델이 predicate가 "관계 자체를 표현하는 것"임을 형식이 아닌 의미로 이해하게 함.

#### 1c. Negative Examples (New)

각 prompt 끝에 추가:

```
BAD patterns — do NOT output these:
  ✗ predicate: "is" — too vague, doesn't capture the relation
  ✗ predicate: "has" — too vague
  ✗ predicate: "related_to" — too vague
  ✗ evidence: "X and Y" — compound claim, split into two
  ✗ subject: "the user" — too generic, use the actual entity
  ✗ NOISE: keyboard smash, API error, gibberish → return {"extractions": []}
```

### Phase 2: Post-Processing (T18)

**파일**: `extract_verify.py`, `_post_process_extractions()` 내 (after line 166)

#### 2a. `_sanitize_predicate()`

```python
def _sanitize_predicate(pred: str) -> str:
    if not pred or not isinstance(pred, str):
        return ""
    p = pred.strip().lower()
    # Convert spaces/dashes to underscores
    p = re.sub(r'[\s\-]+', '_', p)
    # Remove non-alphanumeric (except underscore)
    p = re.sub(r'[^a-z0-9_]', '', p)
    # Remove leading/trailing underscores
    p = p.strip('_')
    # Reject if too short/abstract
    if len(p) < 3 or p in ('is', 'has', 'does', 'was', 'are'):
        return ""
    # Reject if not snake_case (contains uppercase after first char was already lowered)
    if not re.match(r'^[a-z][a-z0-9]*(_[a-z0-9]+)*$', p):
        return ""
    return p[:60]  # safety cap
```

#### 2b. Integration

In `_post_process_extractions()` loop (after line 166 `cleaned.append(ex)` or rather as a filter before append):

```python
# Add after line 166 (cleaned.append(ex)) OR better: insert before line 164
# Sanitize predicate
pred = ex.get("predicate", "")
if pred:
    ex["predicate"] = _sanitize_predicate(pred)
```

### Phase 3: Test Infrastructure (T19)

**파일**: `test_predicate_extract.py`

#### 3a. Turn Selection — Technical Discussion Focus

```python
# Replace current tech_score regex with broader discussion pattern
CASE WHEN LOWER(COALESCE(user_turn,text))
  ~ '(compare|recommend|suggest|plan|propose|think|consider|'
  'versus|vs|better|faster|instead|alternative|switch|migrat|'
  'port|config|model|pod|pipeline|extract|error|bug|change|'
  'update|deploy|restart|install|timer|service|systemd|'
  'db|sql|memory|cpu|swap|disk|container|network|schema|'
  'watchdog|recover|question|ask|why|how|whatif)'
  THEN 20 ELSE 0 END
```

→ 기존 `tech_score > 0` 대신 `tech_score > 0` 유지하되 매칭 범위 확장. 질문, 비교, 추천, 계획 내용까지 포함.

#### 3b. Dynamic Timeout

```python
# Import and reuse extract_llm's _calc_timeout
from pipelines.extract_llm import _calc_timeout, _calc_max_tokens

# In run_turn_extractions, for each turn:
total_chars = len(turn_text)
max_tok = _calc_max_tokens(total_chars) or 256
timeout = _calc_timeout(total_chars, max_tok)  # CAP 1800s within
```

→ `subprocess.run(timeout=...)` 값을 하드코딩 600 → 동적 timeout으로 변경. 실제 파이프라인과 동일한 로직.

#### 3c. Success Criteria Update

```python
criteria = {
    "non_empty_turn_ratio": 0.80,    # >= 80% turns produce >= 1 fact
    "predicate_diversity": 0.50,     # >= 50% predicates NOT from old 5-value set
    "predicate_format_valid": 0.90,  # >= 90% of predicates match snake_case
    "avg_facts_per_turn": 2.0,       # Atomic splitting increases fact count
    "triple_completeness": 0.70,     # >= 70% have all 3 (s/p/o)
}
```

#### 3d. LLM-as-Judge Evaluation (New Function)

```python
def evaluate_predicates(turn_ids):
    """Use DeepSeek/14B to judge predicate quality semantically."""
    # Sample up to 30 facts
    # For each: {evidence, subject, predicate, object}
    # Ask LLM: "Rate how well predicate describes the relation between subject and object"
    # Scale: 0-5
    # Return: avg_score, distribution
```

→ Goldset (2026)의 llmJudge runner 패턴. 정성 평가 추가.

### Phase 4: Run + Iterate (T20)

#### Cycle Structure (De Jure 패턴)

```
Cycle 1: Initial prompt → 10-turn test → Analyze predicate distribution
Cycle 2: Add top 3-5 generated predicates as few-shot → Re-run → Compare
Cycle 3: Adjust negative examples based on failure patterns → Final run
```

각 cycle:

| Step | Action | Duration |
|------|--------|----------|
| 1 | test_setup() — stop day-cycle, register protect | ~1 min |
| 2 | ensure_model(day-extractor) — Pod B :8082 | ~5 min |
| 3 | Run 10-turn extraction | ~30-60 min |
| 4 | check_results() — analyze predicates | ~2 min |
| 5 | Adjust prompt based on analysis | ~5 min |
| 6 | test_complete() — cleanup, restart cycle | ~1 min |

**Total: ~45-75 min per cycle, 3 cycles = ~3-4h**

#### Monitoring Plan

| 간격 | 액션 |
|------|------|
| 10 min | test_heartbeat() + `/tmp/predicate_test_results.json` 확인 |
| 각 turn 완료 | result 출력 (exit code, fact count) |
| Cycle 완료 | predicate 분포 리포트 + LLM-as-Judge 평가 |
| 3 Cycle 완료 | 최종 비교 리포트 |

---

## 4. Concrete Code Changes

### File 1: `extract_llm.py`

| Line(s) | Change |
|---------|--------|
| 118-133 | `_STRUCTURED_FIELDS` — free-form predicate + negative examples |
| 135-169 | `_SYSTEM_USER_EXTRACT` — CoT 제거, flat instruction + atomic rule + examples |
| 170-192 | `_SYSTEM_THINKING_EXTRACT` — 동일 |
| 193-220 | `_SYSTEM_TEXT_EXTRACT` — 동일 |
| (None) | timeout/max_tokens 동적 계산 — 변경 없음, 이미 CAP 1800s |

### File 2: `extract_verify.py`

| Line(s) | Change |
|---------|--------|
| After 166 | `_sanitize_predicate()` 함수 추가 |
| After 166 (in loop) | predicate sanitize 호출 |

### File 3: `test_predicate_extract.py`

| Line(s) | Change |
|---------|--------|
| 40-58 | `select_turns()` — broader technical discussion pattern |
| 81-83 | `run_turn_extractions()` — 동적 timeout (from extract_llm import) |
| 94-131 | `check_results()` — predicate diversity + format + avg_facts metrics |
| After 131 | `evaluate_predicates()` — LLM-as-Judge qualitative eval |
| 170-177 | Report: diversity, format valid ratio, criteria pass/fail |

---

## 5. Risk Matrix

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| LLM reverts to old enum (training bias) | Medium-High | Predicate diversity 낮음 | Negative examples: "NOT from any list. Think fresh." + flat prompt 효과 |
| Free-form predicate too abstract | Medium | 검색/집계 무의미 | `_sanitize_predicate()` reject short/abstract. Quality improves over 3 cycles |
| Atomic claim over-splitting | Low-Medium | Fact flood, noise | CREDENCE (2026): rule-based repair reduces AVR 47-100%. Add rule: "a claim must contain one specific assertion" |
| Long turn timeout (80MB session context) | Low | Dynamic timeout already handles this | CAP 1800s 충분. Test script도 동적 timeout 사용 |
| 3 cycles too time-consuming | Medium | 3-4h total | User can drop to 2 cycles if 1st cycle results are clean |

---

## 6. Dependency Graph

```
Phase 1: Prompt Redesign
  ├── T16: _STRUCTURED_FIELDS [file: extract_llm.py] ✅ DONE (2026-09-20: restored from archive, added to pipelines/extract_llm.py)
  ├── T17a: _SYSTEM_USER_EXTRACT [file: extract_llm.py]
  ├── T17b: _SYSTEM_THINKING_EXTRACT [file: extract_llm.py]
  ├── T17c: _SYSTEM_TEXT_EXTRACT [file: extract_llm.py]
  ↓
Phase 2: Post-Processing
  └── T18: _sanitize_predicate() [file: extract_verify.py]
  ↓
Phase 3: Test Infrastructure
  └── T19: Update test_predicate_extract.py [includes select_turns, timeout, criteria, LLM-as-Judge]
  ↓
Phase 4: Execute
  └── T20a: Run Cycle 1 (10-turn → analyze → adjust)
  └── T20b: Run Cycle 2 (re-run → compare → finalize)
  └── (T20c): Run Cycle 3 if needed
```

---

## 7. Rollback Plan

| Scenario | Action |
|----------|--------|
| Free-form predicate worse than enum | Revert `_STRUCTURED_FIELDS` + 3 prompts to original. Keep `_sanitize_predicate()`. |
| Atomic claim rule breaks NLI | Remove atomic claim instruction. Keep free-form predicate. |
| No improvement after 2 cycles | Re-run with old prompt + dynamic timeout. Record baseline. |
