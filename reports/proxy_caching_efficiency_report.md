# 프록시 캐싱 효율성 평가 보고서 (Proxy Caching Efficiency Evaluation Report)

## 1. 개요 (Overview)

본 보고서는 `anthropic_proxy.py` — Anthropic API 형식을 DeepSeek API로 변환하는 프록시 — 의 캐싱 관련 기능을 평가합니다.  
DeepSeek는 **자동 Prefix Caching (APC)** 을 지원하며, 프록시는 이 효율을 최대화하기 위해 여러 정규화 기법을 적용하고 있습니다.

---

## 2. 캐싱 관련 메커니즘 현황

### 2.1 JSON 직렬화 정규화 (`_forward` 메서드, L706-L751)

프록시는 요청 본문을 DeepSeek에 전달하기 전에 다음 설정으로 **재직렬화(re-serialize)** 합니다:

| 설정 | 환경 변수 | 기본값 | 설명 |
|---|---|---|---|
| `sort_keys` | `ANTHROPIC_PROXY_SORT_KEYS` | `true` | JSON 키 알파벳 순 정렬 |
| Compact separators | `ANTHROPIC_PROXY_COMPACT_JSON` | `true` | `, ` → `,`, `: ` → `:` |

**목적:** DeepSeek의 APC는 요청 body의 **바이트 단위 prefix 일치**에 의존합니다.  
동일한 키 순서와 압축된 JSON은 body의 SHA256 해시 안정성을 높여 캐시 적중률(cache hit rate)을 향상시킵니다.

**평가:** ✅ 적절한 설계. 다만 A/B 테스트용 환경 변수가 있으나 (`_sort_keys`, `_compact`),  
현재 두 설정 모두 `true`가 기본이며 비활성화할 이유가 없습니다.  
→ **개선 제안:** `sort_keys=False`로 인한 캐시 비효율은 거의 확실하므로, 환경 변수 유지는 유연성을 위해 두되 기본값 유지 권장.

### 2.2 Cache Control 헤더 제거 (`_forward`, L770-771)

DeepSeek는 **명시적 cache control** (`anthropic-beta` 헤더의 `prompt-caching`)을 지원하지 않습니다.  
프록시는 요청에서 `cache_control` 필드를 발견하면 이를 자동 제거하고 로그에 기록합니다.

**평가:** ✅ 필수 호환성 조치. DeepSeek 측에서 무시되지 않고 오류가 발생할 경우를 대비한 방어적 코딩.

### 2.3 사용량 로깅 (`log_usage` 함수, L403-L420)

응답의 `usage` 필드에서 토큰 정보를 추출하여 cache hit/miss 현황을 stderr에 출력합니다.

```python
u = r.get("usage", {})
input_tokens = u.get("input_tokens", 0)
cache_read = u.get("cache_read_input_tokens", 0)
cache_create = u.get("cache_creation_input_tokens", 0)
output_tokens = u.get("output_tokens", 0)
```

**문제점 (상세: 섹션 3 참조)**

---

## 3. 발견된 문제점 (Findings)

### 🔴 문제 1: 스트리밍 응답에서 사용량 미로그

`_stream_response` (L443-L482)는 청크를 실시간으로 클라이언트에 전달하지만, **항상 `b""`(빈 bytes)를 반환**합니다.

```python
# L482
return b""
```

`_forward`에서 `log_usage(body, data, resp.status)` 호출 시 (L805)  
`data`가 빈 값이므로 `log_usage`의 조건문 (`if body and resp_status == 200 and data:`) 에서 **항상 스킵**됩니다.

**영향:** 스트리밍 요청의 캐시 효율성을 전혀 모니터링할 수 없습니다.  
대부분의 LLM API 호출은 스트리밍(SSE) 방식임을 고려하면, 캐시 통계의 대부분이 누락됩니다.

### 🔴 문제 2: DeepSeek 응답 필드명 불일치 (실제 로그로 확인)

`log_usage`는 **Anthropic API의 필드명**으로 작성되었습니다.  
실제 proxy 서비스 저널에서 **모든 usage 값이 0으로 기록**되어 있음이 확인되었습니다:

```
Jun 01 04:03:26 devforge-444795 python3[507507]: [anthropic_proxy] usage: input=0 cache_read=0 cache_create=0 output=0 hit_rate=0% body=30KB
Jun 01 04:04:19 devforge-444795 python3[507507]: [anthropic_proxy] usage: input=0 cache_read=0 cache_create=0 output=0 hit_rate=0% body=41KB
```

이는 body=30~41KB의 요청이 정상적으로 전송되었으나,  
DeepSeek 응답의 필드명(`prompt_cache_hit_tokens`, `prompt_cache_miss_tokens`)과  
코드에서 기대하는 필드명(`cache_read_input_tokens`, `cache_creation_input_tokens`)이  
일치하지 않기 때문입니다.

| 의미 | Anthropic 필드명 | DeepSeek 필드명 |
|---|---|---|
| 캐시 읽기 토큰 | `cache_read_input_tokens` | `prompt_cache_hit_tokens` |
| 캐시 생성 토큰 | `cache_creation_input_tokens` | `prompt_cache_miss_tokens` |

