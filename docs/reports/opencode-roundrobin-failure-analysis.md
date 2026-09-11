# OpenRouter Free Model 시스템 — 운영 문서

> 최종 갱신: 2026-09-08
> 상태: 운영 중 (E2E 검증 완료)

## 시스템 개요

3개의 OpenRouter 계정(MESIDS, MINIPARK4U, HYEONMINPARK4U)을 라운드로빈하여
무료 모델의 분당 RPM 제한을 회피하고, 매일 자동으로 최신 무료 모델 Top 3를
선정하여 opencode.json을 갱신한다.

```
                    매일 15:30 UTC (00:30 KST)
                    ┌──────────────────────────────┐
                    │  refresh_openrouter_free_    │
                    │  models.py (oneshot)          │
                    │  ├─ OpenRouter catalog fetch  │
                    │  ├─ coding_index 기준 랭킹    │
                    │  ├─ live-test (Top 15)        │
                    │  └─ opencode.json 자동 갱신   │
                    └──────┬───────────────────────┘
                           │ writes
                           ▼
opencode.json ──> openrouter-rr-proxy.service (127.0.0.1:8451)
                    ├─ 요청 → key[1] MESIDS
                    ├─ 요청 → key[2] MINIPARK4U
                    ├─ 요청 → key[3] HYEONMINPARK4U
                    └─ 요청 → key[1] ... (순환)

opencode ──> http://127.0.0.1:8451/v1 (1개 provider)
```

## 컴포넌트

### 1. RR 프록시 — `openrouter_rr_proxy.py`

| 항목 | 값 |
|------|------|
| 경로 | `/opt/projects/server/scripts/proxies/openrouter_rr_proxy.py` |
| 포트 | 8451 (127.0.0.1 전용) |
| 언어 | Python 3.11, FastAPI + httpx |
| 상태 | systemd user service, enabled, active |

**엔드포인트:**

| 경로 | 메서드 | 설명 |
|------|--------|------|
| `/v1/chat/completions` | POST | OpenAI 호환 채팅 (stream + non-stream) |
| `/v1/models` | GET | OpenRouter 모델 목록 |
| `/health` | GET | 헬스체크 |

**키 로딩 순서:**
1. `~/.config/devforge/secrets.env` 파일 직접 파싱
2. (fallback) 환경변수 `OPENROUTER_MESIDS_API_KEY` 등

**주요 특징:**
- 요청마다 `_next_key()`로 3개 키 순환 (`asyncio.Lock` 불필요 — 단일 worker)
- 429 시 다음 키로 fallback, 3개 키 모두 실패 시 502 반환
- streaming 에러 시 `try/finally`로 연결 누수 방지
- lifespan 이벤트로 httpx.AsyncClient 생명주기 관리

### 2. 자동 갱신 — `refresh_openrouter_free_models.py`

| 항목 | 값 |
|------|------|
| 경로 | `/opt/projects/server/scripts/proxies/refresh_openrouter_free_models.py` |
| 실행 주기 | 매일 15:30 UTC (00:30 KST) |
| 실행 방식 | systemd timer → oneshot service |
| 캐시 | `~/.cache/devforge/openrouter_free_models.json` (24시간 TTL) |

**처리 흐름:**

```
1. Fetch catalog (RR 프록시 통해 /v1/models)
2. :free 모델 필터링 (11개 후보)
3. 도메인 특화 모델 제외 (sante, fin, japanese, content-safety 등)
4. coding_index + context_bonus 기준 스코어링
   - benchmark 있음 → coding_index + context_bonus (30~60점)
   - benchmark 없음 → 29.9점 이하로 캡 (검증된 모델 우선)
5. Live-test: Top 15 모델을 프록시 통해 1회씩 호출 (0.3초 간격)
6. 업스트림 org별 그룹화 (nvidia/cohere/liquid 등)
   - 동일 org에서 최고 점수 1개만 선택
7. 상위 3개 org의 모델을 opencode.json에 기록
   - model: 1위 모델
   - fallback chain: 3개 모델 (서로 다른 업스트림)
   - provider.models: 3개 모델
```

**스코어링 상세:**

| 조건 | 점수 | 예시 |
|------|------|------|
| coding_index = 36.5 + context 256K | 36.5 + 5 = 41.5 | cohere/north-mini-code |
| coding_index = 26.8 + context 1M | 26.8 + 5 = 31.8 | nemotron-3.5-lightning |
| benchmark 없음 (29.9 이하 캡) | min(25 + bonus, 29.9) | liquid/lfm-2.5-2.6b |

### 3. systemd 유닛

**openrouter-rr-proxy.service:**

