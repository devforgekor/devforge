# MCP 툴 사용 감사 (30일) — 불필요/표준이탈 분석

> Status: record · Date: 2026-09-14 · Owner: devforge · Related: `docs/reports/industry-standard-comparison-20260914.md`, `docs/adr/0006-mcp-tool-surface.md`, `docs/plans/final-plan.md`
> 목적: opencode에 실제 로드되는 MCP 툴을 실사용 데이터와 업계 표준에 대조해 **keep / merge / remove**를 확정한다.

---

## 1. 방법
- **로드 툴 집합**: `~/.config/opencode/opencode.json`의 `tools` allowlist 기준(활성 MCP 4종의 실제 로드 툴 = 33).
- **사용 데이터**: `~/.local/share/opencode/opencode.db` `part` 테이블(JSON `type=tool`, `tool` 이름) 최근 **30일**.
- **표준 기준**: 서버당 5~15(최대 10~20), 근접 중복 merge, 30일 0회 제거, 라이프사이클/plumbing 노출 최소화, `readOnly/destructive` annotation.

## 2. 결과 (로드 33툴 × 30일 호출)

| 서버 | 툴 | 30d | 판정 |
|---|---|---:|---|
| devforge-mcp | `deep_planning`(yggdrasil) | 112 | (yggdrasil 소속) Keep |
| devforge-mcp | `obs_write` | 67 | Keep |
| devforge-mcp | `deepdive_step_enter` | 60 | **Merge** |
| devforge-mcp | `deepdive_step_exit` | 58 | **Merge** |
| devforge-mcp | `get_conversation` | 15 | Keep |
| devforge-mcp | `mem_save` | 13 | **Merge(memory)** |
| devforge-mcp | `mem_search` | 11 | **Merge(memory)** |
| devforge-mcp | `deepdive_session_heartbeat` | 11 | **Auto/Merge** |
| devforge-mcp | `search_turns` | 10 | **Merge(search)** |
| devforge-mcp | `obs_search` | 8 | **Merge(memory)** |
| devforge-mcp | `search_similarity` | 8 | **Merge(search)** |
| devforge-mcp | `deepdive_session_status` | 1 | **Merge** |
| devforge-mcp | `deepdive_verify_sandbox` | 1 | Keep (+`destructiveHint`) |
| lsp | `get_diagnostics` | 25 | Keep |
| lsp | `blast_radius` | 23 | Keep |
| lsp | `start_lsp` | 13 | **Auto/Remove** |
| lsp | `find_references` | 4 | Keep |
| lsp | `detect_lsp_servers` | 2 | **Remove** |
| lsp | `find_symbol` | **0** | **Remove**(후보) |
| lsp | `inspect_symbol` | **0** | **Remove** |
| lsp | `get_symbol_source` | **0** | **Remove** |
| lsp | `suggest_fixes` | **0** | **Remove**(후보) |
| lsp | `rename_symbol` | **0** | **Remove**(후보) |
| lsp | `proxy_artifact_get` | **0** | **Remove** |
| lsp | `proxy_artifact_info` | **0** | **Remove** |
| lsp | `proxy_artifact_list` | **0** | **Remove** |
| yggdrasil | `deep_planning` | 112 | Keep |
| yggdrasil | `sequential_thinking` | 14 | **Merge(plan)** |
| yggdrasil | `get_plan` | 1 | Keep |
| yggdrasil | `list_plans` | **0** | **Remove** |
| opencode-db | `list_tables` | 10 | **Merge(schema)** |
| opencode-db | `query` | 5 | Keep (`readOnlyHint`) |
| opencode-db | `schema` | 3 | **Merge(schema)** |

- **총 로드 툴 호출(30d): 475**. **30일 0회: 9개**.
- 참고: opencode **빌트인 툴**(bash 10,697 / read 3,202 / edit 2,089 / grep 860 / glob 494 / write 482 / webfetch 339 / todowrite 332 / websearch 119 / question 110 / task 13 / skill 5)이 MCP 툴보다 압도적으로 많이 쓰임 → **LSP read 계열이 빌트인 `grep/read`로 대체**되고 있음을 시사(`find_symbol`/`get_symbol_source` 0회).

