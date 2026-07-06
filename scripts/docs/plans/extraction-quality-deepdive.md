# Extraction Quality Deep Dive

**Date:** 2026-07-06
**Context:** Post 600-char chunk size optimization (`_split_atomic` default 400→600).
**Test data:** 1800-char English + 1371-char Korean, Qwen3-8B-Q8_0 on llama.cpp.

---

## 1. Current State

### 1.1 Phase 1 Extraction (1800-char English, 600-char chunks)

| Category | GT facts | Phase 1 caught | Recall | Notes |
|----------|----------|----------------|--------|-------|
| Server Arch | 6 | 0 | 0% | **User turn 전면 누락** |
| NLI/Hallucination | 5 | 2 | 40% | DeBERTa F1 0.53/0.60만, GPT-4o/R1/F1 0.57 누락 |
| Cascade Arch | 3 | 1 | 33% | Pre-processor fit만, Router-first/latency 누락 |
| GGUF Quantization | 11 | 4 | 36% | Q4_K_M 3개 + speculative 12%, Q6_K/Q5_K_M/1.1GB 누락 |
| Heartbeat/Bugs | 8 | 3 | 38% | heartbeat/active_contexts/timers summary, 개별 timer 값 누락 |
| **Total** | **33** | **10** | **30%** | |

### 1.2 Phase 2 Supplement (현재)

| Turn | Supplement facts | Novel | Near-dup | Novel rate |
|------|-----------------|-------|----------|------------|
| 1800-char English | 3 | 1 | 2 | 33% |
| 661-char English | 3 | 1 | 2 | 33% |

### 1.3 Hallucination Rate (Phase 1)

| Chunk size | Facts | Hallucinated | Rate |
|------------|-------|-------------|------|
| 400 | 8 | 1 | 12% |
| **600** | **10** | **0** | **0%** |

---

## 2. Problem Analysis

### 2.1 User Turn: Server Arch Facts 전면 누락

**Root cause:** User prompt (`_SYSTEM_USER_EXTRACT_FREE_8B`)와 text prompt가 거의 동일. 모두 "Extract factual triples from the USER MESSAGE / ASSISTANT RESPONSE"로 시작. User turn의 factual statements가 질문/요청 프레임에 묻혀서 LLM이 무시.

**User turn 구조:**
```
"I'd like a review..."              ← 요청 (0 fact)
"We have: Pod A DOWN, Pod B 3B Q8"  ← 사실 2개 (무시됨)
"The system has 22Gi RAM..."         ← 사실 1개 (무시됨)
"BGE-m3 uses 4.3GB..."              ← 사실 3개 (무시됨)
"For NLI: DeBERTa F1 0.53..."       ← 사실 4개 (추출됨)
"We are considering GPT-4o-mini..."  ← 의향 (0 fact)
```

→ 두 번째 단락("We have...")부터 여섯 번째 단락("For NLI...")까지는 **명백한 factual statements**지만, "I'd like..." 요청문 다음에 나와서 LLM이 "요청의 context"로 분류.

→ NLI facts만 추출된 이유: "For the NLI verification pipeline, we are using DeBERTa-v3 which currently achieves F1 0.53" — 이 문장은 "we are using... achieves"라는 declarative structure로 직접 fact를 표현. 반면 "We have two Pods: Pod A has been DOWN..."은 listing style.

### 2.2 Chunk-level Recall Ceiling ~30%

**Root cause:** 8B Q8 zero-shot extraction의 근본적 한계. Single-pass per chunk에서 LLM attention이 처음 1-2 facts에 집중되고 나머지를 놓침 ("lost-in-the-middle" 현상, Liu et al. 2023, arXiv:2307.03172).

**증거:** 600자 chunk 4개에서 각각 평균 2-3 facts만 추출. 각 chunk에는 4-6 facts가 있지만 LLM이 max 4 facts로 제한되어도 실제론 2-3개만 뽑음.

### 2.3 Supplement Near-dup Rate 50%+

**Root cause:** Dedup이 exact SPO string match (`_is_duplicate` at `post_extract_supplement.py:121`). Supplement LLM은 Phase 1과 다른 predicate 표현을 생성함. E.g. Phase 1: `(ETL pipeline, increased_to, 3 hours)` — Supplement: `(ETL pipeline, processing_time_increased_to, 3 hours)`. Predicate string이 다르므로 dedup miss.

**Note:** 이 분석은 1800-char English 단일 문서 기준 3건의 supplement facts로 추정한 수치이며, 통계적 일반화를 위해 최소 10-20건 샘플 재측정이 필요함.

---

## 3. Proposed Improvements

