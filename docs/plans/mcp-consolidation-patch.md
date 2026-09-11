# MCP 통합 패치 기록 (적용/미적용)

> 작성: 2026-09-11 · 계획: `docs/plans/mcp-consolidation-server-side.md` (v3) · 데이터: `docs/reports/mcp-cost-baseline.md`
> 원칙: 순차·검증 우선. 각 변경은 백업 → 적용 → 검증 → 기록.

---

## 1. 적용 완료

### P0 — 계측/백업
- 백업: `~/.claude/mcp.json.bak_20260911_144514`, `~/.config/opencode/opencode.json.bak_20260911_144514`.
- 실측: opencode DB(`~/.local/share/opencode/opencode.db`, 130세션·23,591 툴콜), 컨텍스트 avg cache_read 155,417 tok/메시지.
- 결론 반영: claude-only 집계는 **편향**(context7 0→47, fetch 0→87, github 0→67, filesystem 3→109, time 1→110).

### P1a — 대체가능 3종 제거 (opencode)
- 파일: `~/.config/opencode/opencode.json`
- 변경: `filesystem`·`github`·`time` → `"enabled": false`
- 절감: **8,768 tok/turn**
- 대체: filesystem→내장 Read/Edit/Glob/Grep, github→`gh` CLI, time→`date`
- 검증: JSON 유효(`opencode debug config`).

### P2 — lsp 툴 필터 (opencode)
- 파일: `~/.config/opencode/opencode.json` (`tools` 블록 추가)
- 변경: `"lsp_*": false` + 10종만 `true`
  - 노출: `start_lsp`, `blast_radius`, `find_references`, `find_symbol`, `inspect_symbol`, `get_symbol_source`, `get_diagnostics`, `suggest_fixes`, `rename_symbol`, `detect_lsp_servers`
- 절감: 68→10, **13,204 tok/turn** (측정: kept=3,129 / total=16,333)
- 검증(헤드리스 `opencode run`): 노출 10종 일치, **제외 툴 누출 0**.
- 비고: opencode는 lsp에 `mcp-trunc-proxy`를 쓰지 않아 `proxy_artifact_*`는 원래 없음(allowlist의  3종은 Claude 전용·opencode 무해).

### 누적 효과
| 상태 | 활성 MCP 스키마(tok/turn) |
|---|---:|
| 기준선 | ~36,700 |
| P1a 후 | ~27,900 |
| P2 후 | ~14,700 |
| **P3 후** | **~11,700** ✅ (목표 ≤12,000) |
| **P1b 후** | **~10,100** (fetch·context7 제거) |
| **exa/search 제거 후** | **~8,300** (shrimp 제외) |
| **P7 후** | **~8,300** (shrimp 포함 총계) |

### P3 — devforge/yggdrasil 트림 (opencode) ✅
- 파일: `~/.config/opencode/opencode.json` (동일 `tools` 블록)
- `devforge-mcp_*: false` + **12종 keep**: `deepdive_step_enter/exit/session_heartbeat/session_status/verify_sandbox`, `obs_write/obs_search`, `search_turns`, `search_similarity`, `mem_save`, `mem_search`, `get_conversation` → 25→12, **절감 2,068** (kept=2,611).
- `yggdrasil_*: false` + **4종 keep**: `deep_planning`, `sequential_thinking`, `list_plans`, `get_plan` → 절감 385 (kept=2,533).
- 검증(헤드리스): 노출 12/4종 일치, **누출 0**.
- 비고: yggdrasil 절감은 작음(핵심 `deep_planning`/`sequential_thinking`이 비쌈). devforge는 큰 절감.
- 제거됨(재-enable 가능): `fact_*`, `action_*`, `telegram_send`, `ingest`, `get_turn_facts`, `search_conversations`, `obs_remediate`, `review_sequential`, `flaresolverr_bypass`, `promote_plan`, `archive_plans`.

### P5a — 리서치 서버측 코어 + CLI ✅
- 신규: `scripts/lib/research/{__init__,_keys,web,exa,context7,fetch}.py`
  - `web`(Brave/Tavily/youcom 로테이션), `exa`, `context7`(공용 `_keys`), `fetch`(URL 본문), `__init__.research()/docs()/fetch_page()`
- 래퍼화(무중단): `proxies/search.py`·`exa_mcp.py`·`context7_mcp.py`는 **MCP 전송만**, 코어는 `lib.research` 위임.
- CLI: `cli.py research search|docs|fetch` (`--candidate-k`/`--top-k` 분리, `--json`; **캐시·리랭크 제외**).
- 검증: 라이브 검색(MCP 도구 발견), `docs`(FastAPI→libraryId), `fetch`(example.com), MCP 핸드셰이크 3종, `cli.py lint` 통과.
- 캐시+리랭크는 **P5b(조건부)** — P4 결과상 리서치 ~1.1회/세션이라 보류 권장.

