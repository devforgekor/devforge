# MCP 비용 베이스라인 (실측)

> 작성: 2026-09-11 · 목적: MCP 통합/축소 계획의 **데이터 근거** 확보
> 방법: MCP `tools/list` JSON-RPC 핸드셰이크로 툴 스키마 수집 → 직렬화 문자수 → `tok_est ≈ chars/4`(추정)
> 사용빈도: `~/.claude/projects/-home-opc/*.jsonl` 13개 세션의 `tool_use` 집계
> 한계: `tok_est`는 추정치(정확 토크나이저 아님), 사용빈도 표본은 Claude Code 세션(현 opencode 세션은 lsp를 더 사용). 상대 비교·우선순위 판단용.

---

## 1. 서버별 스키마 비용 (per-turn, 매 요청 재전송)
| MCP | 툴 수 | chars | tok_est | 활성 | 비고 |
|---|---:|---:|---:|:--:|---|
| **lsp** | 68 | 65,572 | **16,393** | Y | 측정 최대 |
| github | 29 | 18,963 | 4,740 | Y | |
| devforge-mcp | 25 | 18,800 | 4,700 | Y | HTTP |
| filesystem | 17 | 14,851 | 3,712 | Y | |
| yggdrasil | 9 | 13,685 | 3,421 | Y | +proxy 3툴 |
| exa-search | 5 | 4,508 | 1,127 | Y | |
| context7 | 5 | 3,193 | 798 | Y | |
| fetch | 4 | 3,154 | 788 | Y | |
| search-proxy | 5 | 2,710 | 677 | Y | |
| time | 2 | 1,267 | 316 | Y | |
| shrimp-task-manager | 15 | (미측정) | ~2,500 | Y | 추정 |
| token-savior | 15 | 5,593 | 1,398 | N | disabled |

**활성 합계 ≈ 37,200 tok/turn(+shrimp 추정 ~2.5k ≈ 39.7k).**
- `mcp-trunc-proxy`가 래핑 서버마다 **+3툴**(`proxy_artifact_get/info/list`)을 추가함(lsp·yggdrasil에서 확인).

## 2. 사용 빈도 & 비용 효율 (13개 세션, tool_use 집계)
| MCP | tok_est | 호출 수 | 호출당 비용(tok) |
|---|---:|---:|---:|
| lsp | 16,393 | 3 | **5,464** |
| github | 4,740 | 0 | ∞ |
| filesystem | 3,712 | 3 | 1,237 |
| yggdrasil | 3,421 | 32 | 107 |
| exa-search | 1,127 | 2 | 564 |
| context7 | 798 | 0 | ∞ |
| fetch | 788 | 0 | ∞ |
| search-proxy | 677 | 11 | 62 |
| time | 316 | 1 | 316 |
| shrimp | ~2,500 | 3 | ~833 |
| devforge-mcp | 4,700 | 59 | 80 |

**MCP 개별 툴 상위**: `search_turns`(25), `deep_planning`(20), `deepdive_step_enter`(14), `web_search`(11), `deepdive_step_exit`(9), `sequential_thinking`(10), `obs_search`(6).
**내장 도구가 압도**: Bash 1118, Read 176, Edit 143.

## 3. 관찰
1. **lsp가 측정 스키마의 ~44%**(16.4k)인데 **13세션 3회** 사용 → 비용/효율 최악. **최대 레버.**
2. **github(0), context7(0), fetch(0), time(1)** = 사실상 미사용인데 합계 ~6.6k.
3. **filesystem(3회)** = 전부 `list_allowed_directories`; 내장 `Read/Bash`가 대체.
4. **search(11) + exa(2)** = 실사용되나 스키마 합계 ~1.8k → 흡수 가치.
5. **yggdrasil(32) + devforge-mcp(59)** = 실제 사용 → 유지, 툴 트림만.
6. **shrimp(3회)** = `tasks` DB와 중복 → 이관.

## 4. 종합
- 현재 **활성 MCP 스키마 ≈ 39.7k tok/turn**(lsp가 41%).
- 목표(현실): **lsp 필터 + 미사용 제거 + 리서치 흡수 → ≈ 10k**(약 **-75%**), 능력 유지.
- 최우선: **lsp 필터**(단일 항목 최대 절감), 다음: **미사용 5종 제거**, 다음: **리서치 흡수(+캐시/리랭크/인용)**.
