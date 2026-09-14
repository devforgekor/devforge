# OpenRouter Free Model 시스템 — 운영 문서

> 최종 갱신: 2026-09-14
> 상태: 운영 중 (E2E 검증 완료)

## 시스템 개요

3개의 OpenRouter 계정(MESIDS, MINIPARK4U, HYEONMINPARK4U)을 사용한다.
**선정된 모델 1개는 전용 계정 1개에 고정**(모델별 1:1 매핑)하여 분당 제한 대신
**일일 쿼터를 계정별로 분산**한다. 매일 자동으로 최신 무료 모델 Top 3를
선정(구글 제외 + 최종 더미 검증 통과분)하여 opencode-rr.json을 갱신한다.

```
                    매일 15:30 UTC (00:30 KST)
                    ┌──────────────────────────────┐
                    │  refresh_openrouter_free_    │
                    │  models.py (oneshot)          │
                    │  ├─ OpenRouter catalog fetch  │
                    │  ├─ coding_index 기준 랭킹    │
                    │  ├─ live-test (Top 15)        │
                    │  ├─ 최종 검증 (dummy+재시도)   │
                    │  └─ opencode-rr.json 자동 갱신│
                    └──────┬───────────────────────┘
                           │ writes
                           ▼
opencode-rr.json ──> openrouter-rr-proxy.service (127.0.0.1:8451)
                     ├─ model[0] → key[1] MESIDS (고정)
                     ├─ model[1] → key[2] MINIPARK4U (고정)
                     ├─ model[2] → key[3] HYEONMINPARK4U (고정)
                     └─ 미지정 모델 → 라운드로빈 (fallback)

opencode-rr ──> http://127.0.0.1:8451/v1 (1개 provider, 모드명 ORP)
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
| `/health` | GET | 헬스체크 (pinned 모델→계정 맵 포함) |

**키 로딩 순서:**
1. `~/.config/devforge/secrets.env` 파일 직접 파싱
2. (fallback) 환경변수 `OPENROUTER_MESIDS_API_KEY` 등

**주요 특징:**
- **모델→계정 고정**: `opencode-rr.json`의 `provider.openrouter.models` **순서**대로
  계정에 1:1 배정 (`models[0]`→MESIDS, `models[1]`→MINIPARK4U, `models[2]`→HYEONMINPARK4U)
- 고정 모델은 전용 계정으로만 라우팅 — **계정 간 fallback 없음** (429/실패 시 그대로
  반환 → opencode의 `modelFallbackChain`이 다음 모델로 진행, 다음 모델은 다른 계정)
- 미지정/미상 모델은 `_next_key()` 라운드로빈 (기존 동작 유지)
- `opencode-rr.json` mtime 체크로 매일 갱신분을 **재시작 없이 자동 재로드**
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
2. :free 모델 필터링 (12개 후보)
3. 도메인 특화 모델 제외 (sante, fin, japanese, content-safety 등) + **구글 모델 제외**
   (구글 free 모델은 업스트림 API 오류 잦음 → 호출 불안정)
4. coding_index + context_bonus 기준 스코어링
   - benchmark 있음 → coding_index + context_bonus (30~60점)
   - benchmark 없음 → 29.9점 이하로 캡 (검증된 모델 우선)
5. Live-test: Top 15 모델을 프록시 통해 1회씩 호출 (0.3초 간격)
6. 업스트림 org별 그룹화 (nvidia/cohere/liquid 등)
   - 동일 org에서 최고 점수 1개만 선택
7. **최종 검증**: 선정 후보(org별 1위)에 "hello" 더미 호출을 최대 3회 재시도
   - 실제 응답하는 모델만 최종 선정 (통과 못하면 다음 org 후보로 대체)
8. 상위 3개 org의 모델을 opencode.json에 기록
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
| `scripts/proxies/openrouter_rr_proxy.py` | 3계정 프록시, 모델→계정 고정 (FastAPI, port 8451) | 운영 |
| `scripts/proxies/refresh_openrouter_free_models.py` | 매일 free 모델 자동 갱신 (구글 제외 + 최종 검증) | 운영 |
| `~/.config/systemd/user/openrouter-rr-proxy.service` | RR 프록시 서비스 | 운영 |
| `~/.config/systemd/user/devforge-openrouter-free-models.service` | 갱신 oneshot 서비스 | 운영 |
| `~/.config/systemd/user/devforge-openrouter-free-models.timer` | 갱신 타이머 (매일 15:30 UTC) | 운영 |
| `~/.config/opencode/opencode-rr.json` | opencode 설정 (자동 갱신 대상, 모드명 `ORP`) | 운영 |
| `~/.config/devforge/secrets.env` | 3개 OpenRouter API 키 | 시크릿 |
| `~/.cache/devforge/openrouter_free_models.json` | 모델 캐시 (24h TTL) | 캐시 |
| `docs/reports/opencode-roundrobin-failure-analysis.md` | 본 문서 | 문서 |

## 설정 파일 (opencode-rr.json)

```json
{
  "model": "openrouter/inclusionai/ling-3.0-flash-vl:free",
  "experimental": {
    "modelFallbackChain": {
      "timeoutMs": 60000,
      "chains": [
        [
          "openrouter/inclusionai/ling-3.0-flash-vl:free",
          "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
          "openrouter/cohere/north-mini-code:free"
        ]
      ]
    }
  },
  "provider": {
    "openrouter": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "ORP",
      "options": {
        "baseURL": "http://127.0.0.1:8451/v1",
        "apiKey": "local-rr-proxy"
      },
      "models": {
        "inclusionai/ling-3.0-flash-vl:free": { "name": "ORP-1(free)" },
        "nvidia/nemotron-3-ultra-550b-a55b:free": { "name": "ORP-2(free)" },
        "cohere/north-mini-code:free": { "name": "ORP-3(free)" }
      }
    }
  }
}
```

> `provider.openrouter.models`의 **키 순서** = 프록시의 모델→계정 고정 순서.
> 순서를 바꾸면 프록시가 mtime으로 자동 재로드한다 (재시작 불필요).

**변경 전후 비교:**

| 항목 | 변경 전 (실패) | 변경 후 (운영) |
|------|---------------|---------------|
| provider 수 | 3개 (openrouter, -minipark4u, -hyeonminpark4u) | 1개 (openrouter) |
| baseURL | https://openrouter.ai/api/v1 | http://127.0.0.1:8451/v1 |
| apiKey | 3개 개별 키 (하드코딩) | local-rr-proxy (프록시가 교체) |
| 모델 | 2개 (minimax, laguna) | 매일 자동 갱신 (3개, 서로 다른 업스트림) |
| 계정 분산 | 요청마다 라운드로빈 (RPM 회피 목적) | **모델별 계정 고정 (일일 쿼터 분산)** |
| 선정 규칙 | top-3 org (단순) | top-3 org + **구글 제외** + **최종 더미 검증** |
| fallback 전략 | 1개 체인, 6개 동일 업스트림 | 1개 체인, 3개 다른 업스트림 |

## 일일 운영 사이클

```
15:30 UTC (00:30 KST)
  └─ devforge-openrouter-free-models.timer fires
       └─ devforge-openrouter-free-models.service (oneshot)
            ├─ OpenRouter 모델 카탈로그 fetch (~1s)
            ├─ 12개 free 모델 스코어링 (~0.1s) — 구글 제외
            ├─ 15개 모델 live-test (~40s)
            │   └─ 각 모델 1회 호출 → 0.3s 간격
            ├─ 최종 검증: 선정 후보 dummy 호출 (최대 3회 재시도)
            └─ opencode-rr.json 갱신 (~0.1s)
                 └─ 프록시 mtime 재로드 + 다음 opencode 세션 시 적용

