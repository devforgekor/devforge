# Deep Dive 분석 보고서 — MCP 의존 구조와 서버측 전환 타당성

> Status: record · Date: 2026-09-11 · Owner: devforge · Related: `docs/reports/mcp-cost-baseline.md`, `docs/reports/mcp-consolidation-applied-20260911.md`
> 작성: 2026-09-11 · 대상: Deep Dive 워크플로우(`llm-agent-rule.md`) · 목적: MCP 의존의 실제 비용/정확도 분석 + 대안 타당성 검증
> 짝 문서: `개선 계획서` = `docs/plans/mcp-consolidation-server-side.md`
> 관련: `docs/reports/control-plane-registry-research.md`, `~/.claude/mcp.json`, `~/.config/opencode/opencode.json`

---

## 0. 요약 (Executive Summary)
- Deep Dive 7단계는 MCP **7종**(yggdrasil·filesystem·lsp·context7·exa-search·search-proxy·fetch) + `shrimp` + `devforge-mcp`에 의존한다.
- 그중 **검색·문서·URL·파일** 계열은 이미 `/opt/projects/server/scripts/`의 서버 코드를 **MCP 프로토콜로 감싼 것**일 뿐이다. MCP는 능력이 아니라 **전송 계층**이다.
- MCP를 줄이는 것은 **가벼워지고**(토큰/프로세스) **툴 선택 정확도가 올라간다**(툴 과다 시 정확도 하락은 벤치로 확인됨).
- 단 **사실 정확도**는 전송 교체가 아니라 **캐시 + cross-encoder 리랭크 + 출처 인용**을 서버측에 붙일 때 오른다.
- `yggdrasil`류 추론 스캐폴드는 **정확도를 더하지 않는다**(구조·기록만). "정확해서 유지" 논리는 성립하지 않는다.
- `shrimp`는 기존 `tasks` DB와 **이중 저장소**로 SSOT를 깬다. `filesystem`은 내장 도구와 중복이다.
- **권고: MCP 의존을 2종(`devforge-mcp`·`lsp`)으로 축소**하고, 리서치는 `lib/research/` + `cli.py research`로 흡수한다.

## 1. Deep Dive 현행 구조
**트리거**: (a) 사용자 명시 "딥다이브", (b) 다중 파일/아키텍처·인터페이스 영향/신규 파일 변경.
**원형**: Devin(Cognition)의 `plan→research→implement→verify` 루프.

| 단계 | 내용 | 사용 도구 |
|---|---|---|
| 1 | 문제 구조화(init→clarify) | `yggdrasil`(deep_planning) |
| 2 | 대상 코드 확인 | `filesystem` |
| 3 | 심볼 분석·참조 추적·blast_radius | `lsp` |
| 4 | 외부 문서/사실 검증 | `context7` **또는** `exa-search`(택1) |
| 5 | 종합·수정 방안(evaluate→finalize) | `yggdrasil`(deep_planning) |
| 6 | 구현 실행 | `shrimp` + `lsp` |
| 7 | 검증(LSP 진단/pytest) | `lsp` / `pytest` |

**구현 상태(이미 서버측)**
- 세션 hang 감지: `deepdive_steps` 테이블 + `deepdive_step_enter/exit/heartbeat/status` 4툴 + 60s 만료 루프(`mcp_server.py`). 단계별 base/min/max bound, 3회 초과 시 ABORTED.
- 7단계 검증 샌드박스: `lib/action_queue.py` 경유 → watchdog `_exec_sandbox_verify()`(podman `--network none --read-only`).

## 2. 의존 인벤토리 (실측)
| MCP | 실제 구현 (서버 파일) | 데이터 출처 | 로컬/원격 | 비고 |
|---|---|---|---|---|
| `search-proxy` | `scripts/proxies/search.py` (`SearchProxy`) | Brave/Tavily/youcom | 로컬(stdio) | 키 로테이션 8개 |
| `exa-search` | `scripts/exa_mcp.py` | Exa API | 로컬(stdio) | 시맨틱 검색 + contents |
| `context7` | `scripts/context7_mcp.py` | Context7 API | 로컬(stdio) | 라이브러리 문서 |
| `fetch` | `uvx mcp-server-fetch` | 임의 URL | 로컬(stdio) | 본문 추출 |
| `filesystem` | `@modelcontextprotocol/server-filesystem` | 로컬 파일 | 로컬(stdio) | 내장 도구와 중복 |
| `yggdrasil` | `yggdrasil-mcp` 바이너리 | 없음(추론 스캐폴드) | 로컬(stdio) | 플랜 파일 저장 |
| `shrimp-task-manager` | Node MCP | 없음(태스크) | 로컬(stdio) | `shrimp_data/` ↔ `tasks` DB 중복 |
| `lsp` | `agent-lsp`(pyright) | 없음(코드 인텔) | 로컬(stdio) | 툴 66개 |
| `devforge-mcp` | `scripts/mcp_server.py` | DevForge DB | **HTTP :8000** | obs/action/검색/deepdive |

**핵심 관찰**: 상위 5개(검색·문서·fetch·filesystem)는 **코어가 이미 서버 코드**이고, MCP는 전송만 담당한다. 즉 "서버측 구현"은 신규 개발이 아니라 **전송 제거 + 통합**에 가깝다.

## 3. 문제 진단

