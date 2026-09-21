# 챗 대화 추출(Conversation Extraction) 연구 보고서
## DevForge 서버 적용 방안

**작성일**: 2026-06-09
**목적**: LLM 챗 대화 로그에서 구조화된 정보 추출 최적 방법론 연구 및 ARM 4코어 환경 적용 방안

---

## 1. 업계 연구 결과 요약

### 1.1 소형 모델(7B~14B) 구조화 추출 성능

| 출처 | 핵심 발견 |
|------|----------|
| **AscentCore Small LLM Benchmark** (2026.04) | Llama 3.1 8B Q8_0: JSON Parse 100%, Schema Compliance **95.7%** — 7B 체급 최고. Qwen 2.5 7B: Schema 73.9%, 속도 대비 품질 최적. 14B는 7B 대비 미미한 향상에 비해 비용 증가 폭 큼 |
| **StruXGPT (NeurIPS 2024)** | Qwen-7B: AppEval +3.6, 0% FormatError. Qwen-14B: AppEval +3.8 (차이 미미). **7B가 structured extraction의 sweet spot** |
| **StructFlowBench** (Multi-turn 대화) | Qwen2.5-7B가 Refinement 과제에서 14B보다 우수 (0.76 vs 0.73). 대화 흐름 추적엔 체급보다 instruction following이 중요 |
| **NuExtract (7B)** | Fine-tuned extraction 전용 모델. GPT-4o와 동등 (0.74 vs 0.73). Extract-only 설계로 hallucination 없음 |
| **SLOT (Structured LLM Output Transformer)** | Mistral-7B + constrained decoding → 99.5% schema accuracy. 심지어 Llama-3.2-1B도 SLOT만 붙이면 대형 모델과 동급 |

**결론**: 대화 추출 baseline으로 7B Q8_0면 충분. 14B로 올라가는 건 Arm 4코어 환경에선 비용 대비 효과가 낮음.

### 1.2 Extract → Verify 파이프라인 (Production Proven)

| 연구 | 구조 | 성과 |
|------|------|------|
| **VeReaFine (ACL 2025)** | Extract(7B) → Verify(8B) → Refine → loop (max 3회) | 26% 오류 감소, 32B급 성능을 7B로 달성 |
| **AUTOSUMM (ACL 2025 Industry)** | Dynamic segmentation → thematic tracking → multi-layer hallucination detection (syntactic + semantic + entailment) | **94% factual consistency**, 89% zero-edit |
| **HALT-RAG (2025)** | Extract → NLI Ensemble verify + Abstention | F1 0.78(summary), 0.98(QA), 0.74(dialogue) |
| **Multi-Agent Debate (IEEE 2025)** | Claim detection → evidence retrieval → multi-agent cross-verification | Robust, but 4-9x cost |

**결론**: Extract → Verify 2-stage가 가장 실전 검증됨. 3단계 이상은 수익성이 낮음.

### 1.3 Multi-Agent Debate / PRJ 패턴

| 연구 | 구조 | 특징 |
|------|------|------|
| **MACA (Meta AI / Columbia, ICLR 2026)** | Debate signal → RL reward → single model internalization | +27.6% self-consistency. **Debate 품질을 단일 모델로** |
| **ARGUS (Production, 2026)** | Moderator + Specialist + Refuter + Jury → Bayesian aggregation | Provenance tracking, PROV-O 호환 |
| **DebateCV (WWW 2026)** | 2 Debaters + Moderator → Debate-SFT training | Claim verification 전용 |
| **SDI + EWSC (2026)** | Confidence-triggered selective debate | Token 소모 **50% 감소** |

**결론**: 순수 PRJ(3개 모델 토론)는 생산 비용 대비 효과 논란. Debate signal을 단일 모델에 증류(MACA)하거나, confidence threshold로 선택적 debate(SDI)이 실전형.

---

## 2. DevForge 서버 현황 분석

### 2.1 하드웨어 제약