```ini
[Unit]
Description=OpenRouter Key Round-Robin Proxy
After=default.target

[Service]
Type=simple
EnvironmentFile=-%h/.config/devforge/secrets.env
ExecStart=/usr/bin/python3.11 /opt/projects/server/scripts/proxies/openrouter_rr_proxy.py
Restart=on-failure
RestartSec=5
```

**devforge-openrouter-free-models.service:**

```ini
[Unit]
Description=DevForge OpenRouter Free Model Refresh (매일 00:30 KST = 15:30 UTC)

[Service]
Type=oneshot
Environment=PYTHONPATH=/opt/projects/server/scripts
ExecStart=/usr/bin/python3.11 /opt/projects/server/scripts/proxies/refresh_openrouter_free_models.py
WorkingDirectory=/opt/projects/server/scripts
Nice=19
IOSchedulingClass=idle
```

**devforge-openrouter-free-models.timer:**

```ini
[Unit]
Description=DevForge OpenRouter Free Model Refresh Timer (매일 00:30 KST)

[Timer]
OnCalendar=*-*-* 15:30:00
Persistent=true
```

## 파일 인벤토리

| 파일 | 역할 | 유형 |
|------|------|------|
| `scripts/proxies/openrouter_rr_proxy.py` | 3키 RR 프록시 (FastAPI, port 8451) | 운영 |
| `scripts/proxies/refresh_openrouter_free_models.py` | 매일 free 모델 자동 갱신 | 운영 |
| `~/.config/systemd/user/openrouter-rr-proxy.service` | RR 프록시 서비스 | 운영 |
| `~/.config/systemd/user/devforge-openrouter-free-models.service` | 갱신 oneshot 서비스 | 운영 |
| `~/.config/systemd/user/devforge-openrouter-free-models.timer` | 갱신 타이머 (매일 15:30 UTC) | 운영 |
| `~/.config/opencode/opencode.json` | opencode 설정 (자동 갱신 대상) | 운영 |
| `~/.config/devforge/secrets.env` | 3개 OpenRouter API 키 | 시크릿 |
| `~/.cache/devforge/openrouter_free_models.json` | 모델 캐시 (24h TTL) | 캐시 |
| `docs/opencode-roundrobin-failure-analysis.md` | 본 문서 | 문서 |

## 설정 파일 (opencode.json)

```json
{
  "model": "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
  "experimental": {
    "modelFallbackChain": {
      "timeoutMs": 60000,
      "chains": [
        [
          "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
          "openrouter/cohere/north-mini-code:free",
          "openrouter/liquid/lfm-2.5-2.6b:free"
        ]
      ]
    }
  },
  "provider": {
    "openrouter": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "OpenRouter (RR Proxy)",
      "options": {
        "baseURL": "http://127.0.0.1:8451/v1",
        "apiKey": "local-rr-proxy"
      },
      "models": {
        "nvidia/nemotron-3-ultra-550b-a55b:free": { "name": "NVIDIA: Nemotron 3 Ultra 550B" },
        "cohere/north-mini-code:free": { "name": "Cohere: North Mini Code" },
        "liquid/lfm-2.5-2.6b:free": { "name": "LiquidAI: LFM2.5-2.6B" }
      }
    }
  }
}
```

**변경 전후 비교:**

| 항목 | 변경 전 (실패) | 변경 후 (운영) |
|------|---------------|---------------|
| provider 수 | 3개 (openrouter, -minipark4u, -hyeonminpark4u) | 1개 (openrouter) |
| baseURL | https://openrouter.ai/api/v1 | http://127.0.0.1:8451/v1 |
| apiKey | 3개 개별 키 (하드코딩) | local-rr-proxy (프록시가 교체) |
| 모델 | 2개 (minimax, laguna) | 매일 자동 갱신 (3개, 서로 다른 업스트림) |
| fallback 전략 | 1개 체인, 6개 동일 업스트림 | 1개 체인, 3개 다른 업스트림 |

## 일일 운영 사이클

```
15:30 UTC (00:30 KST)
  └─ devforge-openrouter-free-models.timer fires
       └─ devforge-openrouter-free-models.service (oneshot)
            ├─ OpenRouter 모델 카탈로그 fetch (~1s)
            ├─ 11개 free 모델 스코어링 (~0.1s)
            ├─ 15개 모델 live-test (~40s)
            │   └─ 각 모델 1회 호출 → 0.3s 간격
            └─ opencode.json 갱신 (~0.1s)
                 └─ 다음 opencode 세션 시작 시 적용

opencode 세션 중:
  └─ http://127.0.0.1:8451/v1 (RR 프록시)
       ├─ 요청마다 3개 키 순환
       ├─ 429 시 다음 키로 fallback
       └─ modelFallbackChain: 3개 모델 순차 시도
```

## 검증 결과

### 단위 검증 (2026-09-08)

