# 런북 — Claude Code MCP 정리

> Status: proposed · Date: 2026-09-11 · Owner: devforge · Related: `docs/reports/mcp-consolidation-applied-20260911.md`, `/home/opc/llm-agent-rule.md`
> **보류-A**: opencode 실사용 안정 확인 후 착수. (지금은 실행하지 않음)

---

## 0. 트리거 (언제)
- opencode를 실제 세션에서 며칠 사용해 **문제 없음**을 확인한 뒤.
- lazy: 급하지 않음. opencode가 정착하면 착수.

## 1. 현황
- 대상: `~/.claude/mcp.json` (**Claude Code 전용**, opencode와 별개).
- 서버 12종: filesystem·search-proxy·exa-search·devforge-mcp·yggdrasil·fetch·github·context7·time·token-savior·shrimp-task-manager·lsp.
- **차이**: opencode와 달리 이 파일에는 항목별 `enabled` 필드가 **없다** → 비활성화는 **항목 제거**(또는 `disabledMcpjsonServers`, `--strict-mcp-config`).
- **주의**: Claude 실사용 패턴(13세션)이 opencode와 **다르다**(lsp 3, context7 0, fetch 0, github 0, filesystem 3, time 1) → opencode 결정을 그대로 적용하지 말고 **Claude 기준 재평가**.

## 2. 선행조건
1. opencode 안정 확인(트리거).
2. 백업: `cp ~/.claude/mcp.json ~/.claude/mcp.json.bak_$(date +%Y%m%d_%H%M%S)` (기존 백업 `mcp.json.bak_20260911_144514` 존재).
3. `claude --version` 및 툴 필터 옵션 재확인.

## 3. 절차
1. **백업**(§2.2).
2. **필터 실험**: 현재 `claude`가 MCP 툴 필터를 지원하는지 1회 확인.
   - `claude --help`에서 `--allowedTools`/`--disallowedTools` 및 MCP 적용 여부(이슈 #12863·#7328 확인).
   - 가능하면 플래그로 lsp 서브셋 제한.
3. **불가 시 프록시**: `mcp-trunc-proxy`에 `--allow` 추가(형제 프록시 신설 금지).
   - **필수**: 자기 툴 `proxy_artifact_get/info/list`를 allowlist에 **자동 포함**(누락 시 잘린 응답 회수 불가).
4. **항목 제거/비활성**: 불필요 서버(filesystem·github·time·fetch·context7·shrimp 등)를 항목 제거 또는 `disabledMcpjsonServers`.
5. **검증**(§4).

## 4. 검증
- 노출 툴 목록이 기대와 일치(제거 서버 부재).
- 리서치(MCP 또는 `cli.py research`), lsp 동작, 태스크(`cli.py task`) 정상.
- 토큰 재측정(`probe_mcp.py`)로 감소 확인.

## 5. 롤백
```bash
cp ~/.claude/mcp.json.bak_<TS> ~/.claude/mcp.json
```
- 항목 제거 방식이므로 **백업 복원**이 유일한 되돌리기. 실행 전 백업 필수.

## 6. 주의
- `mcp-trunc-proxy`를 쓸 경우 `proxy_artifact_*` 포함(Claude는 opencode와 달리 trunc 래핑됨).
- Claude는 `enabled` 토글이 없어 **삭제=복원 필요** → 신중히.
- 본 작업은 **opencode 완료 후**로 미룸(사용자 결정).
