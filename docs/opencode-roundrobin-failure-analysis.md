# OpenRouter Free Model 라운드로빈 실패 분석

> 작성일: 2026-09-08
> 대상: opencode v1.18.29, `experimental.modelFallbackChain`
> 상태: 원인 분석 완료 + 해결책 확정 (Naveenxyz/openrouterproxy, 검증 진행 중)

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

## 해결 방안 — Naveenxyz/openrouterproxy 채택 (2026-09-08 확정)

### 요구사항 재정의

사용자와 협의 후 성공 기준을 명확히 함:

| 항목 | 값 |
|------|-----|
| 회피 대상 | **분당 RPM**만 (OpenRouter: 크레딧 계정 기준 ~20 RPM) |
| 일일 캡 | **계정당 1000건**, 3개 계정 = 일 3000건 total |
| 전략 | 순차 라운드로빈 (key1 → key2 → key3 → key1 → ...) |
| 평균 부하 | 일 3000건 ≈ 분당 ~2건 → 키당 분당 ~0.7건 → **RPM 한도에 크게 미달** |

RPM만 회피하면 되는 구조라, cooldown 관리나 상태 추적이 없는
**단순 라운드로빈 프록시**로 충분하다.

### 후보 3개 비교

| 비교 축 | **Aculeasis/openrouter-proxy** | **Naveenxyz/openrouterproxy** ⭐ | **NousResearch/hermes-agent** |
|---------|-------------------------------|---------------------------------|-------------------------------|
| 언어 | Python, FastAPI | Python 3.8+, FastAPI + httpx | Hermes Agent 클라이언트 내장 |
| 라운드로빈 | ✅ round-robin (기본) | ✅ 순차 순환 | ✅ round_robin / least_used / fill_first / random |
| 429 cooldown | ✅ 4시간 자동 (14400s) | ❌ 없음 (다음 키로만 이동) | ✅ 429→1회 재시도→rotation (1h) |
| 배포 형태 | 프록시 (독립) | 프록시 (독립) | ❌ **클라이언트 완전 교체 필요** |
| opencode 호환 | ✅ base_url만 변경 | ✅ base_url만 변경 | ❌ hermes-agent로 대체 |
| 설정 | `config.yml` + 서비스 설치 스크립트 | `.env`에 `OPENROUTER_API_KEYS="k1,k2,k3"` | `hermes auth add` CLI |
| 오버엔지니어링 | ⚠️ cooldown/free_only 등 과함 | ✅ 요구사항에 정확히 일치 | N/A |
| 주의점 | 66 stars, 신생 | - `python-dotenv` 별도 설치 필요<br>- 429 걸린 키를 추적 안 함 | 키 전환 시 프롬프트 캐시 무효화 (계정별 캐시) |

### 채택 이유: Naveenxyz/openrouterproxy

1. **요구사항이 단순함** — RPM만 회피하면 되므로 Aculeasis의 cooldown/rate_delay/free_only는 불필요한 오버헤드
2. **설정이 1줄** — `.env`에 키 3개 콤마 구분만 하면 끝
3. **상태 추적 없음** — 버그 발생 여지가 적고, 거의 호출되지 않을 429 처리 로직이 단순
4. **배포 간단** — Uvicorn/Podman/systemd user service 모두 적합
5. hermes-agent는 opencode를 못 쓰게 되므로 탈락

### 구축 계획

```
opencode ──> http://127.0.0.1:8000/v1 (Naveenxyz proxy)
              ├─ 요청마다 key1 → key2 → key3 → key1 → ... 순환
              ├─ 429 시 다음 키로 넘김 (다음 rotation에 다시 포함)
              └─ 3개 키 = 분당 RPM 3배 확보

opencode.json 변경:
  provider.openrouter.options.baseURL → "http://127.0.0.1:8000/v1"
  provider.openrouter.options.apiKey  → 로컬 배포용 임의 값 (프록시가 교체)
  experimental.modelFallbackChain    → 단일 체인(선택 사항, 제거 가능)
```

| 단계 | 작업 |
|------|------|
| 1 | `git clone https://github.com/Naveenxyz/openrouterproxy` |
| 2 | venv 생성 + `pip install -r requirements.txt python-dotenv` |
| 3 | `.env`: `OPENROUTER_API_KEYS="sk-or-v1-77ed...,sk-or-v1-1911...,sk-or-v1-7ab4..."` |
| 4 | systemd user service 등록 (Uvicorn, 포트 8000) |
| 5 | opencode.json baseURL 변경 |
| 6 | 검증: 요청 3회 후 각 키의 요청 수 로그로 라운드로빈 확인 |

### 리스크 & 주의

- **일일 캡 초과 위험**: 일 3000건이 순수 분할이므로 어느 계정이 먼저 1000건에 도달할 수 있음.
  → 단순 RR이 아닌 `least_used`(최소 사용 키 우선) 전략이 필요할 수 있음.
  → Naveenxyz는 RR만 지원하므로, 일일 1000건 도달 시 해당 키를 잠정 배제하는
    가드가 추가로 필요할 수 있음 (요구사항 확인 후 결정).

## 부록: 세 프로젝트 상세 조사

### Aculeasis/openrouter-proxy (권장 후보였으나 보류)

- `/api/v1/{path}` 전부 위임, `/api/v1/models`는 public endpoint 가능
- `key_selection_opts`의 `same` 전략: 직전 성공 키 재사용 (세션 유지)
- `global_rate_delay`: Google `RESOURCE_EXHAUSTED` 반복 방지용
- 기본 4시간 cooldown, `service_install.sh`로 systemd 설치 지원

### Naveenxyz/openrouterproxy (채택)

- `POST /v1/chat/completions` (stream 포함), `GET /v1/models`, `GET /` 헬스체크
- `ALLOWED_AUTH_TOKENS` 미설정 시 인증 없이 공개됨 → 설정 권장 (127.0.0.1 바인딩으로 완화)
- `.env` 필요: `OPENROUTER_API_KEYS` (필수), `HOST`, `PORT`

### NousResearch/hermes-agent credential-pools (탈락)

- 같은 provider 내 키 로테이션 (fallback provider와 구분됨)
- 에러 복구: 429(1회 재시도→rotation), 402(즉시 rotation), 401(OAuth refresh→rotation)
- 프로세스 간 OAuth refresh 파일락, 서브에이전트 풀 공유 등 정교함
- **단, standalone 프록시가 아니므로 opencode 유지 불가** → 조건부 채택 불가

## 결론

`modelFallbackChain`은 (1) 공식 스키마에 없는 실험 기능이며, (2) 라운드로빈이 아닌
선형 fallback이므로, **요청 간 키 분산이 불가능**하다. 여기에 (3) 대상 무료 모델들이
전부 종료된 상태가 겹쳐 전체 실패로 귀결됐다.

해결책으로 **Naveenxyz/openrouterproxy**를 채택했다. RPM만 회피하면 되는 단순 요구사항에
정확히 부합하며, 3개 키 순차 순환으로 분당 한도를 3배 확보한다.
일일 계정별 1000건 캡은 순수 RR 분할 대비 편차가 생길 수 있어, 구축 후
**키별 사용량 모니터링과 가드**를 추가하는 것으로 보완한다.

## 참고

- 설정 파일: `/home/opc/.config/opencode/opencode.json`
- 로그: `/home/opc/.local/share/opencode/log/opencode.log`
- opencode 버전: 1.18.29 (Bun binary)
- 바이너리: `/home/opc/.local/lib/node_modules/opencode-ai/node_modules/opencode-linux-arm64/bin/opencode`
- 저장소: `opencode-ai/opencode` (2026-09-07 archived)