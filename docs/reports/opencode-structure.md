# OpenCode — 실행 구조 & 업데이트 이력

> 최종 갱신: 2026-09-09
> 대상: `~/.config/opencode/`, `~/.local/bin/opencode*`, `~/.bashrc.d/`
> 관련 문서: `opencode-roundrobin-failure-analysis.md` (RR 프록시/자동 갱신), `system-architecture.md`

---

## 1. 개요

`opencode`는 Go 기반 터미널 AI 코딩 에이전트(opencode-ai/opencode)다.
이 서버에서는 두 가지 실행 경로를 제공한다.

- **기본 `opencode`**: `opencode-go` provider(`~/.local/share/opencode/auth.json`,
  type=api)의 **`deepseek-v4-flash` 고정 모델**을 사용하는 안정 경로.
- **`opencode-rr`**: 로컬 **RR 프록시(:8451, 모델별 계정 고정 + 미지정 모델 라운드로빈)**를
  경유하고, 기본 모델은 매일 자동 갱신되는 **최고 무료 모델**을 사용하는 경로.

```
opencode (기본·고정)                opencode-rr (자동·무료)
       │                                  │
       ▼                                  ▼
 opencode-go provider                 local RR 프록시
 (auth.json, deepseek-v4-flash)       http://127.0.0.1:8451/v1
       │                                  │ (모델별 1계정 고정)
       ▼                                  │ + 매일 free 모델 자동 갱신
   [opencode-go]                          ▼
                                     [OpenRouter]
```

2026-09-09 이전에는 opencode가 **항상 RR 프록시**만 사용했고, 당일 오전에
**기본=직접 OpenRouter / rr=프록시**로 1차 분리했다. 이후 13:58에 기본 모드를
**직접 OpenRouter → `opencode-go/deepseek-v4-flash`**로 바꾸고, openrouter(Direct)
provider 블록은 전역 설정에서 제거해 모델 피커/헤더의 혼선(`(Direct)` 라벨)을 없앴다
(아래 §2, §4, §9). RR 프록시 상세는 `opencode-roundrobin-failure-analysis.md` 참고.

---

## 2. 실행 분기 (핵심)

| 실행 명령 | 사용 설정 | Provider 경로 | 의미 |
|---|---|---|---|
| `opencode` | `~/.config/opencode/opencode.json` | `opencode-go` (`opencode-go/deepseek-v4-flash`) | 기본(고정·안정) |
| `opencode-rr` | opencode.json base + `opencode-rr.json` override(OPENCODE_CONFIG) | 로컬 RR 프록시 (free 자동 갱신) | 프록시 |

**구현 방식**: opencode의 config는 deep-**merge**(교체 아님)다. 로드 순서는
global config → `OPENCODE_CONFIG`(→ project) 순이라, `opencode-rr.json`이 전역 설정의
`model`/`provider`를 **덮어쓴다**(config.ts: `merge(Global…)` → `merge(Flag.OPENCODE_CONFIG,…)`).
base의 MCP·명령·에이전트는 그대로 보존된다.

### 2.1 기본(고정) — `opencode`
- `model` = `opencode-go/deepseek-v4-flash`
- provider `opencode-go`: auth.json에 등록(type=api). config에 provider 블록 없음.
- `provider.openrouter`(Direct) 블록 **없음** — 헤더/피커에 `OpenRouter (Direct)`가
  뜨는 혼선을 제거하기 위해 rr용 openrouter 설정은 `opencode-rr.json`에만 둔다.

### 2.2 프록시 — `opencode-rr`
`~/.local/bin/opencode-rr` 래퍼:
```bash
#!/bin/bash
set -euo pipefail
export OPENCODE_CONFIG="${OPENCODE_CONFIG:-$HOME/.config/opencode/opencode-rr.json}"
exec opencode "$@"
```
`opencode-rr.json`은 RR 프로필 전체를 담는다 (provider + 기본 model + fallback 체인):
```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
  "experimental": {
    "modelFallbackChain": {
      "timeoutMs": 60000,
      "chains": [["openrouter/nvidia/nemotron-3-super-120b-a12b:free",
                  "openrouter/cohere/north-mini-code:free",
                  "openrouter/google/gemma-4-26b-a4b-it:free"]]
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
      "models": { "...": { "name": "..." } }
    }
  }
}
```
이 파일의 `model`/`experimental.modelFallbackChain`/`provider.openrouter.models`는
**매일 00:30 KST `devforge-openrouter-free-models` 타이머가 라이브테스트 후 자동 갱신**한다
(쓰기 대상 = `opencode-rr.json`. 전역 설정은 건드리지 않음, §7 참고).

