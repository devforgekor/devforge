# TLDR NLI 배치 전략 비교: Local vs Web API

## 문제

`enrich.py`는 turn당 `_verify_tldr()`를 1회 호출 (2-4회 전체 중 1회).
현재 1 turn = 1 LLM call → 60s timeout → 순차 처리.
batch NLI로 묶으면 호출 수를 줄일 수 있음.

---

## 1. Local llama.cpp (8082, Qwen3-8B-Q8)

### 아키텍처

```
enrich.py → call_llm_with_retry(model="day_enrich")
         → MODEL_REGISTRY["day-enricher"] = port 8082
         → http://127.0.0.1:8082/v1/chat/completions
         → Qwen3-8B-Q8_0.gguf (8.2GB, ctx 8192, 4 threads)
```

### 추정 성능 (4-core ARM Neoverse-N1)

| 항목 | 속도 | 출처 |
|------|:----:|:----:|
| Prefill (prompt ingest) | ~3 tok/s | `enrich.py:_calc_timeout()` 보정치 |
| Decode (generation) | ~1 tok/s | 동일 |
| 단일 TLDR NLI (400 tok prompt) | prefill 133s + decode 1s = **134s** | 계산 |
| 배치 10건 (3500 tok prompt) | prefill 1167s + decode 10s = **1177s** | 계산 |

### 확장 구조

```
ThreadPoolExecutor(max_workers=2) + day-enricher(8082) + day-enricher-b(8083)
```

현재 `_generate_enrich_fields` 메인 호출이 8082를 선점 중.
TLDR verify는 메인 호출 완료 후 실행되므로 8082가 비어 있음.

### 기존 batch NLI 패턴 (extract_verify.py 준용)

```python
MAX_BUDGET = 7373          # 8192 * 0.9
FIXED_OVERHEAD = 600        # 프롬프트 템플릿 구조
est_per_turn = len(source) // 3 + len(tldr) // 3 + 25
```

배치 프롬프트 구조:
```
[1]
SOURCE: {turn1 source}
TLDR: {turn1 tldr}

[2]
SOURCE: {turn2 source}
TLDR: {turn2 tldr}
...

Output EXACTLY one label per line:
[1] ENTAILMENT
[2] CONTRADICTION
...
```

### 실행 시간 추정

| 배치 크기 | Local (Qwen3-8B-Q8) |
|:---------:|:-------------------:|
| 1 turn (현행) | 134s |
| 5 turns | 585s (5.9×) |
| 10 turns | 1177s (8.8×) |
| 20 turns | MAX_BUDGET 초과 |

### 장점 / 단점

| 장점 | 단점 |
|------|------|
| $0 비용 (기존 인프라) | 메인 enrich 호출과 8082 경합 |
| 기존 `_parse_batch_nli_output()` 재사용 가능 | 긴 prefill (330 tok/min) |
| 데이터 유출 없음 (완전 온프레미스) | 8082 port가 enrich 전용이 아님 |
| llama.cpp parallel=2로 slot 내 concurrency | 턴당 실제 처리량: **0.45 tpm** |

---

## 2. Web API (DeepSeek V4 Flash / Gemini 3.6 Flash)

### 아키텍처 (기존 인프라 활용)

```
enrich.py → call_llm_endpoint(model="deepseek-v4-flash")
         → anthropic-proxy (127.0.0.1:44777) [기존]
         → 또는 gemini-openai-proxy (127.0.0.1:9099) [기존]
         → HTTPS: api.deepseek.com / generativelanguage.googleapis.com
```

두 proxy 모두 이미 구축되어 있음 (`anthropic-proxy`, `gemini-openai-proxy` systemd active).

### 가격

| Model | Input ($/1M tok) | Output ($/1M tok) | Cache Hit ($/1M tok) |
|-------|:----------------:|:-----------------:|:--------------------:|
| DeepSeek V4 Flash | $0.14 | $0.28 | $0.0028 |
| Gemini 3.6 Flash | $1.50 | $7.50 | N/A (context cache 별도) |
| Gemini 3.6 Batch | **$0.75** | **$3.75** | N/A |

@ DeepSeek cache hit: system prompt이 동일하면 ~95%+ cache hit (prefix caching).
  TLDR batch prompt는 user content만 다르므로 cache miss rate 높음.

### 배치 10건 비용 추정

| 항목 | DeepSeek V4 Flash | Gemini 3.6 Batch |
|:----|:-----------------:|:----------------:|
| Input tokens (10건 × 350 tok) | 3,500 tok | 3,500 tok |
| Output tokens (10 labels) | 50 tok | 50 tok |
| Input cost | $0.14/M × 3500 = **$0.00049** | $0.75/M × 3500 = **$0.0026** |
| Output cost | $0.28/M × 50 = **$0.000014** | $3.75/M × 50 = **$0.00019** |
| **Total per 10-turn batch** | **~$0.0005** | **~$0.0028** |
| 연간 비용 (1,000 batch/일 × 365일) | **$182** | **$1,022** |

