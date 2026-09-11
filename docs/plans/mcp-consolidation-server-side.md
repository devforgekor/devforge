# 개선 계획서 v3 (실측 기반, 상세) — MCP 스키마 예산 & 리서치 서버측화

> 작성: 2026-09-11 (v2 대체) · 상태: **proposed**
> 데이터: `docs/reports/mcp-cost-baseline.md` + 본 문서 §1 실측 · 분석: `docs/reports/deepdive-mcp-analysis.md`
> 웹 검증: Anthropic prompt caching(툴 스키마 캐시됨·cache read 0.1x·툴 정의 변경 시 전체 무효화), opencode `tools` glob 네이티브 필터, Qwen3-Reranker=질의-문서 관련성 모델
> **v2→v3 핵심 변화**: ①비용 프레이밍 정정(캐시) ②**opencode 네이티브 툴 필터 발견**(프록시 불필요) ③무위험 제거 **선행** ④lsp allowlist **per-tool 실측**(11툴=2,936tok) ⑤4단계는 **실행됨**(툴 이탈 문제) ⑥3b 조건부 ⑦목표 **≤12k** ⑧리서치 **CLI-only 확정** ⑨`candidate_k`/`top_k` 분리.

---

## 0. 요약
- 활성 MCP 스키마 **36,672 tok/turn**(+shrimp ~2.5k ≈ 39.2k). `lsp` 단일 **16,333(44.7%)**.
- **무위험 제거**(github·filesystem·fetch·context7·time) = **10,354 tok / 0.3d / 신규코드 0**.
- **lsp 필터** core 11툴 = **2,936** → 절감 **13,397 tok / 0d(opencode 네이티브)**.
- **목표 ≤12k**(여유), 달성 시 실측 예상 **~6k**.
- 리서치 고도화(캐시·리랭크)는 **3b 조건부**: 4단계 호출 회복 시에만.

## 1. 실측 데이터

### 1.1 서버별 스키마 비용 (per-turn)
| MCP | 툴 | tok_est | 활성 |
|---|---:|---:|:--:|
| **lsp** | 68 | **16,333** | Y |
| github | 29 | 4,740 | Y |
| devforge-mcp | 25 | 4,700 | Y |
| filesystem | 17 | 3,712 | Y |
| yggdrasil | 9 | 3,421 | Y |
| exa-search | 5 | 1,127 | Y |
| context7 | 5 | 798 | Y |
| fetch | 4 | 788 | Y |
| search-proxy | 5 | 677 | Y |
| time | 2 | 316 | Y |
| shrimp | 15 | ~2,500* | Y |
| **합계** | | **~39,172** | |

### 1.2 lsp 툴별 비용 (per-tool 실측)
| 순위 | 툴 | tok |
|---|---|---:|
| 1 | `start_lsp` | 500 |
| 2 | `preview_edit` | 466 |
| 3 | `rename_symbol` | 383 |
| 4 | `get_inlay_hints` | 366 |
| 5 | `get_cross_repo_references` | 347 |
| … | (장기 꼬리) | … |
| | **core 11툴 합** | **2,936** |

→ **유지 툴이 오히려 비쌈**(start/preview/rename 상위). 균일추정(11,813)보다 **실제 절감 13,397**.

### 1.3 Deep Dive 실행률 (`deepdive_steps`, 17세션)
| step | 세션 | DONE | ABORTED |
|---|---:|---:|---:|
| 1 | 12 | 5 | 7 |
| 2 | 12 | 9 | 3 |
| 3 | 9 | 7 | 2 |
| **4** | **8** | **7** | **1** |
| 5 | 7 | 6 | 1 |
| 6 | 6 | 5 | 1 |
| 7 | 6 | 6 | 0 |

→ **4단계는 죽지 않음**: 8세션 진입(94% 완료). **문제는 "규정 도구(context7/exa)" 대신 WebSearch(8)·WebFetch(7)·search-proxy(11)로 이탈**.

### 1.4 사용 빈도 (13 claude 세션)
devforge 59 · yggdrasil 32 · search-proxy 11 · lsp 3 · filesystem 3 · shrimp 3 · exa 2 · time 1 · github 0 · context7 0 · fetch 0. 내장 Bash 1,118.