> **참고**: `modelFallbackChain`은 단일 요청 내 선형 fallback이지 요청 간 라운드로빈이
> 아니다. 모델별 계정 고정/미지정 모델 라운드로빈은 RR 프록시 자체가 담당한다
> (`opencode-roundrobin-failure-analysis.md` 참고).

---

## 3. 설정 파일 인벤토리

| 파일 | 역할 | 유형 |
|---|---|---|
| `~/.config/opencode/opencode.json` | 기본(고정) 모델(`opencode-go/deepseek-v4-flash`) + MCP 15개 | 운영 |
| `~/.config/opencode/opencode-rr.json` | RR 프록시 provider + 기본 model/fallback + **모델→계정 고정 순서** (타이머 자동 갱신) | 운영 |
| `~/.config/opencode/opencode.jsonc` | global 최소($schema만) | 뼈대 |
| `~/.config/opencode/opencode.json.bak_*` | 변경 전 백업 (복원용) | 백업 |
| `~/.config/opencode/opencode-rr.json.bak_*` | 변경 전 백업 (복원용) | 백업 |
| `~/.local/bin/opencode` | opencode 바이너리 → `opencode-ai/bin/opencode.exe` | 운영 |
| `~/.local/bin/opencode-rr` | 프록시 모드 래퍼 | 운영 |
| `~/.local/bin/opencode-deepdive` | DeepDive MCP 토글 (`on/off/status`) | 보조 |
| `~/.bashrc.d/opencode-aliases` | `open-newhand`(handover 주입 실행) | 보조 |
| `~/.bashrc.d/claude-env` | secrets.env 소싱 + OpenRouter 키 export | 보조 |

**백업 규약**: config 수정 전 `cp opencode.json opencode.json.bak_<YYYYMMDD_HHMMSS>`
(및 동일 규칙으로 `opencode-rr.json`도 백업).

---

## 4. Provider & 모델

### 4.1 기본(고정) 모델 — `opencode-go`
`~/.config/opencode/opencode.json` 최상위:
```json
"model": "opencode-go/deepseek-v4-flash"
```
- provider `opencode-go`는 `~/.local/share/opencode/auth.json`(type=api)로 인증.
- openrouter(Direct) provider 블록은 제거됨 → rr용 openrouter는 아래 4.2에만 존재.

### 4.2 프록시 — openrouter (ORP)
`~/.config/opencode/opencode-rr.json`:
```json
"provider": { "openrouter": {
  "npm": "@ai-sdk/openai-compatible",
  "name": "ORP",
  "options": { "baseURL": "http://127.0.0.1:8451/v1",
               "apiKey": "local-rr-proxy" },
  "models": {
    "nvidia/nemotron-3-ultra-550b-a55b:free": { "name": "NVIDIA: Nemotron 3 Ultra" },
    "google/gemma-4-26b-a4b-it:free":         { "name": "Google: Gemma 4 26B A4B" },
    "cohere/north-mini-code:free":            { "name": "Cohere: North Mini Code" }
  }
}}
```
- `model` = `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free` (시드, 타이머가 갱신)
- `experimental.modelFallbackChain.chains[0]` = `[nemotron-3-ultra, gemma-4-26b, north-mini-code]`
- **모델→계정 고정**: `provider.openrouter.models`의 **순서**대로 프록시가 계정에 1:1 배정
  (`models[0]`→MESIDS, `models[1]`→MINIPARK4U, `models[2]`→HYEONMINPARK4U).
  분당 제한은 OpenRouter가 전역 관리라 회피 불가 → 각 모델의 **일일 쿼터를 한 계정에
  고정**해 fallback 체인(모델 A→B→C)이 서로 다른 계정의 쿼터를 사용하게 한다.
  프록시는 mtime으로 이 파일을 재로드하므로 타이머 갱신 시 자동 반영 (재시작 불필요).
- **자동 갱신**: `provider.openrouter.models`/`model`/`chain`은 매일 00:30 KST
  `devforge-openrouter-free-models` 타이머가 라이브테스트 후 top-3로 덮어쓴다
  (`refresh_openrouter_free_models.py`의 `OPCODE_CONFIG` = `opencode-rr.json`).
  baseURL/apiKey/name 등 나머지 키는 보존된다.
