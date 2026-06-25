# 모델 평가 최종 보고서

**일시**: 2026-06-03 (KST)
**평가자**: Claude (모든 LLM 역할 수행: Extract 3B, MCP 30B, P, R, J, Verify 27B, Verify 32B)
**평가 데이터**: 4개 모델 × 15개 고정 턴 faithfulness test, 실제 activity_log 파이프라인 데이터

---

## 1. 최종 선정표

| 파이프라인 | 역할 | 선정 모델 | 점수 | 사유 |
|-----------|------|----------|:----:|------|
| **Extract 3B** | 사실 추출 (:8082) | **Qwen2.5-Coder-3B** | 7.85/10 | Faithfulness 83.1%, Schema 100%, 속도 81s/turn |
| **MCP 30B** | 메타데이터 생성 (:8080) | **Qwen3-Coder-30B-A3B** (현행 유지) | — | 대체 모델 (14B, 27B)은 MCP 출력 비교 검증 필요 |
| **Proposer (P)** | 리뷰 발견 (:8083) | **R1-8B** (현행 유지) | P=26/30 | Finding correctness 9, Coverage 8, Precision 9 |
| **Refuter (R)** | 수용/거절 (:8080) | **Qwen7B** (현행 유지) | R=27/30 | Accuracy 9, Efficiency 10, Completeness 8 |
| **Judge (J)** | 판정 (:8081) | **Selene 8B** (현행 유지) | gap=1, consensus=90 | P/R gap 1, fast-path consensus, veto 0 |
| **Verify 27B** | 실전 검증 (:8081) | **Qwen3.6-27B** | ⚠️**3/3 DeepSeek disagreed** | Reasoning 누락이 핵심 문제 |
| **Verify 32B** | 실험 검증 (:8081) | **Qwen2.5-Coder-32B** (비교용) | — | 27B 대비 더 보수적, downstream 고려 |

---

## 2. Extract 3B 상세 평가

### 순위

| 순위 | 모델 | Faithful | Rate | 추출/턴 | 시간 | 가중점수 |
|:---:|------|:--------:|:----:|:-------:|:----:|:--------:|
| 🥇 | **Qwen2.5-Coder-3B** | 54/65 | **83.1%** | 4.6 | 1217s | **7.85** |
| 🥈 | Qwen3-4B | 59/108 | 54.6% | 7.2 | 2737s | 6.05 |
| 🥉 | Gemma 3 4B | 46/108 | 42.6% | 7.2 | 2113s | 4.90 |
| 4 | LFM2-8B-A1B | 12/74 | 16.2% | 5.3 | 812s | 2.00 |

### 선정 사유
- **운영 안정성**: Qwen2.5-Coder-3B의 83.1% faithfulness는 retry/fallback chain 부담 최소화
- **Schema 100% 준수**: compound fact_type 0건 (Qwen3-4B 26.7%, LFM2 85.7%)
- **속도 우위**: 81s/turn (Qwen3-4B 182s/turn의 44%)
- **단점**: 4.6/turn으로 Coverage 제한적 → MCP downstream에는 충분한지 검증 필요 (32B verify 지적)

---

## 3. P-R-J Pipeline 평가 (8B 모델 전용)

Claude가 3개 8B 모델(R1-8B, Qwen7B, Selene) 역할을 모두 수행.

### 조합 A (현행) — R1-8B(P) → Qwen7B(R) → Selene(J)

| 역할 | 점수 | 분석 |
|------|:----:|------|
| **P (R1-8B)** | **26/30** | 7개 finding, 정확도 9, 커버리지 8, 정밀도 9 |
| **R (Qwen7B)** | **27/30** | 7/7 정확, 효율 10, 완전성 8 |
| **J (Selene)** | **gap=1** | P 26 vs R 27, veto 없음, consensus 90/100 |

**강점**: 7개 finding 모두 2:0 fast-path. 명확한 데이터 기반 판단. 각 finding에 정량적 증거 포함.
**약점**: Disputed finding 0건 — Judge의 tie-breaking 능력 미검증.

### 추천 역할 할당 (8B 3개 로테이션)