## 2. 정정 (v2 오류)
| 항목 | v2 | v3 |
|---|---|---|
| 활성 합계 | 37,200 | **36,672**(shrimp 별도) |
| lsp 비중 | 44%/41% 혼용 | **44.7%**(shrimp 제외) / 41.8%(포함) — 분모 명시 |
| 비용 프레이밍 | "매 턴 39.7k 과금" | **캐시 prefix라 read 0.1x**. 실제 피해 = **컨텍스트 점유 + 툴선택 희석 + 초기화 지연** |
| 4단계 | (v2 침묵) | **실행됨(8/17)** — 툴 이탈이 문제 |

## 3. 비용의 정확한 의미 (검증 반영)
- Anthropic: 프롬프트 캐싱은 `tools → system → messages` 순 전체 prefix를 캐시. **툴 스키마도 캐시 대상**, cache read **0.1x**. → **과금 관점 2턴 이후 저렴**.
- 그러나 (a) **캐시 토큰도 컨텍스트 창을 점유**, (b) **툴 정의를 바꾸면 전체 캐시 무효화**, (c) 초기화 비용.
- 따라서 v3의 성공 지표는 "과금 절감"이 아니라 **"컨텍스트 점유율 축소 + 툴 선택 정확도 + 로딩 지연"**. (컨텍스트 창 미실측 → P0에서 확인)

## 4. 목표
- **1차 목표: 활성 MCP 스키마 ≤ 12,000 tok/turn** (달성 예상 ~6,000).
- 2차: 컨텍스트 점유율(모델 창 대비) 실측·명시.
- 비목표: lsp **능력** 제거, 외부 벤더 교체, Deep Dive 7단계 구조 변경.

## 5. 작업 항목 (상세)

### A. 제거 — **2단계로 분할** (P0가 정정)
P0(실측)에서 "무위험 5종"은 **대체 준비도**가 달라 분할한다.

**P1a — 대체수단 즉시 존재 (0.2d)** ⭐먼저
- `filesystem`(내장 Read/Edit/Glob/Grep) · `github`(`gh` CLI) · `time`(`date`).
- 절감 **8,768** tok. 위험 낮음(대체 즉시). → **2026-09-11 opencode 적용 완료**.

**P1b — 대체수단 준비 후 (P5a 뒤, 0.2d)**
- `fetch` · `context7` — opencode 실사용 각 **87·47회**(claude-only 데이터의 "0회"는 **표본 편향**이었음). 대체수단(`lib/research` + `webfetch`) 준비 전 제거 금지.
- 절감 1,586 tok. **P5a에 종속.**

### B. lsp 필터 (P2) — opencode 네이티브, 프록시 불필요
- **opencode**: `tools`에서 `"lsp_*": false` 후 core만 enable:
  ```jsonc
  "tools": { "lsp_*": false,
    "lsp_blast_radius": true, "lsp_find_references": true, "lsp_find_symbol": true,
    "lsp_inspect_symbol": true, "lsp_get_symbol_source": true, "lsp_get_diagnostics": true,
    "lsp_rename_symbol": true, "lsp_suggest_fixes": true,
    "lsp_proxy_artifact_get": true, "lsp_proxy_artifact_info": true, "lsp_proxy_artifact_list": true }
  ```
  (per-agent 또는 global. 접두어는 서버명 `_`.)