### 지연 시간 (예상)

| Model | 추정 latency (10건 배치) | 출처 |
|-------|:-----------------------:|:----:|
| DeepSeek V4 Flash | 2-5s | 1M ctx, 2500 concurrency |
| Gemini 3.6 Flash | 1-3s | 최신 Flash 모델 |
| Gemini 3.6 Batch | 5-15분 (비동기 큐) | Batch API 특성상 지연 큐잉 |

### 장점 / 단점

| 장점 | 단점 |
|------|------|
| Local 8082 경합 없음 | 연간 $182-1,022 운영비 |
| 1-5s 응답 (batch 제외) | 인터넷 의존성 (proxy/API 장애) |
| 2500 concurrency (DS) = 사실상 무제한 | 데이터가 외부로 전송됨 |
| 턴당 실제 처리량: **120-600 tpm** | Gemini Batch는 5-15분 지연 |

---

## 3. 비교 요약 (10건 배치 기준)

| 항목 | Local (Qwen3-8B Q8) | DeepSeek V4 Flash | Gemini 3.6 Batch |
|:----|:-------------------:|:-----------------:|:----------------:|
| **Latency** | ~1,177s (20분) | **2-5s** | 5-15분 (비동기) |
| **Cost/10 turns** | **$0** | ~$0.0005 | ~$0.0028 |
| **연간 (365K batch)** | $0 | $182 | $1,022 |
| **Throughput** | 0.45 tpm | **120-600 tpm** | 0.7-2 tpm |
| **8082 port 경합** | ⚠️ 메인 enrich와 공유 | ✅ 없음 | ✅ 없음 |
| **인프라 변경** | ❌ 없음 (코드만) | ⚠️ Webhook/API key 관리 | ⚠️ Batch job 큐 관리 |
| **프라이버시** | ✅ 완전 온프레미스 | ❌ DeepSeek 서버 전송 | ❌ Google 서버 전송 |
| **출력 파싱** | ✅ `_parse_batch_nli_output` 재사용 | ⚠️ 새 파서 필요 | ⚠️ 새 파서 필요 |
| **신뢰도** | 8B Q8 vs cloud: 정확도 유사 | 최신 V4 Flash로 더 높을 수 있음 | 최신 3.6 Flash |

---

## 4. 권장: Local Batch 우선, Web API는 선택

### 1순위: Local Batch 구현 (코드 변경만, $0)

기존 `extract_verify.py`의 batch NLI 패턴(`_llm_nli_verify`)을 그대로 따라:
- `_batch_tldr_verify()` 함수 신규 작성
- MAX_BUDGET=7373, FIXED_OVERHEAD=600으로 배치 분할
- 기존 `_parse_batch_nli_output()` 재사용 (레이블만 `ENTAILMENT|CONTRADICTION|COMPLEMENTARY|NEUTRAL`로 확장)
- 8082가 메인 enrich 호출 중일 때는 TLDR verify가 대기하므로 경합 없음

**예상 속도**: 10건 배치 ≈ 20분 (prefill 병목). 현행 10건 × 134s = 1,340s 대비 **약간 느림** (1,177s vs 1,340s = ~12% 개선).

→ **prefill 병목이 핵심**: local batch는 prefill 시간이 O(N)으로 증가하므로 큰 이득 없음.

### 2순위: DeepSeek API 혼용 (저비용, 고속)

TLDR NLI만 DeepSeek V4 Flash로 라우팅:
- `call_llm_endpoint("https://api.deepseek.com/v1/chat/completions", ...)` 사용
- 기존 `anthropic-proxy` 재활용 가능
- 10건 배치 = $0.0005, 2-5s = 현행 대비 **270× 속도 개선**
- 연간 $182는 운영비 대비 미미

### 최종 판단

| 접근법 | 구현 난이도 | 속도 | 비용 | 채택 |
|--------|:----------:|:----:|:----:|:----:|
| Local batch | **낮음** (extract 패턴 복사) | 12% ↓ | $0 | ⚠️ prefill 병목으로 큰 효과 없음 |
| DeepSeek API batch | **중간** (endpoint 연동) | 270× ↑ | ~$182/년 | ✅ **추천** |
| Gemini Batch | **높음** (비동기 큐) | 1-2 tpm | ~$1,022/년 | ❌ 지연 + 비용 |

**1순위로 Local batch 구현 → prefill 병목 확인 후 2순위 DeepSeek API로 전환**을 권장.
Local도 prefill이 생각보다 빠르면 그대로 사용 가능 (비용 $0).
