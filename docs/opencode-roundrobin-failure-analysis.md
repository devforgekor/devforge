# OpenRouter Free Model 라운드로빈 실패 분석

> 작성일: 2026-09-08
> 대상: opencode v1.18.29, `experimental.modelFallbackChain`
> 상태: 분석 완료 (원인 3건 확인, 해결 방안 3개 제시)

## 개요

3개의 OpenRouter 계정(MESIDS, MINIPARK4U, HYEONMINPARK4U)에 각각 \$10+ 크레딧을 충전하고,
`modelFallbackChain`을 통해 무료 모델(MiniMax M3:free, Laguna S 2.1:free)의 rate limit을
회피하려는 시도가 실패한 원인을 분석한다.

## 설정 구조

### 의도한 구성

```
3개 API 키 (각각 $10+ 크레딧)
  ├─ MESIDS        (sk-or-v1-77ed...)
  ├─ MINIPARK4U    (sk-or-v1-1911...)
  └─ HYEONMINPARK4U (sk-or-v1-7ab4...)

각 키로 MiniMax M3:free 요청
→ rate limit 도달 시 다른 키로 fallback
→ 3개 키가 라운드로빈 = 3배 처리량
```

### 실제 설정 (변경 전)

```json
"chains": [
  [
    "openrouter/minimax/minimax-m3:free",       // MESIDS 키
    "openrouter-minipark4u/minimax/m3:free",    // MINIPARK4U 키
    "openrouter-hyeonminpark4u/minimax/m3:free",// HYEONMINPARK4U 키
    "openrouter/poolside/laguna-s-2.1:free",    // MESIDS 키
    "openrouter-minipark4u/poolside/...",       // MINIPARK4U 키
    "openrouter-hyeonminpark4u/poolside/..."   // HYEONMINPARK4U 키
  ]
]
```

## 실패 원인 — 3건

### 원인 1: `modelFallbackChain`은 Round-Robin이 아니다

**`modelFallbackChain`은 요청 간 라운드로빈이 아니라, 단일 요청 내 선형 fallback이다.**

```
요청 #1 → model 1(Minimax M3) 실패
         → model 2(Minimax M3, 다른 키) 실패
         → model 3(Minimax M3, 다른 키) 실패
         → model 4(Laguna S 2.1) 실패
         → model 5(Laguna, 다른 키) 실패
         → model 6(Laguna, 다른 키) 실패
         → 요청 #1 실패 ❌

요청 #2 → 다시 model 1(Minimax M3)부터 ❌ (처음으로 돌아감)
```

**원하는 라운드로빈 동작:**
```
요청 #1 → MESIDS 키로 Minimax M3 시도 → 성공 ✓
요청 #2 → MINIPARK4U 키로 Minimax M3 시도 → 성공 ✓
요청 #3 → HYEONMINPARK4U 키로 Minimax M3 시도 → 성공 ✓
요청 #4 → MESIDS 키로 Minimax M3 시도 → 성공 ✓
```

### 원인 2: `chains` 배열이 여러 개여도 `chains[0]`만 사용된다

**`modelFallbackChain`은 공식 문서/스키마에 없는 비공개 실험(`experimental`) 기능이다.**

증거 — opencode v1.18.29 바이너리에 내장된 config schema에서 `experimental` 섹션:
```json
"experimental": {
  "primary_tools": ["edit"],
  "mcp_timeout": 30000
}
```
→ `modelFallbackChain`은 이 스키마에 존재하지 않는다. 공식 지원 기능이 아니므로
  동작이 보장되지 않는다.

**로그 증거** — 실제 동작 추적 (2026-09-08 00:09~00:30):
```
00:09:31  model=minimax/minimax-m3:free  → "unavailable for free" ❌
00:13:32  model=laguna-s-2.1:free        → "Provider returned error" ❌
00:13:43  model=laguna-s-2.1:free        → 재시도 ❌
00:13:59  model=laguna-s-2.1:free        → 재시도 ❌
00:14:01  model=laguna-s-2.1:free        → 재시도 ❌
... (25회 이상 Laguna만 재시도, MiniMax로 돌아가지 않음)
```

→ `chains[0]`만 사용. `chain[1]`(MINIPARK4U)과 `chain[2]`(HYEONMINPARK4U)는
  **한 번도 호출되지 않음**. 여러 `chains`를 정의해도 효과 없음.

### 원인 3: 모든 Free 모델이 종료됨