## 3. 판정 요약

### Remove (9 + α)
| 툴 | 사유 |
|---|---|
| lsp `proxy_artifact_get/info/list` | 0회. 니치 아티팩트, "just-in-case" 툴 |
| lsp `find_symbol`·`inspect_symbol`·`get_symbol_source` | 0회. 빌트인 `grep/read`로 대체 |
| lsp `detect_lsp_servers` | 2회. 서버 진단(내부용) |
| yggdrasil `list_plans` | 0회 |
| (후보) lsp `suggest_fixes`·`rename_symbol` | 0회. 단 쓰기/유용성 재검토 |
| (후보) lsp `start_lsp` | 13회지만 라이프사이클 → 자동화/제거 검토 |

### Merge (근접 중복/plumbing)
| 대상 | 결과 | 근거 |
|---|---|---|
| devforge `deepdive_step_enter/exit/session_heartbeat/session_status` | **1툴 `deepdive(action=…)`** (heartbeat/status는 서버 내부화) | lifecycle plumbing, 호출 4툴→1 |
| devforge `mem_save/mem_search` + `obs_write/obs_search` | **`memory(action=save|search, kind=…)` 2툴** | 의미 중복(선택 혼동) |
| devforge `search_turns` + `search_similarity` | **`search_turns(mode=fts|hybrid)`** | 같은 핸들러+분기 |
| opencode-db `list_tables` + `schema` | **1툴** | 스키마 서술 중복 |
| yggdrasil `deep_planning` + `sequential_thinking` | **1툴(통합) 검토** | 둘 다 계획 계열 |

### Keep
`obs_write`, `get_conversation`, `deepdive_verify_sandbox`(+ann), lsp `get_diagnostics`·`blast_radius`·`find_references`, yggdrasil `deep_planning`·`get_plan`, opencode-db `query`(+ann).

## 4. 목표치
**33 → 약 16** (Remove 9~11 + Merge −6). 표준 safe/caution 구간(<20) 진입.

기대효과(업계 실측): GitHub Copilot은 툴 **40→13**으로 줄여 지연 −400ms·TTFT −190ms·정확도 +2~5pp. 근접중복 제거는 오선택을 줄이고(RAG-MCP 13.6%→43.1%), 컨텍스트도 절감.

## 5. 주의 / 한계
- **단일 프로젝트/기간 편향**: 30일·이 프로젝트 기준. `find_symbol` 등이 0회여도 **타 프로젝트/미래엔 유용**할 수 있음 → 제거 시 `blast_radius`·`find_references`로 대체 가능한지 확인.
- **측정 후 컷 원칙**: 0회라도 "핵심 capability"는 유지 여부를 사용자 판단으로.
- **annotation 부재**: `readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint` 미설정 → 읽기/쓰기 구분 안 됨(예: `obs_write`, `rename_symbol`, `deepdive_verify_sandbox`).
- **보안**: allowlist + description hash pinning(툴 포이즈닝/rug-pull) 병행.

## 6. 적용 순서 (권고)
1. **즉시(저위험)**: 0회 Remove — lsp `proxy_artifact_*`(3), `list_plans`, `detect_lsp_servers`. (`opencode.json tools` allowlist에서 `false`)
2. **Merge 설계**: `deepdive(action)`, `memory(action,kind)`, `search_turns(mode)`, `list_tables`+`schema` — 서버측 구현 필요.
3. **annotation 추가**: read/destructive/idempotent.
4. 재측정(2~4주) 후 `find_symbol`/`suggest_fixes`/`rename_symbol`/`start_lsp` 최종 판정.

## 7. 출처
- 업계: `docs/reports/industry-standard-comparison-20260914.md`(AWS MCP tool design, Gingerlabs 5–15, Albato/Speakeasy 10/20/107, GitHub Copilot 40→13, RAG-MCP, NSA/OWASP tool poisoning)
- 실측: `~/.local/share/opencode/opencode.db` `part`(30d), `~/.config/opencode/opencode.json` `tools` allowlist
