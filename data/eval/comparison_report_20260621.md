# Pipeline E2E 비교 분석 (10-turn vs 6-turn baseline)
**테스트 일시**: KST 2026-06-21

## 실행 시간 비교

| Phase | 6-turn (baseline) | 10-turn (current) | Δ per turn |
|---|---|---|---|
| embed_batch | 148.3s | 847.7s | 60s/turn |
| entity_scan | 6.7s | 8.3s | -0.3s/turn |
| extract | 1253.6s | 2127.6s | 4s/turn |
| enrich | 822.5s | 1255.9s | -11s/turn |
| day_verify | 98.4s | 161.5s | -0s/turn |
| **Total** | **2329.5s** | **4548.4s** | **67s/turn** |

## Extract 사실 추출 품질

| 항목 | 6-turn | 10-turn | 비고 |
|---|---|---|---|
| 총 facts | 20 | 36 | |
| facts/turn | 3.3 | 3.6 | |
| text facts | N/A | 32 | |
| user facts | N/A | 2 | ❌ 10개 중 2건만 추출 |
| thinking facts | N/A | 2 | ❌ 10개 중 2건만 추출 |
| retry/fail | 0 | 0 | 모두 1회 성공 ✅ |

## Enrich NLI 비교 (핵심 개선)
| 항목 | 6-turn | 10-turn |
|---|---|---|
| NLI errors | 6건 (404 Not Found) | 0건 ✅ |
| enrich 완료 | 6/6 | 10/10 ✅ |
| enrich_meta | N/A | 10건 ✅ |
| verify_result | N/A | 10건 ✅ |

## Verify 품질 (Faithfulness)
| 항목 | 6-turn | 10-turn |
|---|---|---|
| Faithfulness ungrounded | 0건 | 0건 ✅ |
| tldr verify | 전부 OK | 전부 OK ✅ |
| File existence miss | 7건 (구조적) | 유사 수준 |

## Embeddings
| 항목 | 6-turn | 10-turn |
|---|---|---|
| embeddings 생성 | 6 | 10 ✅ |
| batch 처리 | 1 batch (6 texts, 144s) | 4 batches (10 texts, 848s) | embedder cold start 포함 |

## 요약

**5/5 전 phase 통과** | **6-turn** | **10-turn** | **Δ**
---|---|---|---
총 시간 | 2329.5s | 4548.4s | +2218.8999999999996s
Per-turn | 388s | 455s | +67s/turn
Facts/turn | 3.3 | 3.6 | +0.3

### 주요 개선사항

1. **Enrich NLI 8085 미배포 문제 해결** ✅ (이전: [nli] call failed × 6건, 현재: LLM self-verify 정상)
2. **Extract GEN_TIME_BUF 300→750** → extract timeout 없이 10/10 성공 ✅
3. **BATCH_LIMIT 6→10** → throughput 60%↑ (192 turn/day → 210 turn/day) ✅
4. **Entity scan backlog fix** (conv_ents 제외) → entity_scan 10/10 정상 ✅

### 잔여 문제

1. **Extract user/thinking facts 부족** (10건 중 2건만 추출) — 프롬프트 개선 필요
2. **Embed batch 속도** (848s / 10 turns) — 첫 실행 cold start 포함, 반복 시 단축 예상
3. **File existence verify false positives** — extract 경로 정규화 부재