- 2026-09-09 13:58 이전에는 이 자동 갱신이 전역 `opencode.json`을 덮어써 기본(직접)
  모델을 깨뜨렸으나, 이후 쓰기 대상을 `opencode-rr.json`으로 전환해 전역은 불변.

---

## 5. 환경변수 & 시크릿

- `OPENROUTER_MESIDS_API_KEY` / `_MINIPARK4U_` / `_HYEONMINPARK4U_`
  - 정의: `~/.config/devforge/secrets.env` (3개 OpenRouter 키)
  - RR 프록시(`openrouter-rr-proxy.service`)가 이 파일에서 직접 키를 로드하고,
    **모델별 전용 계정 고정**(`models[0]`→MESIDS, `models[1]`→MINIPARK4U, `models[2]`→HYEONMINPARK4U).
- 기본(고정) 모드(`opencode-go`)는 자체 키(`auth.json`)를 쓰므로 **OpenRouter env 불필요**.
- `~/.bashrc.d/claude-env`의 키 export는 RR 프록시/타 클라이언트가 env로 키를 쓸 수 있게
  유지한다(2026-09-09 키 미상속 버그 수정 내역 §9 참고).

> 주의: 키를 config에 직접 하드코딩 금지 — env 인터폴레이션(`{env:VAR}`)만 사용.

---

## 6. MCP 서버 인벤토리

`~/.config/opencode/opencode.json`의 `mcp` (15개 정의, 12개 활성):

| 이름 | type | 상태 | 용도 |
|---|---|---|---|
| devforge-mcp | remote | ON | devforge 파이프라인/팩트/딥다이브 |
| search-proxy | local | ON | Brave/Tavily/you.com 자동 회전 검색 |
| exa-search | local | ON | 웹 검색/콘텐츠 추출 |
| context7 | local | ON | 라이브러리 문서/검증 |
| fetch | local | ON | URL fetch |
| filesystem | local | ON | `/opt/projects/server` 파일 접근 |
| github | local | ON | GitHub 작업 |
| lsp | local | ON | 코드 인텔리전스 |
| opencode-db | local | ON | opencode 세션 DB |
| shrimp-task-manager | local | ON | 태스크 관리 |
| time | local | ON | 시간 변환 |
| yggdrasil | local | ON | 심층 플래닝(sequential_thinking) |
| git | local | OFF | (비활성) |
| postgres | local | OFF | (비활성) |
| token-savior | local | OFF | (비활성) |

**DeepDive 토글** (`opencode-deepdive`)로 exa-search/context7/shrimp-task-manager/yggdrasil/lsp를
리스트에서 켜고 끈다.

---

## 7. 관련 systemd 유닛

| 유닛 | 역할 | 상태 |
|---|---|---|
| `openrouter-rr-proxy.service` | RR 프록시 (127.0.0.1:8451, 모델별 계정 고정) | enabled/active |
| `devforge-openrouter-free-models.{service,timer}` | 매일 00:30 KST free 모델 자동 갱신(구글 제외+최종 검증) → **opencode-rr.json** | enabled |
| `devforge-watchdog.service` | RR 프록시·타이머 감시 (`SERVICE_TARGETS`/`TIMER_TARGETS`) | 운영 |

```bash
systemctl --user status openrouter-rr-proxy.service
systemctl --user list-timers | grep openrouter
```

---

## 8. 검증 방법 (양방향)

opencode와 동일한 deep-merge로 유효 config 재구성해 확인:

```bash
bash -c '
python3 - <<PY
import json
import os
def merge(b, o):
    out = dict(b)
    for k, v in o.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out

base = json.load(open(os.path.expanduser("~/.config/opencode/opencode.json")))
rr = json.load(open(os.path.expanduser("~/.config/opencode/opencode-rr.json")))
mcp = sum(1 for v in base.get("mcp", {}).values() if v.get("enabled"))

print("default | model=", base.get("model"),
      "| providers=", list(base.get("provider", {}).keys()), "| mcp=", mcp)

merged = merge(base, rr)
o = merged["provider"]["openrouter"]["options"]
print("rr      | model=", merged.get("model"),
      "| baseURL=", o["baseURL"], "| apiKey=", o["apiKey"],
      "| chain0=", merged["experimental"]["modelFallbackChain"]["chains"][0][:1],
      "| mcp=", mcp)
PY
'
```

