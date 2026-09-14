# 업계 표준 대비 시스템 진단 — 캡처·기억·추출·MCP

> Status: record · Date: 2026-09-14 · Owner: devforge · Related: `docs/plans/cutover-remaining-plan.md`, `docs/adr/0005-extraction-routing.md`, `docs/adr/0006-mcp-tool-surface.md`
> web 조사(2026-09-14)와 서버 실측을 대조하여, 리팩토링/컷오버 방향을 업계 표준에 정렬하기 위한 근거 문서.

---

## 1. 목적
- "CLI/Neovim + 웹 LLM + MCP" 계보(chrome-web-llm → 통합 기억 MCP → devforge-mcp)가 **업계 표준과 어디서 일치/이탈**하는지 규명.
- 특히 **느린 로컬 8B 배치 추출(day_cycle)** 과 **MCP 툴 과다** 문제의 표준 해법을 근거와 함께 제시.

## 2. 방법
- 웹 조사: 브라우저 자동화/캡처, 에이전트 기억(MCP), 추출 파이프라인, MCP 툴 표면 관리, 로컬 vs 클라우드 비용.
- 서버 실측: `turn_watcher` 소스, `devforge-mcp` 툴 목록, `src/devforge` 구현도, `turns.source` 값, `architecture-validation.md`(ARM CPU 실측).
- 비교 프레임 = **5계층**: Capture / Transport / Ingestion / Storage·Retrieval / Extraction·Serving.

## 3. 업계 표준 (5계층)

### 3.1 Capture
- **에이전트 lifecycle hook**이 표준: `SessionStart`/`PromptSubmit`/`PostToolUse`/`SessionEnd`/`Stop`에서 **모델 판단 없이 결정론적으로** raw 캡처. hook은 실패해도 턴을 막지 않는다(fail-silent).
- **웹 UI 캡처**는 **브라우저 확장 + Native Messaging**으로 "이미 로그인된 실제 브라우저"를 사용. 격리 프로필(Playwright 기본)은 로그인/2FA 벽을 매번 넘어야 하므로 부적합.
- raw는 **L0(무가공) → L1(facts) → L2(scenes) → L3(persona)** 로 승격.

### 3.2 Transport / Bridge
- 실 브라우저 제어 = **MCP tool server**(Playwright MCP, Chrome DevTools MCP) vs **실크롬 캡처 = extension + Native Messaging + loopback relay**(browsermcp, chrome-relay, browser-agent-bridge, pkrelay).
- 두 방식은 "누가 두뇌인가"가 다름: MCP 서버는 툴만 제공(호스트가 추론), Native Messaging은 로컬 프로세스가 CDP 브리지.

### 3.3 Ingestion
- hook → **atomic queue** → idempotent **importer**(정규화, governance gate: empty/self-capture/trivial 필터, project resolver). 중복은 checkpoint/해시로 방지.

### 3.4 Storage / Retrieval
- Postgres + **pgvector**, **FTS(BM25) + vector + RRF** 하이브리드, knowledge graph, dedup/conflict/TTL, provenance(`source`, `confidence`, `updated_at`).
- write 경로와 indexing 경로 분리(즉시 delta → 비동기 compaction).

### 3.5 Extraction / Serving
- **deterministic-first, LLM-second**: 입력 40~60%는 regex/룰. LLM은 애매한 케이스만.
- **구조화 강제**: JSON Schema/`strict` Pydantic + **constrained decoding(GBNF/XGrammar)** → 스키마 유효도 99%+.
- **evidence binding**: 추출값이 원문에 존재하는지 검증 → 환각 ~60% 감소.
- **하이브리드 라우팅**: routine은 소형 로컬, hard만 frontier → frontier 비용 60~90% 절감. escalation rate를 1급 지표로.
- **비동기 2계층**: near-real-time(가벼움) + batch(rate-limited, 무거움). 지연 크리티컬 패스에 heavy 추출 배치 금지.
- **MCP 툴 표면**: 서버당 **10~20 활성 툴** 권장, 30~50 넘으면 선택 정확도 급락. 15~20 초과 시 **progressive discovery(`search_tools`/`defer_loading`)**.

## 4. 시스템 대조 요약

| 계층 | 업계 표준 | 본 시스템 | 판정 |
|---|---|---|---|
| Capture(로컬) | hook | `turn_watcher` **3초 폴링** + 파서 5종 | ⚠️ 개선 |
| Capture(웹) | extension+Native Messaging | 확장 **아카이브**, recorder-mcp 미설치 | ❌ 갭 |
| Ingestion | hook→queue→importer | `turn_watcher`→raw→`raw_consumer` | ⚠️ 폴링/별도 worker |
| Storage/Retrieval | FTS+vector+RRF, KG, L0~L3 | Postgres+pgvector+FTS5+RRF | ✅ 일치(계층 일부 부재) |
| Extraction | deterministic→LLM, 구조화강제, evidence, hybrid | 로컬 8B **배치**(day_cycle) | ❌ 이탈 |
| Serving(MCP) | 10~20 툴 + 점진공개 | `devforge-mcp` **25+** 전량 선로딩 / 리팩토링본 5 | ❌ 과다/불일치 |
| provenance | source/confidence | `turns.source` 전부 `unknown` | ❌ 미기록 |

## 5. 세부 진단