- **Claude Code**: 네이티브 per-tool 필터가 불확실(#7328 closed, #12863). `claude --allowedTools` 실험 1회 → 불가 시 **`mcp-trunc-proxy`에 `--allow` 플래그 추가**(신규 프록시 금지). **자기 툴 `proxy_artifact_*` 3종은 allowlist에 자동 포함**(누락 시 잘린 응답 회수 불가).
- Acceptance: lsp 노출 11툴, `blast_radius`/`find_references`/`rename_symbol`/`get_diagnostics` 정상, `proxy_artifact_get` 정상, 절감 ≥13k.

### C. devforge/yggdrasil 트림 (P3, 0.5d)
- 변경 후 **devforge-mcp가 최대 서버**(4,700). 실사용 92%가 4툴(`search_turns`25·`deepdive_step_enter`14·`deepdive_step_exit`9·`obs_search`6) → **25→8~10툴**.
- yggdrasil 9→4(`deep_planning`·`sequential_thinking`·`list_plans`·`get_plan`; 사용 94%가 2툴).
- 절감 ~5,100.

### D. 4단계 원인 진단 (P4, 0.3d) — 결정 분기
- **사실: 4단계는 8/17세션 실행됨.** 원인은 미실행이 아니라 **도구 이탈**.
- 진단: 4단계 진입 세션에서 실제 호출 도구 대조(로그). 예상: WebSearch/WebFetch/search-proxy 우세.
- 분기:
  - (a) 규칙-현실 불일치 → **`cli.py research` 하나로 규정**(가벼운 CLI가 준수율↑) → 3a 진행.
  - (b) 호출 절대량 부족 → 캐시는 무의미 → **3b 보류**.
- Acceptance: 세션별 4단계 도구 분포 표 산출.

### E. 리서치 흡수 (P5a 선행 / P5b 조건부)
- **P5a (0.5d)**: `proxies/search.py`·`exa_mcp.py`·`context7_mcp.py` 코어 → `lib/research/{web,exa,context7,fetch}.py`(기존 파일은 얇은 래퍼). **`cli.py research`**. 캐시/리랭크 **제외**. → `fetch`/`context7` 기능 회복 + 1,804 절감.
- **P5b (1d, 조건부)**: `research_cache` + 리랭크 + **`candidate_k`/`top_k`** + 골든셋. **착수 조건 = 4단계 호출 ≥3회/세션**. 근거: 리랭크는 후보군 있어야 유효(5→5=0).
- **CLI-only 확정**(MCP 툴 미추가): 내장/Bash 1,437회 vs MCP 114회. `--json` 엄격 계약.

### F. shrimp → tasks DB (P7, 1d)
- `shrimp_data/` → `tasks` DB(`cli.py task`). `deepdive_step_*` 게이팅 패턴 재사용. 절감 ~2,500.

## 6. lsp allowlist (확정, 11툴 / 2,936tok)
`blast_radius`, `find_references`, `find_symbol`, `inspect_symbol`, `get_symbol_source`, `get_diagnostics`, `rename_symbol`, `suggest_fixes` + **필수** `proxy_artifact_get/info/list`.
- 제외: `start_lsp`(500, **라이프사이클** — 클라이언트/프록시 자동화 대상), `preview_edit`/`apply_edit`/`replace_symbol_body`(내장 Edit 143회로 대체 가능), `run_tests`/`run_build`(Bash 1,118), `*_simulation`(7), `get_*`(tokens/hints/…), `format_*`, `*_cache`, `type_hierarchy`, `go_to_*` 변형.
- **확인 필요**: `blast_radius`가 `get_cross_repo_references`에 의존하는지(제거 시 조용한 축소 방지). 정리 단계의 `safe_delete_symbol` 사용 시 별도 취급.

## 7. 단계 (P0~P7)
| P | 내용 | 절감 | 공수 | Acceptance |
|---|---|---:|---|---|
| **P0** | 백업 + **툴별 char** + **opencode 로그** + shrimp 프로브 + 컨텍스트창 확인 | — | 0.5d | 기준선 재현 |
| **P1a** | 대체가능 3종 제거(filesystem·github·time) | 8,768 | 0.2d | ≤28k, 세션 정상 |
| **P1b** | fetch·context7 제거 (**P5a 종속**) | 1,586 | 0.2d | CLI 대체 후 |
| **P2** | opencode 네이티브 lsp 필터(+11툴) / Claude Code 플래그·trunc-proxy `--allow` | 13,397 | 0.2–0.5d | lsp 11툴, ≥13k |
| **P3** | devforge 25→8, yggdrasil 9→4 | ~5,100 | 0.5d | 툴 축소, 필수 동작 |
| **P4** | 4단계 진단 | — | 0.3d | 도구 분포표 |
| **P5a** | `lib/research/` + `cli.py research`(캐시X) | 1,804 | 0.5d | CLI JSON 계약 |
| **P6** | 규칙 전환(`llm-agent-rule.md`/`AGENTS.md`) | — | 0.5d | 4단계=CLI 명시 |
| **P5b** | 캐시+리랭크+`candidate_k/top_k`+골든셋 | — | 1d | **조건부** |
| **P7** | shrimp → tasks | 2,500 | 1d | SSOT 단일 |
| **P8** | 검증·정리·Deep Dive 1회 | — | 0.5d | §9 통과 |

## 8. 인터페이스
```python
# lib/research/__init__.py
def research(query, mode="auto", candidate_k=30, top_k=5,
             rerank=True, use_cache=True, ttl_sec=None) -> dict
# results:[{title,url,snippet,source,score}] + meta:{cache_hit,reranked,provider,candidate_k,top_k}
```
```
cli.py research search "질의" [--mode auto|web|exa|docs] [--candidate-k 30] [--top-k 5] [--no-rerank] [--no-cache] [--json]
cli.py research docs "lib" "질의" [--json]
cli.py research fetch <url> [--json]
```

## 9. 검증 기준 (개선 증거)
- 활성 MCP 스키마: **39.2k → ≤12k** (측정 스크립트 재실행).
- lsp 노출: 68 → 11.
- 리랭크 on/off **정렬 변화**(동작 아닌 **개선** 증거) + 골든셋 nDCG/precision.
- 캐시: 반복쿼리 히트 > 0 (**단, P5b 조건부**).
- 4단계 도구 준수율: 규정(CLI/context7/exa) 비율 상승.
- 롤백: 설정 원복·`RESEARCH_BACKEND`.

## 10. 리스크
- **reranker 도메인**: Qwen3-Reranker는 질의-문서 관련성 모델 → 리스크 **낮음**(검증). 실제 제약은 **후보군 부족**과 4B로 30~50건 채점 지연.
- **allowlist 누락**: `proxy_artifact_*`·`start_lsp` 등 → 워크플로우 필요 툴 보수적 포함.
- **캐시 무효화**: 툴 정의 변경은 전체 캐시 무효화 → 필터는 **세션 시작 시 1회**.
- **표본 편향**: lsp 사용 logs가 Claude 위주 → **opencode 로그 선행**(P0).
- **KeyRotator 동시접근**: MCP 래퍼+CLI → 전환기 단일 경로.

## 11. 미결
- yggdrasil 유지(권장) vs `cli.py plan` 대체.
- Claude Code per-tool 필터 네이티브 여부(실험 1회).
- 컨텍스트 창(모델별) → 점유율 확정.

## 부록 A. 진단 쿼리
```sql
-- Deep Dive 단계 실행률
SELECT step, count(distinct session_id) AS sessions,
       sum((status='DONE')::int) AS done, sum((status='ABORTED')::int) AS aborted
FROM deepdive_steps GROUP BY step ORDER BY step;
-- 4단계 진입 세션 목록 (로그 대조용)
SELECT session_id, started_at, status FROM deepdive_steps WHERE step=4 ORDER BY started_at DESC;
```
## 부록 B. 측정 스크립트
- MCP 스키마 프로브: `probe_mcp.py`(tools/list 핸드셰이크) — 서버별/툴별 tok_est 산출. P0·P1·P2 재측정에 사용.

---

## 12. P0 실행 로그 (2026-09-11)

### 12.1 opencode 실사용 (authoritative, 130세션·23,591 툴콜)
| MCP | opencode 호출 | claude 호출 | 비고 |
|---|---:|---:|---|
| search-proxy | 207 | 11 | 고사용 |
| devforge-mcp | 183 | 59 | 고사용 |
| yggdrasil | 123 | 32 | 고사용 |
| time | 110 | 1 | **claude 편향** |
| filesystem | 109 | 3 | **claude 편향** |
| fetch | 87 | 0 | **claude 편향** |
| shrimp | 77 | 3 | |
| github | 67 | 0 | **claude 편향** |
| exa-search | 58 | 2 | |
| lsp | 48 | 3 | |
| context7 | 47 | 0 | **claude 편향** |

→ **claude-only 집계는 opencode의 결정을 대표하지 못함.** 모든 제거 판단은 opencode 기준으로 재평가.

### 12.2 lsp 실사용 (opencode, 48콜)
`blast_radius` 19 · `get_diagnostics` 13 · **`start_lsp` 11** · `open_document` 2 · `detect_lsp_servers` 2 · `find_references` 1.
→ allowlist는 **blast_radius·get_diagnostics·start_lsp·find_references**를 반드시 포함. `find_symbol`/`inspect_symbol`/`rename_symbol`/`suggest_fixes`는 opencode 사용 **0** → 축소 검토.

### 12.3 컨텍스트 실측 (opencode 21,470 메시지)
avg 신규 input **5,820** · avg **cache_read 155,417** tok/메시지 · 합계 cache_read 33.4억.
→ 툴 스키마(~36k)는 컨텍스트의 **약 20%+** 점유. **과금이 아니라 컨텍스트 예산**이 핵심 근거(§3 확증).

### 12.4 적용 완료
- 백업: `~/.claude/mcp.json.bak_20260911_144514`, `~/.config/opencode/opencode.json.bak_20260911_144514`.
- **opencode P1a 적용**: `filesystem`·`github`·`time` `enabled:false` (절감 8,768). JSON 유효.
- 대체: filesystem→내장, github→`gh`, time→`date`(즉시 가용).
- **미적용(다음 단계)**: Claude Code `~/.claude/mcp.json`(파일별 enabled 없음 → 항목 제거 방식), lsp 필터, fetch/context7 제거(P5a 종속).

