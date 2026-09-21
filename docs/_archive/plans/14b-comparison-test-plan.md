# 14B Q4_K_M 3종 비교 테스트 계획
## 평가 항목: 속도 + 정확도 + MiniCheck + Claude Pro 격차

---

## 1. 테스트 대상

| # | 모델 | 파일명 | 크기 | 비고 |
|---|------|--------|------|------|
| A | **Qwen 2.5 Coder 14B Q4_K_M** | qwen2.5-coder-14b-instruct-q4_k_m.gguf | 8.4G | 코드 특화 |
| B | **Qwen 2.5 14B Instruct Q4_K_M** | Qwen2.5-14B-Instruct-Q4_K_M.gguf | 8.4G | 일반 지시 |
| C | **Qwen3 14B Instruct Q4_K_M** | Qwen3-14B-Q4_K_M.gguf | 8.4G | 최신 Qwen |

(참고 baseline: 7B Mistral/Qwen/Llama PRJ 결과 — 이미 완료)

---

## 2. 테스트 케이스 (6개)

| # | 케이스 | 난이도 | 특징 |
|---|--------|--------|------|
| 1 | perf_bug | 하 | 단순 성능 버그, 1개 finding |
| 2 | security_leak | 하 | 명확한 보안 이슈 |
| 3 | no_issue | 중 | **false positive trap** |
| 4 | hallu_trap | 중 | **할루시네이션 유도** (OOM 잘못 판단) |
| 5 | mixed | 상 | 복합 이슈 3건 |
| 6 | mcp_context | 중 | MCP global context 포함 추출 |

---

## 3. 평가 지표 (5축)

| 축 | 지표 | 비중 | 설명 |
|----|------|------|------|
| ① **GT Score** | count + category + hallu rate | 40% | 기존 GT 기반 정확도 |
| ② **MiniCheck** | hallucination rate (%) | 20% | 증거 기반 사실성 |
| ③ **Speed** | tokens/sec + case당 elapsed | 20% | ARM 실전 가능성 |
| ④ **Claude Pro Gap** | Pro 결과 대비 유사도 (%) | 20% | 대형 모델과의 격차 |
| ⑤ **Claude Ref Score** | Reference 대비 coverage | (참고) | 정성 평가 |

---

## 4. 실행 순서

### Phase 1: 14B 로컬 테스트 (이 세션)
1. Pod B → **Coder 14B** swap → 6 cases extract + MiniCheck (save)
2. Pod B → **Qwen 2.5 14B** swap → 6 cases extract + MiniCheck (save)
3. Pod B → **Qwen3 14B** swap → 6 cases extract + MiniCheck (save)
4. 결과 집계 → JSON 저장

### Phase 2: Claude Pro 테스트 (Pro 세션 또는 툴)
1. 동일 6개 케이스를 Claude Pro(Opus)로 extract
2. Pro 결과 추출 (14B와 동일한 프롬프트 사용)
3. MiniCheck은 Pro에도 적용 (할루시네이션 측정)

### Phase 3: 종합 비교
1. 14B 3종 × 6케이스 결과 vs 7B PRJ vs Claude Pro
2. 모델 간 정확도/속도/할루시네이션 격차 분석
3. **최종 추천**: Day extract에 가장 적합한 모델 선정

---

## 5. 필요 리소스

| 항목 | 내용 |
|------|------|
| 모델 | 3개 × 8.4G = 25G (disk 충분, 24G free) |
| Pod B | 단독 사용, systemd HealthStartPeriod=300s |
| RAM | 14B Q4_K_M + cache_ram=2048 = ~10-12G (22Gi 중 여유) |
| 시간 | 모델당 ~15-20분 × 3 = ~45-60분 |