### P6 — 규칙 전환 ✅
- `llm-agent-rule.md` 갱신 → `sync_gemini_rules.py`로 `AGENTS.md` 재생성.
  - MCP 표: `filesystem`/`github`/`time`/`fetch`/`context7` 항목 제거, **리서치=`cli.py research`** 명시.
  - Deep Dive **4단계 = `cli.py research`(docs 또는 web/exa)**, 2단계 = 내장 도구.
  - lsp 예시 `restart`→`start_lsp`(노출 목록 반영).
  - 원칙: "context7/exa 중복 금지" → "research는 CLI 일원화".

### P1b — fetch·context7 제거 (opencode) ✅
- 파일: `~/.config/opencode/opencode.json` → `fetch`·`context7` `enabled:false`
- 절감: **1,586** (fetch 788 + context7 798)
- 대체: `cli.py research fetch <url>`, `cli.py research docs "<lib>" "<질문>"` (+ 내장 webfetch)
- 검증(헤드리스): `context7`/`fetch` 노출 **NONE**.

### P5a-제거 — exa-search·search-proxy 제거 (opencode) ✅
- 파일: `~/.config/opencode/opencode.json` → `exa-search`·`search-proxy` `enabled:false`
- 절감: **1,804** (exa 1,127 + search-proxy 677)
- 대체: 내장 `websearch` + `cli.py research search --mode web|exa`
- 규칙: "전환기 예외" 제거 → 리서치 CLI/내장 일원화(AGENTS.md 재생성)
- 검증(헤드리스): `exa`/`search-proxy` 노출 **NONE**, `cli.py research search` 정상.
- 비고: `proxies/search.py`·`exa_mcp.py` 파일은 **Claude Code용으로 유지**(옵션1에서 정리).

### P7 — shrimp 제거, 소프트 체크포인트로 대체 ✅
- 근거: `docs/reports/p7-gating-design-research.md` (Plan-and-Act·Structured Prompting·Orkes 검증 → 하드락 아닌 **소프트 체크포인트**).
- 제거: `shrimp-task-manager` (`opencode.json` `enabled:false`), 절감 **~2,500**.
- 대체(기존 자산): 태스크=`cli.py task`(tasks DB), 체크포인트=`deepdive_step_*`, 검증=`tasks`/`obs_write`. **스키마 변경 없음**.
- 규칙: `llm-agent-rule.md`의 "Shrimp+LSP"→"**Task+LSP**", "반드시 shrimp 태스크"→"`cli.py task` 추적(강제락 아님)". AGENTS.md 재생성.
- 검증(헤드리스): `shrimp` 노출 **NONE**, 규칙 내 잔여 Shrimp 참조 0.

### 현재 활성 컴포넌트
`lsp` 3,129 · `devforge-mcp` 2,611 · `yggdrasil` 2,533 · `opencode-db`(소) ≈ **8.3k (총계)**.
남은 여지: 없음(추가 제거 시 기능 손실). 추가 절감은 lsp/devforge 필터 미세조정뿐.

### P8 — 최종 검증 ✅ (2026-09-11)
- **설정**: `opencode.json` 유효. enabled = `devforge-mcp`·`yggdrasil`·`lsp`·`opencode-db` (4종). `tools` 필터 32키.
- **스키마**: 최종 활성 **8,416 tok/turn** (lsp 3,129 · devforge-mcp 2,611 · yggdrasil 2,533 · opencode-db 143) = 기준선 36,672 대비 **−77.1%**.
- **노출(헤드리스)**: 활성 4종만 확인, 제거 7종(`shrimp`·`context7`·`exa-search`·`search-proxy`·`filesystem`·`github`·`time`/`fetch`) 부재.
- **규칙**: AGENTS.md — 4단계=`cli.py research`, `Task+LSP`, shrimp 참조는 removed-note만.
- **기능**: `cli.py research search|fetch` 정상, `cli.py task list` 정상, lint(신규·변경 파일) 위반 0.
  - lint: `cli.py` 전체 포함 **위반 0** (`ts` 약어 정리 완료).

### P8.1 — 버그 수정 + 양방향 검증 (2026-09-11)
**발견·수정한 버그**
1. `cli.py::cmd_task_add` — priority 미지정 시 `''` INSERT → `tasks.priority` CHECK(P0/P1/P2) **위반으로 조용히 실패**. → 미지정 시 `NULL` 삽입으로 수정. (P7 태스크 대체 경로의 필수 버그)
2. `cmd_research_fetch` / `cmd_research_docs` — `ValueError`(잘못된 URL 등) 미처리로 traceback. → 포착 후 깔끔한 에러 메시지.
3. (기존) `cli.py` `ts` 약어 → `utc_timestamp` (lint).