### 3.1 #1 User Prompt 강화 (Priority: HIGH, Cost: 10min, Effect: +2-3 facts/turn)

**Target:** `_SYSTEM_USER_EXTRACT_FREE_8B` at `extract_llm.py:159`

**Change:**
```
- "Extract factual (subject, predicate, object) triples from the USER MESSAGE."
+ "Extract factual (subject, predicate, object) triples from the USER MESSAGE.
+  Factual statements may be embedded within questions, descriptions, or requests —
+  extract them regardless of the surrounding conversational framing.
+  Skip speculative or hypothetical statements — only extract explicitly stated facts."
```

**Rationale:** User turn에 Server Arch facts 6개가 0% recall. Prompt에 질문/요청 속 factual content를 명시적으로 지시하면 LLM이 "listing style" statements도 fact로 인식.

**Risk:**
- False positive: "I'd like a review..." → "I'd like"를 fact로 오인 가능성
  - Mitigation: "Skip speculative or hypothetical" 문장 추가
- NLI facts regression: 현재 NLI facts(DeBERTa F1 0.53/0.60)는 잘 추출 중. Prompt 변경으로 방해받지 않도록 기존 구조 유지.

### 3.2 #2 Supplement Semantic Dedup (Priority: MEDIUM, Cost: 30min, Effect: +0.7 novel fact/turn)

**Target:** `_is_duplicate` at `post_extract_supplement.py:121`

**Change:** Replace exact SPO string match with semantic near-dup detection:

```python
_PRED_SYNONYMS = {
    'increased_to': {'increased_to', 'processing_time_increased_to', 'grew_to', 'rose_to'},
    'configured_to': {'configured_to', 'set_to', 'adjusted_to', 'tuned_to'},
    'disabled_during': {'disabled_during', 'failed_during', 'stopped_during'},
    'peaked_at': {'peaked_at', 'peaked', 'maxed_at'},
    'decreased_to': {'decreased_to', 'dropped_to', 'fell_to', 'reduced_to'},
}

def _entity_match(a: str, b: str) -> bool:
    """Check if two entity strings refer to the same entity."""
    a, b = a.lower().strip(), b.lower().strip()
    if a == b:
        return True
    if a in b or b in a:
        return True
    return False

def _value_match(a: str, b: str) -> bool:
    """Check if two object values match (fuzzy number comparison)."""
    a, b = a.lower().strip(), b.lower().strip()
    if a == b:
        return True
    # Extract numbers and compare
    nums_a = re.findall(r'[\d.]+', a)
    nums_b = re.findall(r'[\d.]+', b)
    if nums_a and nums_b and nums_a == nums_b:
        return True
    return False

def _is_semantic_dup(turn_id, subject, predicate, object_):
    existing = get_existing_facts(turn_id)
    for ef in existing:
        if not _entity_match(ef['subject'], subject):
            continue
        if not _value_match(ef['object'], object_):
            continue
        if ef['predicate'] == predicate:
            return True
        if predicate in _PRED_SYNONYMS and ef['predicate'] in _PRED_SYNONYMS.get(predicate, set()):
            return True
    return False
```

**Effect on 1800-char test (n=3, preliminary — 재측정 필요):**
- Current: 3 supplement facts → 1 novel (ETL pipeline dup, API gateway novel, SSL novel)
- With semantic dedup: 3 supplement facts → 2 novel (ETL pipeline filtered as dup of `increased_to`)
- Novel rate: 33% → 67%

### 3.3 #3 2-pass Extraction (Priority: LOW, Cost: +100% LLM time, Effect: +2-4 facts)

**Approach (masked re-extraction, inspired by lost-in-the-middle mitigation strategies):**
1. Pass 1: chunks → extract facts → normalize → dedup (현재 Phase 1과 동일)
2. Pass 2: 각 chunk에서 Pass 1 facts의 evidence span 마스킹 → 재추출
3. Pass 1 + Pass 2 합쳐서 full normalize/dedup

**Problem:**
- LLM time 2x: 2002s → ~4000s per turn. 4-core CPU server에서 비현실적.
- Phase 2 supplement(87s/turn)가 같은 목표를 더 효율적으로 달성 중.
- Evidence span masking: NLI evidence가 정확한 원문 substring이 아닐 수 있음 (LLM이 생성한 evidence는 원문과 정확히 일치하지 않음).

**Conclusion:** Phase 2 supplement를 개선하는 것이 2-pass 도입보다 실용적.

---

## 4. Comparison Matrix

