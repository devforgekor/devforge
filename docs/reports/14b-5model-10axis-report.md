# 14B 5-Model Extract Comparison — 최종 보고서

## 개요
- **평가 방식**: Pro 설계 10축 체계 (checklist.yaml) — 각 축 0~10점, 가중합 100점 만점
- **테스트 대상**: 3B Q8, 7B Q8, Coder 14B Q4_K_M, Qwen2.5 14B Q4_K_M, Qwen3 14B Q4_K_M
- **테스트 케이스**: 6개 (extract_perf, extract_security, extract_clean, extract_hallu_trap, extract_mcp_rich, verify_contradict)
- **Test 1**: 루브릭 없음
- **Test 2**: Pro 설계 84줄 10축 루브릭 프롬프트 주입
- **실행일**: 2026-06-09 ~ 2026-06-10 KST
- **서버**: DEVFORGE ARM Neoverse-N1, Pod B 단독 사용

---

## 1. Test 1 결과 (루브릭 없음)

| 모델 | 축1 | 축2 | 축3 | 축4 | 축5 | 축6 | 축7 | 축8 | 축9 | 축10 | COMP | 등급 | 속도 |
|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:----:|:---:|------|
| 3B Q8 | 4 | 2 | **10** | 4 | 6 | **10** | 6 | 6 | **10** | 0 | **50** | C | 5.2t/s |
| 7B Q8 | 4 | 2 | **10** | 2 | 7 | **10** | 6 | 6 | **10** | 2 | **50** | C | 3.1t/s |
| Coder 14B | 4 | 2 | **10** | 4 | 7 | **10** | 6 | 6 | **10** | 2 | **52** | C | 1.5t/s |
| Qwen 2.5 14B | 4 | 2 | **10** | 4 | 7 | **10** | 6 | 6 | **10** | 2 | **52** | C | 1.5t/s |
| Qwen3 14B | 4 | 2 | 8 | 2 | 6 | **10** | 6 | 6 | **10** | 2 | **47** | C | 1.6t/s |

### 강점 (전 모델 공통)
- **축3 할루저항 10점**: 5개 중 4개 모델 만점. hallu_trap에서 OOM이 아닌 디스크 92%를 정확히 포착
- **축6 MCP컨텍스트 10점**: PostgreSQL, Caddy 등 인프라 정보 완벽 반영
- **축9 JSON안정성 10점**: 모든 응답 파싱 성공, 코드펜스 없음

### 약점 (전 모델 공통)
- **축1 Fact 추출력 4점**: reference 대비 절반도 못 찾음. Token mismatch 문제 (한국어 evidence와 영어 reference 간 Jaccard 불일치)
- **축2 정밀도 2점**: extract_clean 케이스에서 전 모델이 5~7개 가짜 fact 생성. 정답은 빈 배열
- **축10 일관성 0~2점**: 케이스별 편차 큼 (특히 extract_clean과 타 케이스 간 점수차)

---

## 2. Test 2 결과 (루브릭 있음)

| 모델 | 축1 | 축2 | 축3 | 축4 | 축5 | 축6 | 축7 | 축8 | 축9 | 축10 | COMP | 등급 | 속도 |
|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:----:|:---:|------|
| 3B Q8 | 4 | 2 | **10** | 4 | 7 | **10** | **10** | 6 | **10** | 0 | **55** | C | 5.1t/s |
| 7B Q8 | 4 | 2 | **10** | 2 | 8 | **10** | **10** | 6 | **10** | 2 | **55** | C | 3.0t/s |
| Coder 14B | 4 | 2 | **10** | 4 | 7 | **10** | **10** | 6 | **10** | 0 | **55** | C | 1.4t/s |
| Qwen 2.5 14B | **6** | 2 | **10** | 4 | 7 | **10** | **10** | 6 | **10** | 2 | **59** | C | 1.5t/s |
| Qwen3 14B | **6** | 2 | **10** | 4 | 6 | **10** | **10** | **10** | **10** | 2 | **62** | B | 1.6t/s |

### 변화
- **축7 Verify 6→10**: 모든 모델이 루브릭의 scale 정의("10=escalate+exact reasoning")를 따름. verify_contradict 정확히 escalate 판정
- **축1+축4 14B 상승**: Qwen2.5 14B, Qwen3 14B만 Fact 추출력 +2, FactType 정확도 +2
- **축8 NLI Qwen3 14B**: faithful 비율 80% 돌파로 10점

---