| # | 검증 항목 | 결과 |
|---|----------|------|
| 1 | Health endpoint | ✅ `{"status":"ok","keys":3}` |
| 2 | Models list | ✅ HTTP 200, 704KB |
| 3 | Non-streaming chat | ✅ GPT-4o-mini, cost $4.95e-06 |
| 4 | Streaming chat | ✅ SSE data: chunks |
| 5 | Round-robin (6회 병렬) | ✅ key[1]→[2]→[3]→[1]→[2]→[3] |
| 6 | Invalid model → fallback | ✅ 400 + 다음 키 시도 |
| 7 | Invalid JSON body | ✅ 400 "Invalid JSON body" |
| 8 | All keys 429 → 502 | ✅ 3개 키 전부 실패 시 502 |
| 9 | opencode.json 설정 일치 | ✅ baseURL, model, chain 일치 |
| 10 | OpenAI 클라이언트 호환 | ✅ Bearer auth + 전체 응답 |
| 11 | Stress 10 concurrent | ✅ Race condition 없음, RR 유지 |
| 12 | 메모리 | ✅ RSS 60MB |
| 13 | 포트 바인딩 | ✅ 127.0.0.1:8451 (외부 차단) |

### E2E 검증 (타이머 → 서비스 → 갱신)

| # | 검증 항목 | 결과 |
|---|----------|------|
| 1 | 타이머 발동 시각 | ✅ 정확히 04:28:00 GMT |
| 2 | Service 실행 | ✅ exit 0 / SUCCESS |
| 3 | 소요 시간 | ✅ 38초 (live-test 12개) |
| 4 | opencode.json mtime 변경 | ✅ 갱신 확인 |
| 5 | 업스트림 다양성 | ✅ 3개 org (nvidia/cohere/liquid) |
| 6 | 타이머 복원 | ✅ 15:30 UTC로 복원 |

### 운영 모델 429 현황

| 구분 | 429 발생 | 키 분산 |
|------|---------|---------|
| 운영 모델 (선정된 Top 3) | **0건** | ✅ 균등 (23/21/19) |
| Live-test 모델 (탐색용) | 101건 (업스트림 공유 풀) | — |

## 참고: OpenRouter Rate Limit 정책 (공식 문서)

| 조건 | RPM | 일일 한도 |
|------|-----|-----------|
| 크레딧 < $10 | 20 RPM | 50 requests/day |
| 크레딧 ≥ $10 (우리 상황) | 20 RPM | 1,000 requests/day |

> "Making additional accounts or API keys **will not affect your rate limits**, as we govern capacity globally."
> — OpenRouter 공식 문서

**즉, 3개 키로 RPM을 3배 늘리는 건 공식 문서상 효과가 제한적이다.**
그러나 운영 모델에서 429=0건인 것은 실제로 모델별 rate limit이 다르고,
3개 키 분산이 부하를 낮추는 데 기여하기 때문으로 추정된다.

## 부록 A: 실패 분석 이력

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
```

### 원인 2: `chains` 배열이 여러 개여도 `chains[0]`만 사용된다

opencode v1.18.29 내장 config schema에서 `experimental` 섹션:
```json
"experimental": {
  "primary_tools": ["edit"],
  "mcp_timeout": 30000
}
```
→ `modelFallbackChain`은 이 스키마에 존재하지 않는다. 공식 지원 기능이 아니므로
  동작이 보장되지 않는다.

**로그 증거 (2026-09-08 00:09~00:30):**
```
00:09:31  model=minimax/minimax-m3:free  → "unavailable for free" ❌
00:13:32  model=laguna-s-2.1:free        → "Provider returned error" ❌
00:13:43  model=laguna-s-2.1:free        → 재시도 ❌
... (25회 이상 Laguna만 재시도, MiniMax로 돌아가지 않음)
```

### 원인 3: 모든 Free 모델이 종료됨

| 모델 | 에러 메시지 | 상태 |
|------|-----------|------|
| `minimax/minimax-m3:free` | "This model is unavailable for free. The paid version is available now - use this slug instead: minimax/minimax-m3" | 무료 종료 |
| `poolside/laguna-s-2.1:free` | "Provider returned error" | 무료 종료/에러 |
| `mimo-v2.5-free` (opencode 내장) | "Endpoint is unavailable" / "Rate limit exceeded" | 불가 |

### 타임라인

| 일자 | 이벤트 |
|------|--------|
| 2026-07-05 | 초기 설정: DeepSeek V4 Flash Free → Laguna XS 2.1 free fallback |
| 2026-08-26 | OpenRouter rate limiting + fallback models 구성 |
| 2026-09-04 | 3개 키 + 2개 모델(MiniMax M3, Laguna S 2.1)로 fallback chain 확장 |
| 2026-09-06 | MiMo free → rate limit / MiniMax M3 daily limit 도달 |
| 2026-09-07 | Gemini 3.8 Flash → 크레딧 부족 / MiniMax M3 free → 무료 종료 |
| 2026-09-08 | 모든 free 모델 사망, Laguna S 2.1만 25회 연속 실패 |
| 2026-09-08 | **opencode-ai/opencode 저장소 archived** |
| 2026-09-08 | **RR 프록시 + 자동 갱신 시스템 구축 완료** |

## 와치독 통합 (2026-09-08)

RR 프록시와 갱신 타이머는 `devforge-watchdog`가 관리한다.

| 타깃 | 종류 | 와치독 역할 | max_idle |
|------|------|------------|----------|
| `openrouter-rr-proxy` | **SERVICE_TARGETS** | 다운 시 자동 재시작 | — |
| `devforge-openrouter-free-models.timer` | **TIMER_TARGETS** | 26h 안에 안 돌면 Slack 알림 | 93600s |

**설정 위치**: `lib/watchdog/config.py`
```python
SERVICE_TARGETS = [
    "devforge-turn-watcher",
    "openrouter-rr-proxy",          # ← 추가 (자동 재시작)
]