| Dimension | #1 User Prompt | #2 Supp Dedup | #3 2-pass |
|-----------|---------------|---------------|-----------|
| **Problem solved** | User turn 0 fact | Supplement near-dup 50%+ | Recall ceiling 30% |
| **Novel facts/turn** | +2-3 | +0.7 | +2-4 |
| **LLM time increase** | 0% | 0% | +100% |
| **Code changes** | Prompt only (2 lines) | New module (~50 lines) | Core pipeline restructure |
| **Risk** | Low (prompt only) | Low (supplement only) | High (core pipeline) |
| **Regression risk** | Low (NLI regression unlikely) | None (only dedup) | High (pass 2 interference) |
| **ROI** | **Highest** | Medium | Lowest |

---

## 5. Recommendation

### Phase 1 (Now): User Prompt 강화

1. Add 2 lines to `_SYSTEM_USER_EXTRACT_FREE_8B` — "Factual statements may be embedded within questions..." + "Skip speculative statements"
2. Re-run 1800-char English A/B test → verify Server Arch facts recovery (threshold: 6 facts 중 ≥4 recovery = pass)
3. If positive → deploy

### Phase 2 (Next): Supplement Semantic Dedup

1. Implement `_is_semantic_dup` with predicate synonym map + entity/value fuzzy match
2. Re-run supplement → verify novel rate increase
3. If positive → deploy

### Phase 3 (Future): Re-evaluate 2-pass

Only if Phase 1 + 2 combined still insufficient. Monitor live recall after both improvements before deciding.

---

## 6. Appendices

### A. Test Data: 1800-char English Text

(생략 — test_chunk_ab.py 참조)

### B. Ground Truth (33 facts)

| ID | Subject | Predicate | Object | Category |
|----|---------|-----------|--------|----------|
| 1 | Pod A | status | DOWN for 3 days after OOM crash | Server |
| 2 | Pod B | model | 3B Q8 consuming 3.2GB RAM | Server |
| 3 | system | total RAM | 22Gi with 16Gi available | Server |
| 4 | BGE-m3 | RAM usage | 4.3GB | Server |
| 5 | removing BGE-m3 | frees disk | 12.5GB | Server |
| 6 | sentence-transformers FP16 | RAM usage | 800MB | Server |
| 7 | DeBERTa-v3 | current F1 | 0.53 | NLI |
| 8 | DeBERTa-v3-Large 304M | SciHal 2025 F1 | 0.60 | NLI |
| 9 | GPT-4o | SciHal 2025 F1 | 0.43 | NLI |
| 10 | DeepSeek-R1 | SciHal 2025 F1 | 0.49 | NLI |
| 11 | GPT-4o-mini distillation | expected F1 | 0.57 | NLI |
| 12 | Pre-processor pattern | fit | P-R-J fixed pipeline | Cascade |
| 13 | Router-first (AutoMix/Doorman/CSCR) | ruled out | requires restructuring | Cascade |
| 14 | Pre-processor | latency reduction | 35% in GSCP-Lite | Cascade |
| 15 | Q4_K_M vs Q8_0 | DRAM read reduction | 44% | GGUF |
| 16 | Q6_K vs Q8_0 | DRAM read reduction | 25% | GGUF |
| 17 | Q5_K_M vs Q8_0 | DRAM read reduction | 33% | GGUF |
| 18 | Q4_K_M | inference speed before | 3.5 tok/s | GGUF |
| 19 | Q4_K_M | inference speed after | 5.2 tok/s (48% gain) | GGUF |
| 20 | Q4_K_M | MMLU before | 72.3 | GGUF |
| 21 | Q4_K_M | MMLU after | 70.1 (2.2% drop) | GGUF |
| 22 | speculative decoding + Q4_K_M | throughput gain | 12% | GGUF |
| 23 | speculative decoding + Q4_K_M | total speed | 5.8 tok/s | GGUF |
| 24 | speculative decoding | MMLU loss | 0.8% | GGUF |
| 25 | 0.6B draft model | RAM usage | 1.1GB | GGUF |
| 26 | _build_heartbeat_blocks | bug | incorrectly marking test ports unhealthy | Heartbeat |
| 27 | _build_heartbeat_blocks | fix | port status filtering | Heartbeat |
| 28 | active_contexts | behavior | gates 30-min reporting to active contexts | Heartbeat |
| 29 | activity-summarizer | timer | KST 06:00 | Heartbeat |
| 30 | validate-gen | timer | KST 00:30 | Heartbeat |
| 31 | devforge-nightly | timer | KST 03:00 | Heartbeat |
| 32 | auto_mode.sh:238 | comment says | KST 02:00 | Heartbeat |
| 33 | auto_mode.sh:238 | actual timer | KST 03:00 | Heartbeat |

### C. Phase 2 Supplement Prompt (current)

(post_extract_supplement.py:34 참조 — `_SYSTEM_SUPPLEMENT`)
