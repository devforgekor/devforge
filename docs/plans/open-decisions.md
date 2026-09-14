# 의사결정 대기 목록 (Open Decisions)

> Status: active · Date: 2026-09-14 · Owner: devforge · Related: `docs/plans/cutover-remaining-plan.md`, `docs/adr/0005-extraction-routing.md`, `docs/adr/0006-mcp-tool-surface.md`, `docs/specs/ingest-provenance.yaml`
> 목적: 컷오버/리팩토링 진행을 막는 **사용자(오너) 결정 사항**을 배경·선택지·영향·권장과 함께 고정한다.
> 사용법: 각 항목의 `결정:` 칸에 선택지를 적으면, 아래 §4의 실행 매핑대로 착수한다. 상태: `대기 → 승인/보류`.

---

## 0. 결정 요약표

| ID | 결정 | 선택지 | 권장 | 상태 |
|---|---|---|---|---|
| **D1** | 추출 아키텍처 | 승인 / 수정 / 보류 | 승인 | 대기 |
| **D2** | MCP 전략 | 승인 / 조정 / 보류 | 승인 | 대기 |
| **D3** | 웹 수집(chrome-web) | 재가동 / 은퇴 | 재가동 | 대기 |
| **D4** | 첫 적용 착수 | Phase A-1 착수 / 순서변경 | A-1 착수 | 대기 |
| **D5** | ingest 보안·프라이버시 | loopback/bearer, redaction, provenance 값 | loopback+bearer, redaction 적용, 값 유지 | 대기 |
| **D6** | 범위/일정 | worker 이관, 인력, 컷오버 방식 | 흡수·범위축소·병렬전환 | 대기(비차단) |

최소 응답 예: `1a 2a 3a 4a 5-1 loopback+bearer 5-2 적용 5-3 유지, 6 보류`

---

## D1. 추출 아키텍처 승인 (`ADR-0005`)

- **배경**: `day_cycle`이 4코어 ARM CPU-only에서 로컬 8B로 extract→verify→enrich→embed를 **단일 배치**로 수행 → 메모리 대역폭 병목으로 본질적으로 느림(`reports/architecture-validation.md`). 서버는 이미 클라우드 경로(OpenRouter RR, DeepSeek/Anthropic/Gemini 프록시, Azure Qwen)를 보유.
- **선택지**
  - (a) **승인**: `deterministic prefilter → 소형 로컬(constrained decoding) → hard만 클라우드 escalation`. 출력은 strict schema + evidence binding + confidence gate + 1회 재시도 후 quarantine. heavy 추출은 **비동기 batch tier**. 서버 역할은 **검증/후보정**(NLI grounding·dedup·entity resolution) 중심.
  - (b) **수정**: 라우팅 비율/우선순위(예: 클라우드 우선, 로컬은 embedding/rerank만) 변경.
  - (c) **보류**: 현행 로컬 8B 배치 유지.
- **영향**: 파이프라인 지연, 클라우드 비용, `pipeline_stages`/`application` 재설계 범위(Phase C), 기존 `review_facts` 의미(1차 추출 vs 후보정).
- **권장**: **(a)**. 단, 정확도 검증을 위해 골든셋(field-level precision/recall)과 **escalation rate** 측정을 수락기준에 포함.
- **근거**: `reports/industry-standard-comparison-20260914.md` §5.3, `adr/0005-extraction-routing.md`.
- **결정**: _<미정>_ / 일자: ____

## D2. MCP 전략 승인 (`ADR-0006`)

