# OpenCode — 실행 구조 & 업데이트 이력

> 최종 갱신: 2026-09-09
> 대상: `~/.config/opencode/`, `~/.local/bin/opencode*`, `~/.bashrc.d/`
> 관련 문서: `opencode-roundrobin-failure-analysis.md` (RR 프록시/자동 갱신), `system-architecture.md`

---

## 1. 개요

`opencode`는 Go 기반 터미널 AI 코딩 에이전트(opencode-ai/opencode)다.
이 서버에서는 **OpenRouter 라운드로빈(RR) 프록시**와 **직접 OpenRouter** 두 가지 경로를
모두 제공하며, 실행 명령에 따라 전환한다.

```
opencode (기본·직접)              opencode-rr (프록시)
       │                                │
       ▼                                ▼
 OpenRouter 직접                     local RR 프록시
 https://openrouter.ai/api/v1        http://127.0.0.1:8451/v1
       │                                │
       │ (1개 키, 내 키)                  │ (3개 키 라운드로빈)
       ▼                                ▼
   [OpenRouter]                     [OpenRouter]
```

이전에는 opencode가 **항상 RR 프록시**만 사용했으나, 2026-09-09에
**기본=직접 / rr=프록시**로 분리했다(아래 §2). RR 프록시 상세는
`opencode-roundrobin-failure-analysis.md` 참고.

---

## 2. 실행 분기 (핵심)

| 실행 명령 | 사용 설정 | Provider 경로 | 의미 |
|---|---|---|---|
| `opencode` | `~/.config/opencode/opencode.json` | 직접 OpenRouter | 기본(비프록시) |
| `opencode-rr` | 동일 base + `opencode-rr.json` 오버라이드 | 로컬 RR 프록시 | 프록시 |

**구현 방식**: opencode의 config는 deep-**merge**(교체 아님)다. `OPENCODE_CONFIG`에
오버라이드 파일을 주면 global config와 병합되어 **모델·MCP는 보존**되고
`provider.openrouter.options`(baseURL/apiKey)만 덮어쓴다.

### 2.1 기본(직접) — `opencode`
- `provider.openrouter.options.baseURL` = `https://openrouter.ai/api/v1`
- `apiKey` = `{env:OPENROUTER_MESIDS_API_KEY}` (env 인터폴레이션)
- 키 소스: `~/.bashrc.d/claude-env` → `~/.config/devforge/secrets.env`
  (2026-09-09부터 `export` 추가, §5)

### 2.2 프록시 — `opencode-rr`
`~/.local/bin/opencode-rr` 래퍼:
```bash
#!/bin/bash
set -euo pipefail
export OPENCODE_CONFIG="${OPENCODE_CONFIG:-$HOME/.config/opencode/opencode-rr.json}"
exec opencode "$@"
```
`opencode-rr.json`은 오버라이드만 담는다:
```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "openrouter": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "OpenRouter (RR Proxy)",
      "options": {
        "baseURL": "http://127.0.0.1:8451/v1",
        "apiKey": "local-rr-proxy"
      }
    }
  }
}
```

> **참고**: `modelFallbackChain`은 단일 요청 내 선형 fallback이지 요청 간 라운드로빈이
> 아니다. 프록시 라운드로빈은 RR 프록시 자체가 담당한다(`opencode-roundrobin-failure-analysis.md` 부록 A).

---

## 3. 설정 파일 인벤토리

| 파일 | 역할 | 유형 |
|---|---|---|
| `~/.config/opencode/opencode.json` | 기본(직접) provider + 모델 + MCP | 운영 |
| `~/.config/opencode/opencode-rr.json` | RR 프록시 오버라이드 (OPENCODE_CONFIG용) | 운영 |
| `~/.config/opencode/opencode.jsonc` | global 최소($schema만) | 뼈대 |
| `~/.config/opencode/opencode.json.bak_*` | 변경 전 백업 (복원용) | 백업 |
| `~/.local/bin/opencode` | opencode 바이너리 → `opencode-ai/bin/opencode.exe` | 운영 |
| `~/.local/bin/opencode-rr` | 프록시 모드 래퍼 (신규) | 운영 |
| `~/.local/bin/opencode-deepdive` | DeepDive MCP 토글 (`on/off/status`) | 보조 |
| `~/.bashrc.d/opencode-aliases` | `open-newhand`(handover 주입 실행) | 보조 |
| `~/.bashrc.d/claude-env` | secrets.env 소싱 + OpenRouter 키 export | 보조 |

**백업 규약**: config 수정 전 `cp opencode.json opencode.json.bak_<YYYYMMDD_HHMMSS>`.

---

## 4. Provider & 모델