TIMER_TARGETS = {
    ...
    "devforge-openrouter-free-models.timer": {"expected": "free_models", "max_idle": 93600},
}
```

**config 반영**: 와치독은 SIGHUP으로 리로드 지원
```bash
kill -HUP $(systemctl --user show devforge-watchdog.service -p MainPID --value)
```

## 유지보수 가이드

### 일상 점검

```bash
# 프록시 상태 확인 (와치독이 자동 재시작)
systemctl --user status openrouter-rr-proxy.service

# 타이머 상태 확인 (와치독이 26h idle 시 Slack 알림)
systemctl --user status devforge-openrouter-free-models.timer

# 다음 타이머 예정 시각
systemctl --user list-timers | grep openrouter

# 최근 갱신 로그
journalctl --user -u devforge-openrouter-free-models.service --since "1 hour ago"

# 와치독 감시 로그
journalctl --user -u devforge-watchdog.service --since "1 hour ago" | grep -i "free\|rr-proxy"

# 현재 적용된 모델 확인
python3 -c "import json; c=json.load(open('/home/opc/.config/opencode/opencode.json')); print(c['model']); print(c['experimental']['modelFallbackChain']['chains'][0])"
```

### 문제 해결

| 증상 | 확인 사항 | 조치 |
|------|----------|------|
| 프록시 502 | `journalctl -u openrouter-rr-proxy` | 키 만료 확인, `secrets.env` 점검 |
| 타이머 실행 안 됨 | `systemctl --user list-timers` | `systemctl --user enable --now devforge-openrouter-free-models.timer` |
| 갱신 후 모델 전부 429 | refresh 스크립트 재실행 | `--force`로 캐시 무시, 수동으로 live-test 재시도 |
| opencode 설정 안 됨 | opencode 세션 재시작 | `modelFallbackChain`은 세션 시작 시 읽힘 |

### 수동 강제 갱신

```bash
cd /opt/projects/server/scripts
PYTHONPATH=/opt/projects/server/scripts python3.11 -m proxies.refresh_openrouter_free_models --force
```

## 부록 B: 해결책 비교 (프로젝트 선정 사유)

| 비교 축 | **Aculeasis/openrouter-proxy** | **Naveenxyz/openrouterproxy** ⭐ | **NousResearch/hermes-agent** |
|---------|-------------------------------|---------------------------------|-------------------------------|
| 배포 형태 | 프록시 (독립) | 프록시 (독립) | ❌ 클라이언트 완전 교체 필요 |
| opencode 호환 | ✅ base_url만 변경 | ✅ base_url만 변경 | ❌ hermes-agent로 대체 |
| 429 cooldown | ✅ 4시간 자동 | ❌ 없음 | ✅ 1h |
| 설정 복잡도 | config.yml + 설치 스크립트 | .env 파일 1줄 | CLI 명령어 |
| 선정 사유 | 불필요한 오버헤드 | ⭐ 요구사항에 정확히 일치 | 탈락 (클라이언트 교체) |

## 참고 링크

- RR 프록시 소스: `/opt/projects/server/scripts/proxies/openrouter_rr_proxy.py`
- 갱신 스크립트: `/opt/projects/server/scripts/proxies/refresh_openrouter_free_models.py`
- opencode 설정: `/home/opc/.config/opencode/opencode.json`
- 시크릿: `/home/opc/.config/devforge/secrets.env`
- 캐시: `/home/opc/.cache/devforge/openrouter_free_models.json`
- OpenRouter 공식 문서: https://openrouter.ai/docs/api_reference/limits.md
- OpenRouter BYOK: https://openrouter.ai/workspaces/default/byok