DeepSeek의 응답은 다음과 같은 구조입니다:
```json
{
  "usage": {
    "input_tokens": 100,
    "output_tokens": 50,
    "prompt_cache_hit_tokens": 60,
    "prompt_cache_miss_tokens": 40
  }
}
```

현재 코드에서 `cache_read_input_tokens`와 `cache_creation_input_tokens`는 **항상 `0`**으로 읽히므로  
캐시 효율성 메트릭이 무의미해집니다.

### 🟡 문제 3: DeepSeek Cache Hit 공식

`log_usage`에서 hit rate 계산:
```python
denom = input_tokens + cache_read
pct = min((cache_read / denom * 100), 100.0) if denom > 0 else 0
```

이 공식은 DeepSeek에 적용하기에 적절합니다. DeepSeek 문서에 따르면:
```
Cache Hit Rate = prompt_cache_hit_tokens / (input_tokens + output_tokens)
```

다만 분모를 `input_tokens + cache_read`로 사용하는 것은 일부 경우 부정확할 수 있습니다.  
DeepSeek는 cache hit 여부와 무관하게 `input_tokens`를 항상 전체 입력에 대해 집계하며,  
`prompt_cache_hit_tokens`는 그중 캐시된 부분만 별도로 보고합니다.

**권장 공식:** `hit_rate = prompt_cache_hit_tokens / input_tokens * 100`

### 🟡 문제 4: 비정상 응답 처리

`log_usage`에서 `resp_status == 200` 검사는 유효하지만,  
DeepSeek API가 200 응답 본문에 에러 메시지를 포함하는 경우에 대한 처리가 없습니다.  
예외 처리(`except Exception: pass`)가 모든 에러를 무음 처리하여 디버깅이 어렵습니다.

---

## 4. 권장 개선 사항 (Recommendations)

### 필수 (P0) — Task 3 연계

| # | 설명 | 우선순위 | 난이도 |
|---|---|---|---|
| R1 | `log_usage`에서 DeepSeek 필드명 매핑 추가 | P0 | 낮음 |
| R2 | 스트리밍 응답에서 SSE last chunk 파싱하여 usage 추출 | P0 | 중간 |

### 권장 (P1)

| # | 설명 | 우선순위 | 난이도 |
|---|---|---|---|
| R3 | Cache hit rate 공식을 `prompt_cache_hit_tokens / input_tokens`로 수정 | P1 | 낮음 |
| R4 | 로그 레벨/형식 개선 (구조화된 JSON 로그로 전환 검토) | P1 | 낮음 |
| R5 | `cache_read_input_tokens`/`cache_creation_input_tokens` → `cache_read`/`cache_create`로 계산 단순화 | P1 | 낮음 |

### 장기 검토 (P2)

| # | 설명 | 우선순위 |
|---|---|---|
| R6 | 캐시 통계를 메트릭 수집 시스템(예: Prometheus)으로 전송하는 옵션 추가 | P2 |
| R7 | A/B 테스트용 분류기: `sort_keys=True/False` 요청을 일정 비율로 라우팅하여 실제 캐시 영향 측정 | P2 |

---

## 5. 캐시 효율성 예상 분석 (Expected Cache Performance)

### 5.1 캐시 적중에 유리한 패턴

| 패턴 | 영향 | 설명 |
|---|---|---|
| 긴 System prompt (`messages[0].content`) | 🟢 높음 | 프록시가 `system` 필드로 분리하여 매 요청마다 동일한 prefix 형성 |
| 동일한 대화 컨텍스트 재사용 | 🟢 높음 | truncation 로직이 오래된 메시지를 제거해도 최근 context의 prefix 안정성 유지 |
| JSON 키 일관성 (`sort_keys=True`) | 🟢 중간 | 동일한 요청 구조가 매번 같은 바이트 표현으로 직렬화됨 |

### 5.2 캐시 적중에 불리한 패턴

| 패턴 | 영향 | 설명 |
|---|---|---|
| 매번 달라지는 User content | 🔴 낮음 | 메시지 내용이 매번 다르면 prefix 매칭 실패 |
| Tool call 결과 다양성 | 🟡 중간 | Tool 결과가 동일한 형식이어도 값이 달라지면 캐시 무효화 |
| 긴 대화 히스토리 truncation | 🟡 중간 | Truncation으로 인해 요청 body의 prefix가 변경될 수 있음 |

---

## 6. 결론

현재 프록시의 **JSON 정규화 및 호환성 처리**는 DeepSeek APC를 최대한 활용하기 위한 적절한 설계입니다.  
그러나 **스트리밍 응답의 사용량 로깅 누락**(Problem 1)과 **Anthropic 필드명 의존성**(Problem 2)으로 인해  
캐시 효율성 모니터링이 사실상 동작하지 않고 있습니다.

**가장 시급한 조치:**  
1. `log_usage`가 DeepSeek 응답 필드명(`prompt_cache_hit_tokens`, `prompt_cache_miss_tokens`)을 인식하도록 수정  
2. Streaming 응답에서 SSE 마지막 청크의 usage 정보를 추출하여 로깅

이 두 가지 수정만으로도 캐시 효율성 모니터링이 정상화되어,  
이후 캐시 최적화 전략 수립이 가능할 것으로 판단됩니다.