opencode-rr 세션 중:
  └─ http://127.0.0.1:8451/v1 (RR 프록시, 모드명 ORP)
       ├─ 선정 모델 → 전용 계정 1개로 고정 라우팅
       ├─ 실패(429 등) → 계정 간 fallback 없이 그대로 반환
       │   └─ opencode modelFallbackChain이 다음 모델(다른 계정)로 진행
       └─ 미지정 모델 → 라운드로빈 + 429 재시도
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
| 9 | opencode-rr.json 설정 일치 | ✅ baseURL, model, chain 일치 |
| 10 | OpenAI 클라이언트 호환 | ✅ Bearer auth + 전체 응답 |
| 11 | Stress 10 concurrent | ✅ Race condition 없음 |
| 12 | 메모리 | ✅ RSS 60MB |
| 13 | 포트 바인딩 | ✅ 127.0.0.1:8451 (외부 차단) |

### 핀 고정 검증 (2026-09-14)

| # | 검증 항목 | 결과 |
|---|----------|------|
| 1 | Health pinned 맵 | ✅ `nemotron→MESIDS, gemma→MINIPARK4U, north→HYEONMINPARK4U` |
| 2 | 모델별 고정 라우팅 | ✅ nemotron→key[1] MESIDS, gemma→key[2] MINIPARK4U, north→key[3] HYEONMINPARK4U |
| 3 | mtime 자동 재로드 | ✅ 순서 변경 시 재시작 없이 새 매핑 반영 |
| 4 | 구글 제외 | ✅ 후보에서 google 모델 미포함 |
| 5 | 최종 더미 검증 | ✅ 선정 후보 dummy 호출 통과분만 기록 |
| 6 | 핀 고정 모델 실호출 | ✅ 새 primary(ling-3.0-flash-vl) 200 OK |