## 3. Test 1 → Test 2 델타

| 모델 | Test 1 | Test 2 | Δ | 개선 요인 |
|------|:------:|:------:|:---:|----------|
| 3B Q8 | 50 | 55 | **+5** | Verify +4, MCP +1 |
| 7B Q8 | 50 | 55 | **+5** | Verify +4, MCP +1 |
| Coder 14B | 52 | 55 | **+3** | Verify +4, MCP -0 |
| Qwen 2.5 14B | 52 | 59 | **+7** | Verify +4, Recall +2 |
| **Qwen3 14B** | 47 | 62 | **+15** | Verify +4, Recall +4, NLI +4 |

---

## 4. 축별 해석

### 루브릭 효과 있음 ✅
| 축 | Test 1 | Test 2 | 원인 |
|---|:---:|:---:|------|
| **축7 Verify** | 6 | **10** | 루브릭에 "escalate with exact reasoning=10" 명시 → 전원 따름 |
| **축1 Recall** | 4 | 4~6 | Qwen2.5/Qwen3 14B만 개선. 3B/7B/Coder14B는 효과 없음 |

### 루브릭 효과 없음 ❌
| 축 | 점수 | 이유 |
|---|:---:|------|
| **축2 Precision** | 2→2 | extract_clean에서 빈 배열 반환이라는 루브릭 지시를 무시. 루브릭으로 해결 안 됨 |
| **축3 Hallu** | 10→10 | 이미 만점, 개선 여지 없음 |
| **축10 Consistency** | 0~2→0~2 | 케이스 간 편차는 루브릭으로도 개선 안 됨 |

---

## 5. 모델별 총평

| 모델 | COMP | 등급 | 장점 | 단점 |
|------|:----:|:---:|------|------|
| **Qwen3 14B** | 62 | **B** | 루브릭 효과 가장 큼(+15), NLI 우수 | 14B 중 가장 느림 |
| Qwen 2.5 14B | 59 | C | Recall 개선(+2) | 1.5t/s, 3B 대비 가성비 낮음 |
| 3B Q8 | 55 | C | **5.2t/s** 최고속도, 일관성만 개선되면 1위 가능성 | 축10=0, extract_clean 완전 실패 |
| 7B Q8 | 55 | C | 3B와 동점 | 3B보다 느림(3.1t/s), FactType 부정확 |
| Coder 14B | 55 | C | 14B 중 가장 안정적 | 1.4t/s, 5배 느리면서 3B와 동점 |

---

## 6. Claude Pro Baseline (참고)

| | Test 1 | Test 2 | Δ |
|---|:---:|:---:|:---:|
| **Claude Pro (Opus 4.8)** | 89 (A) | 90 (S) | +1 |

※ Claude Pro baseline은 이전 간이 채점 시스템 기준. 10축 재측정 필요.

---

## 7. 결론 및 추천

1. **3B Q8이 실무 최적**: 14B보다 4~5배 빠르면서 루브릭 적용 시 점수차 7점 이내. 비용 대비 효율 최고
2. **루브릭은 Verify에 가장 효과적**: +4점. extract 품질 개선에는 한계
3. **extract_clean이 공통 난제**: 루브릭 유무와 관계없이 모든 모델이 "이슈 없음" 판단 실패. 별도 few-shot 또는 post-processing 필요
4. **14B Q4_K_M 가치 낮음**: 5배 느리면서 점수 향상 미미. 이 서버에서는 3B Q8 유지 권장
5. **Qwen3 14B만 B등급**: 루브릭 반응성 좋고 NLI 우수하나, 여전히 실무 단독 투입은 어려운 점수(62/100)

---

## 8. 데이터 위치

| 파일 | 내용 |
|------|------|
| `/tmp/14b_comparison_results.json` | Test 1 전체 raw 결과 |
| `/tmp/14b_comparison_results_rubric.json` | Test 2 전체 raw 결과 |
| `/tmp/14b_test1_10axis.log` | Test 1 실행 로그 |
| `/tmp/14b_test2_10axis.log` | Test 2 실행 로그 |
| `docs/specs/14b-comparison-rubric-prompt.txt` | 10축 루브릭 프롬프트 (Pro 설계) |
| `docs/specs/14b-comparison-checklist.yaml` | 10축 채점 기준 (Pro 설계) |
| `scripts/model_comparison_test.py` | 테스트 스크립트 (10축 구현) |

🤖 Generated with [Claude Code](https://claude.com/claude-code)