| 포지션 | 1순위 | 2순위 | 3순위 |
|--------|:-----:|:-----:|:-----:|
| **Proposer (P)** | R1-8B | Selene | Qwen7B |
| **Refuter (R)** | Qwen7B | R1-8B | Selene |
| **Judge (J)** | Selene | Qwen7B | R1-8B |

→ **현행(조합 A) 유지 권장**. R1-8B의 깊은 추론이 P에 적합, Qwen7B의 효율적 판단이 R에 적합, Selene의 구조적 scoring이 J에 적합.

---

## 4. MCP 30B 평가

**평가 데이터**: activity_log #281 (실제 extract_pipeline.py 출력)

| 기준 | 점수 | 분석 |
|------|:----:|------|
| TLDR 정확도 | **9/10** | 사실적, 15단어 이내, 의도 일치 |
| Entity 정확도 | **4/10** | 3개 파일 경로 전부 존재하지 않음 (de_mod_pipeline.py 오타, cli.py 경로 누락) |
| Tag 유용성 | **8/10** | 5개 태그 모두 검색/라우팅에 유용 |

**핵심 이슈**: 30B MCP가 `entities.files`에서 0/3 hallucination. "de_mod_pipeline.py"는 "code_mod_pipeline.py"의 오타로 추정되나 파이프라인이 자동 보정하지 않음. 30B fallback caution prompt에도 불구하고 지속 발생.

---

## 5. Verify 27B vs 32B 비교

### 실제 데이터 분석 (activity_log)

| 항목 | 27B (Qwen3.6-27B) | 32B (Qwen2.5-Coder-32B, 실험) |
|------|:------------------:|:-----------------------------:|
| DeepSeek 동의율 | **0%** (0/3) | — (아직 비교 데이터 없음) |
| Reasoning 누락 | **67%** (2/3) | — |
| 과잉신뢰 (이유 없는 high confidence) | **100%** (2/2 reasoned missing) | — |

### 27B 근본 문제
1. **Reasoning 누락**: 추론 없이 verdict만 출력하는 경우가 67%
2. **Item 타입 오인**: meta-test를 code change로 오해 (activity_log #278)
3. **Confidence 부정확**: 근거 없는 0.95 confidence
4. **임시방편 escalate**: 추론 실패 시 기본값 escalate (activity_log #214)

### 해결 방안 (검증 기준)
- `reasoning` 필드 길이 ≥100자 강제 검증
- reasoning 없는 confidence는 자동으로 50%로 감소
- DeepSeek Pro API audit의 disagreed율 30% 초과 시 alert

---

## 6. 발생한 저장 파일

```
data/eval/
├── eval_rubric_extract_3b.json       # 역할별 rubric + 4개 모델 점수
├── eval_posthoc_extract_3b.json      # 사후 평가 + 운영 권장사항
├── eval_mcp_30b.json                 # MCP 30B 출력 평가
├── eval_verify_27b.json              # 27B verify 평가
├── eval_verify_32b.json              # 32B verify 평가 (독립적 관점)
└── eval_verify_feedback.json         # Verify 종합 피드백 + 생산 데이터 이슈
```

## 7. 후속 조치

| 우선순위 | 작업 | 담당 |
|:--------:|------|------|
| **P0** | Verify 27B reasoning 누락 핫픽스 — reasoning 길이 검증 로직 추가 | Claude |
| **P1** | Extract 3B Qwen2.5-Coder-3B로 전환 (day 모드) | 수동 (모드 파일 변경) |
| **P2** | MCP 30B entity hallucination 조사 — 'de_mod_pipeline.py' 오타 수정 | Claude |
| **P3** | Extract rubric에 cost_efficiency criterion 추가 | Claude |
| **P4** | P-R-J disputed finding 시나리오 테스트 (8B 조합 B, C) | 야간 배치 |
| **P5** | MCP 14B/27B 비교 평가 (동일 extract로) | 모델 스왑 필요 |

---

## 8. 비고

- **좋은 결과도 활용**: LFM2-8B-A1B의 16.2%는 extract에 부적합하지만, fallback chain 검증의 negative test로 사용 가능
- **나쁜 결과의 가치**: 27B verify의 3/3 DeepSeek disagreed는 verify 품질 개선의 중요한 신호
- **Rubric + Post-hoc 이중 평가**는 모델 선정의 신뢰도를 높임 (32B가 27B의 과신을 지적한 사례)