| 항목 | 값 | 영향 |
|------|-----|------|
| CPU | ARM Neoverse-N1, **4코어** | 추론 속도 bottleneck |
| RAM | 22Gi + 4G zram + 12G swap | Q8_0 7B 모델 1개는 가능, 2개 동시 실행은 무리 |
| GPU | **없음** | CPU 추론만 가능 (llama.cpp) |
| 추론 속도 | ~2-3 t/s (7B Q8_0) | 512 token 응답에 **~170-256초** |
| Pod 구조 | Pod B 단독, swap 방식 | 모델 전환 시 60-144s 로딩 시간 |

### 2.2 Ground Truth Test 결과 (2026-06-09)

```
P (Mistral 7B Instruct Q8_0): 평균 56% — false positive 심각
R (Qwen 2.5 7B Instruct Q8_0): 평균 92% — 가장 안정적  
J (Llama 3.1 8B Instruct Q8_0): 평균 28% — 3/5 timeout (300s)

속도: P ~150s/case, R ~95s/case, J ~190-302s/case
```

### 2.3 가능한 모델 옵션

| 모델 | 크기 | RAM | ARM 추정 t/s | 실전 가능? |
|------|------|-----|-------------|-----------|
| Qwen 2.5 Coder 3B Q8_0 | 3.2G | 낮음 | ~6 t/s | **현행 extract** |
| Qwen 2.5 Coder 7B Q8_0 | 7.2G | 중간 | ~3 t/s | verify 전용 확정 |
| Qwen 2.5 7B Instruct Q8_0 | 7.2G | 중간 | ~3 t/s | R 역할 검증 완료 |
| Mistral 7B Instruct Q8_0 | 7.2G | 중간 | ~2.5 t/s | P 역할 → 교체 권장 |
| Llama 3.1 8B Instruct Q8_0 | 8.0G | 중간 | ~2 t/s | **ARM에서 timeout 위험** |
| Qwen 2.5 14B Instruct Q4_K_M | 8.2G | 중간 | ~1 t/s | 14B Q4 = 7B Q8과 RAM 비슷하나 속도↓ |
| Gemma 2 27B Q4_K_M | 15G | 높음 | ~0.5 t/s | **불가** (RAM 부족 + 너무 느림) |
| Qwen 2.5 32B Q4_K_M | 18G | 높음 | ~0.3 t/s | **불가** |

---

## 3. DevForge 적용 방안

### 3.1 현실적 판단

업계 연구는 "14B 단일 → 27B Verify"를 권장하나, **DevForge의 ARM 4코어 환경에서는 27B/32B 모델이 물리적으로 불가능**합니다. 따라서 DevForge에 맞게 조정된 2가지 현실적方案을 제안합니다.

### 3.2 추천 구조: Hybrid Extract → 7B Verify

```
[1단계 Extract]  →  [2단계 Verify]
  Qwen Coder 3B     Qwen Coder 7B (확정)
  (항시 Pod B)      (day_verify 전용)
       │                    │
       │              MiniCheck hallucination
       │              threshold=0.3
       ▼                    ▼
  review_facts DB    py_verify 결과
```

**이미 확정된 부분**: extract=3B Q8, verify=Coder 7B Q8.
**변경 권장**: P/R/J 3단계 PRJ 대신 **2단계 extract → verify**로 단순화.

### 3.3 P/R/J 폐지 근거

| 근거 | 설명 |
|------|------|
| 속도 | PRJ 3단계 = ~500s/case. Extract → Verify = ~200s/case. **60% 단축** |
| 하드웨어 | PRJ는 모델 3회 swap 필요 (60-144s × 3 = 180-432s 손실) |
| 품질 | Qwen 7B R(92%)은 우수하나, P(56%, Mistral)와 J(28%, Llama timeout)가 약함 |
| 업계 증거 | StruXGPT: 7B 단일로도 충분. SLOT: constrained decoding으로 99.5%. NuExtract 7B: GPT-4o급 |

### 3.4 그래도 PRJ가 필요하다면: 7B PRJ → MiniCheck Verify