**양방향 검증 (모두 통과)**
- **정방향(설정→동작)**: MCP stdio 3종 실호출(web_search·exa_search·context7) 정상 · CLI 4종(`research search` web/exa, `docs`, `fetch`) 정상 · devforge-mcp 필터 툴(`search_similarity`·`obs_search`·`deepdive_session_status`) 정상 · `cli.py task add/update/list/delete` 정상.
- **역방향(동작→설정/규칙)**: `opencode.json` enabled 4종(`devforge-mcp`·`yggdrasil`·`lsp`·`opencode-db`) = 헤드리스 노출 4종 **일치** · 규칙 removed-list = 실제 제거 MCP **일치**, 잔여 참조 0.
- **회귀**: 옛 MCP 모듈 importers **0**, `py_compile` OK, lint 위반 **0**.
- 참고(무관): `search_similarity`가 `embed API unavailable` 반환 — 임베더(:8081) 환경 상태로 본 리팩터와 무관.

### 결론
MCP 통합 목표 달성: **36,672 → 8,416 tok/turn (−77%)**, 활성 MCP 4종. 리서치·태스크는 서버측(CLI/DB)으로 일원화, 규칙 동기화 완료.

## 2. 보류 / 후속 항목

### 보류-A (사용자 결정) — **Claude Code 정리 (옵션 1)**
- **결정: 보류.** opencode를 실제 세션에서 사용해 안정성 확인 후 착수.
- 대상: `~/.claude/mcp.json` (Claude Code 전용, **opencode와 별개**).
- 차이: 항목별 `enabled` 필드가 **없음** → 비활성화는 **항목 제거** 방식(`disabledMcpjsonServers`, `--strict-mcp-config` 활용) → 되돌리기 = 백업 복원.
- Claude 실사용(13세션): lsp 3, context7 0, fetch 0, github 0, filesystem 3, time 1 — **opencode와 패턴이 다름** → Claude 기준으로 재평가.
- lsp 필터: 네이티브 per-tool 필터 불확실(`--allowedTools`/`--disallowedTools`, 이슈 #12863·#7328). 대안 = `mcp-trunc-proxy`에 `--allow` 추가(**자기 툴 `proxy_artifact_*` 자동 포함 필수** — 누락 시 잘린 응답 회수 불가).
- 절차: ① 필터 실험 → ② 플래그/`--allow` → ③ 백업 후 항목 제거 → ④ 검증.
- 백업(존재): `~/.claude/mcp.json.bak_20260911_144514`.

### 보류-B (조건부) — **P5b: research_cache + 리랭크**
- **결정: 보류.** P4 실측 리서치 **~1.1회/세션** → 캐시 히트 거의 없음.
- 내용: `research_cache`(Postgres) + cross-encoder 리랭크(:8080) + `candidate_k`/`top_k` + 골든셋.
- 착수 조건: 리서치 호출이 **세션당 ≥3회**로 증가할 때. (리랭크는 후보군이 있어야 유효.)
- 선행: 리랭커 도메인(검색 관련성) 실측.

### 후속(선택)
- **lsp allowlist 추가 축소**: `find_symbol`/`rename_symbol` 등 opencode 사용 0종 제거 여부(규칙 동반 갱신).
- **devforge-mcp 추가 트림**: 실사용 낮은 툴 추가 제거 여부.

### 완료(참고)
- ✅ P0·P1a·P1b·P2·P3·P4·P5a·P6·P7·P8 · exa/search 제거 · lint `ts` 정리(이전 옵션3).

## 3. 롤백
```bash
# opencode 전체 원복
cp ~/.config/opencode/opencode.json.bak_20260911_144514 ~/.config/opencode/opencode.json
# 또는 개별: mcp.<name>.enabled=true, tools 블록의 "lsp_*": false 라인 제거
```
- P1a/P2 모두 설정 파일 한 곳 → 즉시 원복.
- Claude Code 미적용이므로 롤백 불필요.

## 4. 참고
- 측정 스크립트: `probe_mcp.py`(tools/list 핸드셰이크).
- 검증 명령: `opencode debug config`(파싱), `opencode run "..."`(실제 노출 툴).
- P4 진단: `deepdive_steps` step=4 = 8세션(7 DONE); step-4 창에서 context7 8·exa 1콜, 대체도구 이탈 없음.