### 5.1 웹 수집(chrome-web) — 미연결
- `chrome-web-llm`(확장+relay+fallback)은 표준 패턴과 **일치**하나, 이 리포 파이프라인에 **미연결**.
- 수신 표면: 레거시 `scripts/mcp_server.py`의 MCP `ingest` 툴만 존재, `POST /ingest`는 미구현, 리팩토링 MCP에는 **`ingest` 없음**.
- 결과: 웹 대화가 `turns`로 유입되지 않고, 유입돼도 provenance(`source`)가 안 남음(`turns.source` 7,131건 전부 `unknown`).

### 5.2 MCP 툴 표면 — 과다
- `devforge-mcp` = **25+ 툴**(knowledge/memory/obs/action/deepdive/review/ingest/telegram/flaresolverr) 전량 선로딩 → 표준(10~20) 초과.
- 리팩토링 MCP(`adapters/driving/mcp`)는 **5툴**로 반대편 불일치 → 컷오버 시 기능 소실 위험.

### 5.3 추출/day_cycle — 로컬 8B 배치
- 4코어 ARM CPU-only에서 로컬 8B 추론은 **memory-bandwidth-bound** → 본질적으로 느림(`reports/architecture-validation.md`와 일치).
- 그런데 이 서버는 **이미 클라우드 경로 보유**(OpenRouter RR, DeepSeek/Anthropic/Gemini 프록시, Azure Qwen). 표준은 이 경우 "routine=소형 로컬, hard=클라우드"로 라우팅.
- 사용자 가설("extract는 에이전트 측 로직으로 처리되고, 서버는 후보정/검증")은 표준과 부합: **1차 추출 → 검증/후보정**으로 역할을 재배치하는 것이 정답.

## 6. 권고 (granular)

### 6.1 캡처/수집
- `turn_watcher` 폴링은 유지하되, **hook 기반 캡처를 우선**으로 도입(외부 에이전트가 지원 시).
- 웹 수집: **Chrome 확장 + Native Messaging + loopback relay**를 표준으로 채택(격리 Playwright 프로필 지양).
- `ingest` 수신을 **API(`/api/v1/ingest`)** 와 **MCP `ingest` 툴** 양쪽에 제공(하나를 정본, 하나를 호환).

### 6.2 저장/검색
- `turns.source`/`agent`/`confidence`/`updated_at` **provenance 표준화**(예: `chrome:qwen`, `claude-code`, `opencode`).
- 기억 승격 계층 도입(L0 raw → L1 facts → L2 scenes), write/index 분리.

### 6.3 추출 파이프라인
- `deterministic prefilter` → `소형 로컬(constrained decoding)` → `hard는 클라우드 escalation`.
- 출력 계약: JSON Schema/Pydantic `strict`, **evidence binding**, confidence gate, **1회 재시도 후 quarantine**.
- heavy 추출을 **비동기 batch tier**로 이동(크리티컬 패스 제거). 서버의 1차 역할은 **검증/후보정(NLI grounding, dedup, entity resolution)**.

### 6.4 MCP
- `devforge-mcp`를 **네임스페이스 분할 + 10~20 활성 툴**로 재편, 초과분은 **`search_tools`(progressive discovery)**.
- 툴 description에 **"언제 쓰고 언제 쓰지 말지(경계)"** 명시.

## 7. 검증/측정 지표
- MCP: 툴 정의 토큰(컨텍스트 대비 %) < 5%, 툴 선택 정확도 골든셋.
- 추출: field-level precision/recall, **escalation rate**, 드리프트(골든셋 재채점).
- 파이프라인: raw 캡처 누락 0, provenance 커버리지 100%.

## 8. 결론
- 이 시스템의 계보(웹 LLM 캡처 → 통합 기억 → MCP)는 업계 표준과 동일한 방향이다.
- 이탈점은 **(a) 웹 수집 미연결**, **(b) MCP 툴 과다**, **(c) 로컬 8B 배치 추출** 세 가지이며, 모두 표준 패턴으로 교정 가능하다.
- 우선순위: **MCP 툴 표면·ingest 복원 → 웹 수집 편입·provenance → 추출 라우팅 재설계**.

## 9. 출처
- MCP 공식 Client Best Practices (progressive discovery) — https://modelcontextprotocol.io/docs/2026-07-28/develop/clients/client-best-practices
- MCP 툴 스프롤/점진공개 — https://www.stridetechworks.com/blog/mcp-tool-sprawl-progressive-discovery , https://www.channel.tel/blog/mcp-progressive-tool-discovery , https://dev.to/ji_ai/mcp-tool-sprawl-why-40-tools-wreck-tool-selection-accuracy-2pnk
- 실크롬 캡처(extension+Native Messaging) — https://github.com/nooma-stack/pkrelay , https://github.com/kiluazen/chrome-relay , https://github.com/escapeWu/chrome-agent-bridge
- 브라우저 제어 MCP 비교 — https://browserbash.com/blog/playwright-mcp-vs-browser-use , https://browsermcp.dev/compare/browser-automation-mcp-servers/
- 에이전트 기억(MCP/hook/계층) — https://github.com/liangquanzhou/kg-memory-mcp , https://github.com/chirino/memory-service , https://medium.com/neo4j/orchestrating-an-agentic-memory-system-...
- 추출 파이프라인(deterministic-first, structured, evidence) — https://github.com/wilsebbis/llm-extraction-pipeline , https://github.com/tahasiddiquii/llm-extraction-pipeline , https://dzone.com/articles/llm-multi-agent-data-extraction
- 로컬 vs 클라우드/구조화 추출 — https://bestllmfor.com/best/structured-data-extraction-json/ , https://shawnng.com/posts/local-models-vs-frontier , https://www.aitoolpipelines.com/articles/local-llm-vs-cloud-api-cost-comparison
