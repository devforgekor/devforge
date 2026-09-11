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
| **P2 후** | **~14,700** |
| 목표 | ≤12,000 (P3 필요) |

## 2. 미적용 — 다음 단계

### 옵션 1 (사용자 요청: 마지막 패치) — **Claude Code 정리**
- 대상: `~/.claude/mcp.json` (Claude Code 전용). **opencode와 별개.**
- 차이: 이 파일에는 항목별 `enabled` 필드가 **없음** → 비활성화는 **항목 제거**(또는 `~/.claude.json`/`settings.json`의 `disabledMcpjsonServers`, `--strict-mcp-config` 활용) 방식 → 되돌리기 = 백업 복원.
- Claude 실사용(13세션): lsp 3, context7 0, fetch 0, github 0, filesystem 3, time 1 — **opencode와 패턴이 다름** → opencode 결정을 그대로 적용하지 말고 **Claude 기준으로 재평가**.
- lsp 필터: Claude는 네이티브 per-tool 필터가 불확실(`--allowedTools`/`--disallowedTools` 존재하나 MCP 적용은 미확인, 이슈 #12863·#7328). 대안 = `mcp-trunc-proxy`에 `--allow` 추가(**자기 툴 `proxy_artifact_*` 자동 포함 필수** — 누락 시 잘린 응답 회수 불가).
- 절차: ① `claude` 버전에서 MCP 툴 필터 실험(1회) → ② 가능하면 플래그, 불가하면 `--allow` → ③ 백업 후 항목 제거 → ④ 검증.

### 그 외
- **P1b**: `fetch`·`context7` 제거 — **P5a(`lib/research`+CLI) 완료 후**. (opencode 실사용 87·47회.)
- **P3**: devforge-mcp 25→8~10, yggdrasil 9→4 (변경 후 devforge가 최대 서버). — 목표 ≤12k 달성용.
- **P5a**: `lib/research/` 코어 이관 + `cli.py research`(캐시 제외).
- **P5b(조건부)**: `research_cache`+리랭크+`candidate_k/top_k`. **P4 결과: 리서치 ~1.1회/세션 → 캐시 가치 낮음 → 보류 권장.**
- **P6**: 규칙 전환(`llm-agent-rule.md`/`AGENTS.md` — 4단계=CLI, lsp 노출 목록 반영).
- **P7**: shrimp → `tasks` DB.
- **P3 후속**: opencode `lsp_*` allowlist 추가 축소(`find_symbol`/`rename_symbol` 등 사용 0종) 여부 — 규칙 갱신과 함께.

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
