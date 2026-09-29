# Extraction & Pipeline Routing Decision Record (ADR-0005)

## Status
Proposed (2026-09-14) — recommendation; not yet implemented.

**실행 목표: 2026-10-13 (시스템 변경 후) daycycle API 전환** — 재검토 체크포인트는
`handover.yaml` known_issues `q4-concurrency-recheck-2026-10-13` (CLI task #501).

## Execution evidence (2026-09-29 실측)

| 항목 | 측정/판정 |
|---|---|
| 하드웨어 | 4코어 ARM Neoverse-N1 · RAM 22,945MB · swap 4,095MB · 컨테이너 CPU/메모리 제한 없음 |
| 모델 | **Q8_0 → Q4_K_M 전환 완료** (`scripts/lib/model_registry.py` SSOT) |
| 동시 기동 | embed 단독 12.5GB · day(extract+reranker) 12.5GB · **동시 21.9GB(잔여 1GB) → 부적합** → mode-exclusive 유지 |
| 임베딩/리랭커 | **OpenCode 계열 API 부재** — GO 30종·ZEN 44종 전부 chat, `/embeddings`·`/rerank`·`/v1/*` 전부 404 → **로컬 고정 확정** |
| cloud 경로 | `https://opencode.ai/zen/go/v1` (기존 `OPENCODE-GO-API-KEY` 재사용): `mimo-v2.6-flash` 5.1s · `deepseek-v4-flash` 1.1s · `response_format:json_object` 통과 |
| 필수 헤더 | `User-Agent`(없으면 CF 403/error 1010) · `x-opencode-session`(없으면 400 MissingSessionID) |
| 무료 티어 | Zen `*-free` 모델은 `403 FreeTierError: only used from within OpenCode` → **파이프라인 사용 불가** |
| 스키마 이탈 | 동일 프롬프트에 mimo는 객체 스키마 준수 / deepseek는 `{"facts":[문자열]}`로 탈출 → **Decision 3·4(구조화 출력+검증) 필요함을 실측 확인** |
| 물량·한도 | `review_facts` 769건/7일(≈110/day) → 월 ~10k 호출 · Go 한도 150,400req/월·$60/월, 추정 비용 ~$2/월 · **5시간 $12 한도가 실제 병목** |

### 확정된 초기 폴백 체인
```
mimo-v2.6-flash → 실패/429/5xx → deepseek-v4-flash → 실패 → 로컬 Q4 (llama.cpp)
```
embed(8081)·reranker(8080)는 위 체인에 포함하지 않는다 — 로컬 전용.

## Context
`day_cycle` performs extract → verify → enrich → embed as a single heavy batch on a
4-core ARM CPU-only host using a local 8B model. ARM decode is memory-bandwidth-bound,
so this is inherently slow (`docs/reports/architecture-validation.md`). Meanwhile the
host already has cloud routes (OpenRouter RR, DeepSeek/Anthropic/Gemini proxies, Azure
Qwen). Industry practice for structured extraction is: deterministic-first, LLM-second,
strict schema, evidence binding, hybrid local/cloud routing, and asynchronous tiers.

Evidence: `docs/reports/industry-standard-comparison-20260914.md`.

## Decision
Re-architect extraction as a **tiered, routed, post-correction-oriented** pipeline:

1. **Deterministic prefilter** (regex/rules) handles high-certainty input first (~40–60%).
2. **Routine extraction** uses a small local model with **constrained decoding** (GBNF/
   XGrammar) — schema validity ≥99% — only for the ambiguous remainder.
3. **Hard cases escalate to cloud** small/frontier models with **structured outputs**
   (JSON Schema / `strict` Pydantic). Track **escalation rate** as a first-class metric.
4. Every extraction is validated: schema + **evidence binding** + confidence gate, with
   **one retry then quarantine** (fail-closed).
5. Heavy extraction runs in an **asynchronous batch tier**, never on the critical path.
6. Server's primary role shifts toward **verification/post-correction** (NLI grounding,
   dedup, entity resolution, provenance) where the agent/client already produced structure.

## Consequences
- Faster pipeline; frontier spend only for genuinely hard inputs.
- Requires an evalset (field-level precision/recall) and drift detection.
- Adds a routing/validation layer (`ports/inference` already exists; add clarity adapter + router).
- Supersedes the "single local-8B batch" assumption in `REFACTORING_PLAN` Phase 3.