| 모델 | 에러 메시지 | 상태 |
|------|-----------|------|
| `minimax/minimax-m3:free` | "This model is unavailable for free. The paid version is available now - use this slug instead: minimax/minimax-m3" | 무료 종료 |
| `poolside/laguna-s-2.1:free` | "Provider returned error" | 무료 종료/에러 |
| `mimo-v2.5-free` (opencode 내장) | "Endpoint is unavailable" / "Rate limit exceeded" | 불가 |

## 부차적 문제

### 3개 키로 같은 모델 호출 시 rate limit 회피 효과는 제한적

OpenRouter의 rate limit 정책:
- **무료 모델(`:free`)**: 모델 기준 rate limit (RPD 등). 키가 여러 개여도 같은 한도에 묶임.
  로그: `Daily limit reached for minimax/minimax-m3:free via GMICloud. Credits don't affect this cap.`
- **크레딧 보유 계정($10+)**: rate limit이 완화되지만, 같은 모델의 유효 한도는 공유됨.
- 키별 처리량 차등 적용은 **유료 모델**에 한정.

→ 같은 무료 모델을 3개 키로 호출해도 rate limit 회피 효과는 **제한적**이다.

## 타임라인

| 일자 | 이벤트 |
|------|--------|
| 2026-07-05 | 초기 설정: DeepSeek V4 Flash Free → Laguna XS 2.1 free fallback |
| 2026-08-26 | OpenRouter rate limiting + fallback models 구성 |
| 2026-09-04 | 3개 키 + 2개 모델(MiniMax M3, Laguna S 2.1)로 fallback chain 확장 |
| 2026-09-06 | MiMo free → rate limit / MiniMax M3 daily limit 도달 |
| 2026-09-07 | Gemini 3.8 Flash → 크레딧 부족 / MiniMax M3 free → 무료 종료 |
| 2026-09-08 | 모든 free 모델 사망, Laguna S 2.1만 25회 연속 실패 |
| 2026-09-08 | **opencode-ai/opencode 저장소 archived** (더 이상 개발 중단 확인) |

## 해결 방안 — 3개

### 방안 A: 외부 라운드로빈 프록시 구축 (권장)

opencode 설정을 단일 `openrouter` provider로 통일하고,
3개 키를 요청마다 순환하는 프록시를 앞에 둔다.

```
opencode → 127.0.0.1:4311 (round-robin proxy)
           → 요청마다 MESIDS / MINIPARK4U / HYEONMINPARK4U 키 순환
           → OpenRouter API
```

구현 예시 (Python 3.11 stdlib, ~50줄):
```python
class Handler(http.server.BaseHTTPRequestHandler):
    counter = 0
    def do_POST(self):
        key = self.KEYS[self.counter % len(self.KEYS)]
        self.counter += 1
        # 요청 body + key로 OpenRouter에 포워딩
```

**장점**: opencode 설정 변경 불필요, 어떤 LLM 클라이언트와도 호환.
**단점**: 프록시 프로세스 유지 필요 (systemd user service).

### 방안 B: 유료 모델로 전환

`minimax/minimax-m3` (접미사 `:free` 제거) 사용.
3개 계정에 \$10+ 크레딧이 있으므로 유료 사용에 문제없음.

**장점**: 안정적, 추가 인프라 불필요.
**단점**: 크레딧 소모, 일일 유료 모델 사용량 관리 필요.

### 방안 C: 다른 Provider의 무료 모델 사용

OpenRouter 대신 Google AI Studio, Hugging Face 등 직접 API 사용.
opencode에 새 provider로 등록.

**장점**: 무료 유지 가능.
**단점**: 모델 품질/중량 불확실, 각 서비스별 TOS 확인 필요.

## 결론

`modelFallbackChain`은 (1) 공식 스키마에 없는 실험 기능이며, (2) 라운드로빈이 아닌
선형 fallback이므로, **요청 간 키 분산이 불가능**하다. 여기에 (3) 대상 무료 모델들이
전부 종료된 상태가 겹쳐 전체 실패로 귀결됐다.

가장 실용적인 해결책은 **방안 A(외부 라운드로빈 프록시)** 이다.
다만 이 방식도 같은 무료 모델을 여러 키로 호출하는 것의 rate limit 회피 효과가
제한적일 수 있으므로, **방안 B(유료 전환)** 와 병행하는 것을 권장한다.

## 참고

- 설정 파일: `/home/opc/.config/opencode/opencode.json`
- 로그: `/home/opc/.local/share/opencode/log/opencode.log`
- opencode 버전: 1.18.29 (Bun binary)
- 바이너리: `/home/opc/.local/lib/node_modules/opencode-ai/node_modules/opencode-linux-arm64/bin/opencode`
- 저장소: `opencode-ai/opencode` (2026-09-07 archived)