- **배경**: 활성 MCP 서버 4개. 서버 노출은 `devforge-mcp` **25**, `lsp` **65**지만, **opencode `tools` allowlist가 `devforge 12 / lsp 13 / yggdrasil 4 (+opencode-db 무필터)** ≈ **33개로 프루닝** → 서버별 10~20 충족, **opencode에선 이미 해소**. 잔여 리스크: (a) allowlist 없는 클라이언트(예: Claude Code)는 25/65 전량 로드, (b) 리팩토링 MCP(`adapters/driving/mcp`=**5툴**)가 **허용 12툴 계약** 미보존 시 컷오버 기능 소실, (c) `ingest` 미복원.
- **선택지**
  - (a) **승인**: FastMCP Streamable HTTP 정합 + 네임스페이스 8종. **opencode 허용 12툴 계약 보존**(전체 25 기본노출 금지) + `ingest`(HTTP+MCP) 복원. 점진공개(`search_tools`)는 무필터 클라이언트 대비 **옵션**. 툴 description에 사용/비사용 경계 명시.
  - (b) **조정**: always-on 집합·네임스페이스 경계를 사용자가 지정.
  - (c) **보류**: 현행 유지(컷오버 불가).
- **영향**: 에이전트 클라이언트 무변경 전환 가능성, 컨텍스트 비용, Phase A 범위.
- **권장**: **(a)**.
- **근거**: `adr/0006-mcp-tool-surface.md`, MCP 공식 client best practices.
- **결정**: _<미정>_ / 일자: ____

## D3. 웹 수집(chrome-web) 방향

- **배경**: 웹 LLM(ChatGPT/Claude/Gemini/AI Studio/DeepSeek) 대화를 수집하는 Chrome 확장이 **아카이브**되어 있고(`_archive/seedling/chrome-extension`, `~/.local/share/chrome-web-llm/`에 별도 구현), 서버 파이프라인에 **미연결**. `chat-history-recorder-mcp`도 미설치. 결과적으로 웹 대화는 지식베이스에 유입되지 않음.
- **선택지**
  - (a) **재가동**: 확장(Manifest v3 현대화) + Native Messaging + loopback relay → 서버 `ingest`로 직접 수집(ADR-0006). provenance `chrome:<provider>` 기록.
  - (b) **은퇴**: 확장 폐기, `scripts/web_chat.py`(Playwright)와 수동 export만 유지.
- **영향**: 수집 커버리지(웹 LLM 포함 여부), 보안/프라이버시(D5), Phase B 범위.
- **권장**: **(a)**, 단 D5 보안 조건 충족 시.
- **근거**: `reports/industry-standard-comparison-20260914.md` §5.1.
- **결정**: _<미정>_ / 일자: ____

## D4. 첫 적용(구현) 착수 승인

- **배경**: 문서/계약(`specs/ingest-provenance.yaml`)이 고정됨 → 이제 코드 적용 시작 지점 결정.
- **선택지**
  - (a) **Phase A-1 착수**: `POST /api/v1/ingest` + MCP `ingest` 툴 구현 + provenance 기록. (스펙 준수, 단위·E2E 테스트 포함)
  - (b) **순서 변경**: 예) provenance 먼저, 툴 표면 정리 먼저, 웹 수집 먼저 등.
- **영향**: 컷오버 진행 속도, 의존 단계.
- **권장**: **(a)** — `ingest`/provenance가 웹 수집(D3)·MCP 복원(D2)의 선행.
- **결정**: _<미정>_ / 일자: ____

## D5. ingest 보안 · 프라이버시 정책

- **D5-1 노출 범위**: `POST /api/v1/ingest` 접근 제어
  - 선택: `loopback-only(127.0.0.1)` / `bearer 토큰(secrets.env)` / **둘 다**
  - 권장: **loopback + bearer(외부망 사용 시)**
- **D5-2 민감정보 처리**: 웹 대화 캡처 시 시크릿(토큰/쿠키/API 키) **redaction** 적용
  - 선택: 적용 / 미적용
  - 권장: **적용**(write 시점 redaction)
- **D5-3 provenance 값 확정**: `chrome:chatgpt|claude|gemini|deepseek|aistudio|qwen`, `claude-code`, `opencode`, `aider`, `copilot`, `gemini-cli`, `mcp_ingest`
  - 선택: 유지 / 수정 / 추가
  - 권장: **유지**(`specs/ingest-provenance.yaml` 기준)
- **영향**: 보안 사고 표면, 규정 준수, 수집 신뢰성.
- **결정**: D5-1 _<미정>_ / D5-2 _<미정>_ / D5-3 _<미정>_ / 일자: ____

## D6. 범위 · 일정 (비차단)

- **D6-1 worker 이관**: `container-devforge-worker`(worker_supervisor)를 **application 흡수** vs 별도 어댑터. 권장: 흡수.
- **D6-2 인력/일정**: `REFACTORING_PLAN` v1.4 **2인·14주** 가정 vs 1인/범위축소. 권장: 범위 우선순위화(A→B→C 먼저).
- **D6-3 컷오버 방식**: **병렬 검증 후 전환** vs 즉시 전환. 권장: 병렬 검증(비파괴, 롤백 보존).
- **결정**: D6-1 _<미정>_ / D6-2 _<미정>_ / D6-3 _<미정>_ / 일자: ____

---

## 4. 결정 → 실행 매핑

| 결정 | 실행 산출물 | 문서 갱신 |
|---|---|---|
| D1 승인 | `pipeline_stages` 라우팅 + 골든셋 eval | ADR-0005 Accepted, Phase C |
| D2 승인 | MCP 네임스페이스·`search_tools`·`ingest` 툴 | ADR-0006 Accepted, Phase A |
| D3 재가동 | 확장+relay → `ingest` 편입 | Phase B, INDEX |
| D4 착수 | `api/v1/ingest` + MCP `ingest` 코드 | `specs/ingest-provenance.yaml` 구현 상태 |
| D5 확정 | 인증·redaction 구현 | 스펙에 auth/redaction 반영 |
| D6 확정 | 워커/일정/전환 방식 확정 | cutover-remaining-plan 갱신 |

## 5. 근거

- `docs/reports/industry-standard-comparison-20260914.md` — 업계 표준 5계층 대조
- `docs/adr/0005-extraction-routing.md`, `docs/adr/0006-mcp-tool-surface.md` — 결정 기록(proposed)
- `docs/specs/ingest-provenance.yaml` — ingest/provenance 계약
- `docs/plans/cutover-remaining-plan.md` §9 미결 — 상위 계획
- 서버 실측: `turn_watcher` 소스, `devforge-mcp` 툴 목록(25+), `turns.source`=전량 `unknown`