속도를 희생하더라도 정밀도가 중요한 domain(예: 보안/법률 상담)이라면:

```
P (Qwen 2.5 7B Instruct) ─→ findings
                                ↓
R (Qwen 2.5 7B Instruct) ─→ verdicts  
                                ↓
J (Qwen 2.5 7B Instruct) ─→ scores
                                ↓
                         MiniCheck Verify (hallucination)
```

**변경 사항**:
- P: Mistral 7B → **Qwen 2.5 7B Instruct** (false positive 개선 기대)
- R: Qwen 2.5 7B Instruct — 유지 (92% 검증 완료)
- J: Llama 3.1 8B → **Qwen 2.5 7B Instruct** (timeout 문제 해결)
- Verify: MiniCheck (flan-t5-large) — 유지

**예상 속도**: case당 P~150s + R~95s + J~150s + MiniCheck~5s = ~400s (7분)
**예상 정확도**: 70-80% (Mistral P의 false positive만 개선돼도 큰 폭 상승)

### 3.5 추가 최적화: Chunking + TokenBudget

Ground truth test는 full context를 사용했으나, 실제 대화 로그는 더 깁니다. Phase B에서 예정된 최적화:

```python
# pipeline_common.py TokenBudget 적용
budget = TokenBudget(max_tokens=4096, reserve_ratio=0.3)
context = build_context(facts, budget=budget)  # priority filtering
```

효과:
- 긴 대화에서 최신/고priority fact만 전달 → 관련성 ↑, hallucination ↓
- LLM input 토큰 감소 → 속도 향상
- NuExtract-style: extract-only로 hallucination 원천 차단

---

## 4. 권장 로드맵

| Phase | 내용 | 기간 | 기대 효과 |
|-------|------|------|----------|
| **Phase A (즉시)** | day_pipeline.py: P/R/J → Extract → day_verify 로 전환. classify.py PRJ 제거 | 1일 | 불필요한 swap 제거, 속도 60% 개선 |
| **Phase B** | Chunking + TokenBudget 적용 (classify.py → pipeline_common.py) | 2일 | 대화 맥락 유지 + hallucination 감소 |
| **Phase C** | day_runner.py: Pod B swap 로직 추가 (day verify용 Coder 7B) | 1일 | day/night 분리 기반 |
| **Phase D** | SLOT-style constrained decoding 도입 검토 | 연구 후 결정 | Schema compliance 99%+ |

---

## 5. 참고 문헌

- [StruXGPT (NeurIPS 2024)](https://papers.nips.cc/paper_files/paper/2024/file/f169ec4d47933ea4896b994af8ff4f17-Paper-Conference.pdf) — 7B vs 14B structured extraction
- [AscentCore Small LLM Benchmark (2026)](https://ascentcore.com/2026/04/01/small-llm-performance-benchmark/) — 22개 소형 모델 JSON/구조화 비교
- [NuExtract (HuggingFace)](https://huggingface.co/numind/NuExtract) — 7B extraction 전용 모델, GPT-4o 동등
- [VeReaFine (ACL 2025)](https://aclanthology.org/2025.bionlp-share.34/) — Extract → Verify → Refine 3-stage pipeline
- [AUTOSUMM (ACL 2025 Industry)](https://aclanthology.org/2025.acl-industry.35/) — 94% factual consistency in regulated domains
- [MACA (Meta AI / Columbia, ICLR 2026)](https://github.com/facebookresearch/maca) — Debate signal → single model distillation
- [ARGUS Debate Framework (PyPI)](https://pypi.org/project/argus-debate-ai/) — Production multi-agent debate
- [HALT-RAG (2025)](https://huggingface.co/papers/2509.07475) — Post-hoc hallucination detection with NLI ensembles
- [DevForge Ground Truth Test (2026-06-09)](file:///opt/projects/server/scripts/prj_ground_truth_test.py) — 자체 P/R/J 3모델 비교 테스트 결과