### provider.openrouter (기본)
```json
"provider": { "openrouter": {
  "npm": "@ai-sdk/openai-compatible",
  "name": "OpenRouter (Direct)",
  "options": { "baseURL": "https://openrouter.ai/api/v1",
               "apiKey": "{env:OPENROUTER_MESIDS_API_KEY}" },
  "models": {
    "deepseek/deepseek-v4-flash":            { "name": "DeepSeek: V4 Flash" },
    "nex-agi/nex-n2.5-mini:free":             { "name": "Nex: N2.5 Mini" },
    "inclusionai/ling-3.0-flash-sante:free":  { "name": "Ling: 3.0 Flash Sante" }
  }
}}
```
- `model` = `openrouter/deepseek/deepseek-v4-flash`
- `experimental.modelFallbackChain.chains[0]` = `[deepseek-v4-flash, nex-n2.5-mini:free, ling-3.0-flash-sante:free]`
- 모델·체인은 매일 00:30 KST `devforge-openrouter-free-models` 타이머가 자동 갱신(과거 방식).
  현재는 기본=직접 모드로 전환되어, **직접 OpenRouter에서 유효한 모델**을 사용해야 한다.

---

## 5. 환경변수 & 시크릿

- `OPENROUTER_MESIDS_API_KEY` / `_MINIPARK4U_` / `_HYEONMINPARK4U_`
  - 정의: `~/.config/devforge/secrets.env` (3개 OpenRouter 키, 라운드로빈용)
  - **핵심**: secrets.env는 `export` 없이 할당 → shell 로컬. 2026-09-09부터
    `~/.bashrc.d/claude-env`에서 `export`하여 **자식 프로세스(=`opencode`)가 상속**.
- 기본(직접) 모드는 `{env:OPENROUTER_MESIDS_API_KEY}` 로 1개 키를 직접 사용.
- RR 프록시는 3개 키를 라운드로빈(시크릿은 `EnvironmentFile`로 프록시에 주입).

> 주의: 키를 config에 직접 하드코딩 금지 — env 인터폴레이션(`{env:VAR}`)만 사용.

---

## 6. MCP 서버 인벤토리

`~/.config/opencode/opencode.json`의 `mcp` (15개 정의, 12개 활성):

| 이름 | type | 상태 | 용도 |
|---|---|---|:---:|
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
| `openrouter-rr-proxy.service` | RR 프록시 (127.0.0.1:8451) | enabled/active |
| `devforge-openrouter-free-models.{service,timer}` | 매일 00:30 KST free 모델 자동 갱신 | enabled |
| `devforge-watchdog.service` | RR 프록시·타이머 감시 (`SERVICE_TARGETS`/`TIMER_TARGETS`) | 운영 |

```bash
systemctl --user status openrouter-rr-proxy.service
systemctl --user list-timers | grep openrouter
```

---

## 8. 검증 방법 (양방향)

`opencode`와 동일한 deep-merge로 유효 config 재구성해 확인:

```bash
bash -c '
source ~/.bashrc.d/claude-env 2>/dev/null
python3 - <<PY
import json, os
def merge(b, o):
    out = dict(b)
    for k, v in o.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out

def interp(k):
    if isinstance(k, str) and k.startswith("{" + "env:"):
        var = k[5:-1]
        return ("RESOLVED(len " + str(len(os.environ.get(var, ""))) + ")") if os.environ.get(var) else "EMPTY"
    return k

base = json.load(open(os.path.expanduser("~/.config/opencode/opencode.json")))
rr = json.load(open(os.path.expanduser("~/.config/opencode/opencode-rr.json")))
for c, n in [(base, "default"), (merge(base, rr), "rr")]:
    o = c["provider"]["openrouter"]["options"]
    print(n, "| model=", c["model"], "| baseURL=", o["baseURL"], "| apiKey=", interp(o["apiKey"]),
          "| mcp=", sum(1 for v in c.get("mcp", {}).values() if v.get("enabled")))
PY
'
```

결과 기대치:
- default: `baseURL=https://openrouter.ai/api/v1`, `apiKey=RESOLVED(len 73)`, `mcp=12/15`
- rr: `baseURL=http://127.0.0.1:8451/v1`, `apiKey=local-rr-proxy`, `mcp=12/15`

자식 상속 확인:
```bash
bash -c 'source ~/.bashrc.d/claude-env; echo parent=${#OPENROUTER_MESIDS_API_KEY}; \
         bash -c "echo child=\${#OPENROUTER_MESIDS_API_KEY}"'
# parent=73 / child=73 (export 없으면 child=0 → 오류)
```

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

**버그(2026-09-09) 요약**:
- 증상: 기본(직접) 모드에서 `{env:OPENROUTER_MESIDS_API_KEY}`가 자식 프로세스에서 빈 값
- 원인: `OPENROUTER_MESIDS_API_KEY=...`(export 없음) → shell 로컬만, `opencode` 미상속
- 수정: `~/.bashrc.d/claude-env`에서 `[ -n "${!VAR:-}" ] && export "$VAR"`
- 검증: parent len=73 → child len=73 (수정 전 child=0)

---

## 10. 유지보수

- 기본 모드가 401/키 오류이면: `source ~/.bashrc.d/claude-env` 후 키 존재 확인
- 프록시 모드가 502면: `systemctl --user status openrouter-rr-proxy.service`,
  `journalctl --user -u openrouter-rr-proxy` 로 키 만료 점검
- config 수정 후 opencode는 **재시작**해야 반영 (시작 시 1회 로드, 핫리로드 없음)