### E2E 검증 (타이머 → 서비스 → 갱신)

| # | 검증 항목 | 결과 |
|---|----------|------|
| 1 | 타이머 발동 시각 | ✅ 정확히 04:28:00 GMT |
| 2 | Service 실행 | ✅ exit 0 / SUCCESS |
| 3 | 소요 시간 | ✅ 38초 (live-test 12개) |
| 4 | opencode-rr.json mtime 변경 | ✅ 갱신 확인 |
| 5 | 업스트림 다양성 | ✅ 3개 org (inclusionai/nvidia/cohere) |
| 6 | 타이머 복원 | ✅ 15:30 UTC로 복원 |

### 운영 모델 429 현황

| 구분 | 429 발생 | 계정 분산 |
|------|---------|---------|
| 운영 모델 (선정된 Top 3) | **0건** | ✅ 모델별 전용 계정 (1:1) |
| Live-test 모델 (탐색용) | 101건 (업스트림 공유 풀) | — |

## 참고: OpenRouter Rate Limit 정책 (공식 문서)

| 조건 | RPM | 일일 한도 |
|------|-----|-----------|
| 크레딧 < $10 | 20 RPM | 50 requests/day |
| 크레딧 ≥ $10 (우리 상황) | 20 RPM | 1,000 requests/day |

> "Making additional accounts or API keys **will not affect your rate limits**, as we govern capacity globally."
> — OpenRouter 공식 문서

**즉, 3개 키로 RPM을 3배 늘리는 건 공식 문서상 효과가 제한적이다.**
따라서 (2026-09-14부터) 요청마다 라운드로빈하는 대신 **모델별 계정을 고정**하여
각 모델의 **일일 쿼터**(1,000 req/day)를 계정별로 분산한다. 분당 제한은 어차피
전역 관리라 회피 불가하므로 포기하고, 대신 fallback 체인이 서로 다른 계정을
타게 해 일일 한도만 회피한다.

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
| 2026-09-14 | **모드명 ORP 변경 + 모델→계정 고정(일일 쿼터 분산) + 구글 제외 + 최종 더미 검증** |

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

# 현재 적용된 모델 + 계정 고정 맵 확인
python3 -c "import json; c=json.load(open('/home/opc/.config/opencode/opencode-rr.json')); print('model=', c['model']); print('chain0=', c['experimental']['modelFallbackChain']['chains'][0])"
curl -s http://127.0.0.1:8451/health
```

### 문제 해결

| 증상 | 확인 사항 | 조치 |
|------|----------|------|
| 프록시 502 | `journalctl -u openrouter-rr-proxy` | 키 만료 확인, `secrets.env` 점검 |
| 타이머 실행 안 됨 | `systemctl --user list-timers` | `systemctl --user enable --now devforge-openrouter-free-models.timer` |
| 갱신 후 모델 전부 429 | refresh 스크립트 재실행 | `--force`로 캐시 무시, 수동으로 live-test 재시도 |
| opencode 설정 안 됨 | opencode 세션 재시작 | `modelFallbackChain`은 세션 시작 시 읽힘 |
| 계정 고정 맵이 안 맞음 | `curl :8451/health` | opencode-rr.json `models` 순서 변경 → 프록시 mtime 자동 재로드 |

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
- opencode RR 설정: `/home/opc/.config/opencode/opencode-rr.json` (모드명 ORP)
- 시크릿: `/home/opc/.config/devforge/secrets.env`
- 캐시: `/home/opc/.cache/devforge/openrouter_free_models.json`
- OpenRouter 공식 문서: https://openrouter.ai/docs/api_reference/limits.md
- OpenRouter BYOK: https://openrouter.ai/workspaces/default/byok