결과 기대치:
- default: `model=opencode-go/deepseek-v4-flash`, `providers=[]`, `mcp=12`
- rr: `model=openrouter/<최고 무료 모델>`(타이머 갱신 시 변동),
  `baseURL=http://127.0.0.1:8451/v1`, `apiKey=local-rr-proxy`, `mcp=12`
- 프록시 계정 고정 맵: `curl -s http://127.0.0.1:8451/health` → `pinned` 필드로 확인

---

## 9. 업데이트 이력

| 일자 | 변경 | 사유/결정 |
|---|---|---|
| 2026-07-05 | 초기 opencode 구성 (free 모델) | — |
| 2026-09-04 | OpenRouter 3키 + 2모델 fallback 확장 | RPM/rate-limit 회피 시도 |
| 2026-09-08 | RR 프록시+자동 갱신 시스템 구축 (8451) | free 모델 사망(전량 429) 원인 분석, `opencode-roundrobin-failure-analysis.md` |
| 2026-09-08 | default model → `nvidia/nemotron-3-ultra-550b-a55b:free` + 3개 fallback | 매일 자동 갱신 대상 |
| 2026-09-09 13:25 | **default model → `deepseek/deepseek-v4-flash-vision-exp`** (env 인터폴레이션 도입) | 프록시에 없는 gemma 모델 429 |
| 2026-09-09 13:29 | **default model → `deepseek/deepseek-v4-flash`** (비전 제거) | 사용자가 기본 모델 지정 |
| 2026-09-09 13:33 | **2모드 분리**: 기본=직접 OpenRouter / `opencode-rr`=RR 프록시 | `opencode`는 기본 설정으로, `opencode-rr`만 프록시 |
| 2026-09-09 13:35 | **키 export 버그 수정**: `~/.bashrc.d/claude-env`에 OpenRouter 키 `export` | secrets.env가 export 없이 할당 → opencode(자식)가 상속 못 함(빈 키), 양방향 검증서 발견 |
| 2026-09-09 13:58 | **기본=`opencode-go/deepseek-v4-flash` 고정, 전역에서 openrouter(Direct) 제거** / `opencode-rr.json`에 RR provider+model/fallback을 완전 포함, 자동 갱신 쓰기 대상도 rr로 전환 | rr 기본이 `(Direct)` deepseek로 뜨던 문제 + 모델 피커/헤더 혼선 제거. rr 기본 = 매일 자동 추천 무료 모델 |
| 2026-09-14 | **모드명 `OpenRouter (RR Proxy)` → `ORP`** / 프록시 **모델→계정 고정**(일일 쿼터 분산, mtime 자동 재로드) / 선정 시 **구글 제외 + 최종 더미 검증** | 모드명 축약, 분당 제한(전역) 회피 불가 → 일일 한도만 계정별 분산. 구글 free 모델 업스트림 오류 잦음 |

**버그(2026-09-09) 요약**:
- 증상: 기본(직접) 모드에서 `{env:OPENROUTER_MESIDS_API_KEY}`가 자식 프로세스에서 빈 값
- 원인: `OPENROUTER_MESIDS_API_KEY=...`(export 없음) → shell 로컬만, `opencode` 미상속
- 수정: `~/.bashrc.d/claude-env`에서 `[ -n "${!VAR:-}" ] && export "$VAR"`
- 검증: parent len=73 → child len=73 (수정 전 child=0)

> 참고: 13:58 이후 기본 모드는 `opencode-go` 키(`auth.json`)를 쓰므로 위 OpenRouter env
> export는 기본 모드와 무관해졌다(§5).

---

## 10. 유지보수

- 기본 모드가 인증 오류이면: `~/.local/share/opencode/auth.json`의 `opencode-go` 키 확인
  (`opencode auth list`). 기본 모델 변경 시 전역 `model` 키만 수정.
- 프록시 모드가 502면: `systemctl --user status openrouter-rr-proxy.service`,
  `journalctl --user -u openrouter-rr-proxy` 로 키 만료 점검
- rr 기본 모델이 429/실패면(상위 shared-pool): 즉시 수동 갱신
  `python3.11 /opt/projects/server/scripts/proxies/refresh_openrouter_free_models.py`
  (쓰기 대상 = `opencode-rr.json`, 전역은 불변)
- 프록시 계정 고정 맵 확인/조정: `curl -s http://127.0.0.1:8451/health` →
  `pinned`이 `models` 순서와 일치해야 함. 순서 바꾸면 mtime 자동 재로드(재시작 불필요)
- config 수정 후 opencode는 **재시작**해야 반영 (시작 시 1회 로드, 핫리로드 없음)