### 3.1 전송/스키마 오버헤드
- MCP 클라이언트는 **매 요청마다** 서버 전 툴 스키마를 컨텍스트에 싣는다(턴 간 캐시 없음). 서버가 많을수록 토큰·초기화 지연·실패면이 커진다.
- 출처: Anthropic(code-execution-with-MCP), MindStudio(턴당 15–20k 토큰), 실측 GitHub=55k 토큰.

### 3.2 툴 과다 → 선택 정확도 저하
- 툴 카탈로그가 커지면 function-calling/툴 선택 정확도가 **떨어진다**.
- 출처: arXiv 2605.24660, Presenc(5툴 85–91% → 20툴 65–78%), Writer RAG-MCP(선택 정확도 13.62%), tianpan.co.

### 3.3 태스크 저장소 이중화
- `shrimp`는 `shrimp_data/`에 저장 → 기존 `tasks` DB(`cli.py task`, `devforge_app`)와 **이중 저장소**. 두 시스템 불일치(drift) 위험.
- 원칙: 단일 진실 소스(SSOT) 위반. 출처: Baserow/Wikipedia SSOT.

### 3.4 스캐폴드 ≠ 정확도
- `yggdrasil`(Sequential Thinking + Deep Planning)은 **사고 기록/구조화** 도구로, *"does not evaluate, generate, or improve the reasoning itself; that stays with the model"*.
- CoT의 가치는 최신 추론 모델에서 **혼재/비유의**. 즉 "스캐폴드를 쓰니 더 정확"은 근거 부족(누락 방지엔 도움).
- 출처: mcpservers.org(seq-thinking), Wharton(CoT 가치 하락), 원 CoT 논문(약한 모델엔 이득) — 목적(외부 사실 검증)엔 무관.

### 3.5 중복 도구
- `filesystem`은 내장 `Read/Write/Edit/Glob/Grep`와 **완전 중복**. 유지 근거 없음.

### 3.6 리서치 결과의 비재현성
- 현재 4단계는 매 호출 결과를 버린다. 캐시·중복제거·리랭크·출처 인용이 없어 **재현·검증이 어렵다**.

## 4. 웹 검증 (주장별)
| # | 주장 | 판정 | 근거 |
|---|---|---|---|
| 1 | MCP는 전송/프로토콜 계층일 뿐 | 지지 | MCP 공식: *"MCP focuses solely on the protocol for context exchange"* |
| 2 | MCP 미사용/축소는 더 가볍다 | 지지 | Anthropic(툴 정의가 컨텍스트 잠식, 98.7%↓), MindStudio(턴당 15–20k, 캐시 없음) |
| 3 | 서버가 많을수록 부정확(툴 선택) | 지지 | arXiv 2605.24660, Presenc, tianpan.co |
| 4 | 전송 교체만으로 사실 정확도↑ 아님 | 지지 | 같은 API면 동일. 리랭크가 +25–40% (NVIDIA/Pinecone/DataAspirant) |
| 5 | 스캐폴드는 사실 정확도 안 더함 | 부분지지 | seq-thinking "improve the reasoning itself" 불가, Wharton CoT 가치 하락. 누락 방지 도움은 인정 |
| 6 | shrimp는 이중 저장소(drift) | 지지(원칙) | Baserow/Wikipedia SSOT |

## 5. 결론 및 권고
1. **MCP 의존 7종 → 2종**(`devforge-mcp`·`lsp`). 검색·문서·fetch 흡수, `filesystem` 제거, `shrimp`→`tasks` DB, `yggdrasil`은 유지(스캐폴드 가치) 또는 대체(선택).
2. 리서치는 **`lib/research/` + `cli.py research`** 로 서버측 구현(전송·스키마 제거).
3. 정확도는 **`research_cache` + reranker(:8080) + 출처 인용**으로 확보.
4. 검증실패 시 롤백 가능하게 `RESEARCH_BACKEND=cli|mcp` 토글 + 전환기 `enabled:false`.

## 6. 리스크
- 전환기 MCP/CLI 이중화로 혼선 → 단계적 전환 + 규칙 문서 동시 갱신.
- `shrimp`의 step-gating을 실제 사용 중이면 대체 비용 발생 → `cli.py task`로 기능 동등성 먼저 확인.
- 외부 API 소스(Exa/Brave/Context7)는 유지 → 정확도 원천은 불변, 손실 없음.

## 7. 출처
- MCP 공식 아키텍처 — modelcontextprotocol.io/docs/2026-07-28/learn/architecture
- Anthropic, Code execution with MCP — anthropic.com/engineering/code-execution-with-mcp
- MindStudio, Claude Code MCP token overhead — mindstudio.ai/blog/claude-code-mcp-server-token-overhead
- arXiv 2605.24660 (툴 수 vs function-calling 정확도), Presenc AI 2026 벤치, tianpan.co(over-tooled agent)
- NVIDIA / Pinecone / DataAspirant — cross-encoder 리랭크 정확도
- mcpservers.org(sequential-thinking), Wharton GAIL(CoT 가치), arXiv 2201.11903(CoT 원논문)
- Baserow / Wikipedia — single source of truth
- 코드: `scripts/proxies/search.py`, `scripts/exa_mcp.py`, `scripts/context7_mcp.py`, `scripts/mcp_server.py`, `lib/llm_client/__init__.py::reranker_score`
