# DevForge Server — Status & Plan

> Status: superseded · Date: 2026-09-14 (내용은 2026-05-24 스냅샷) · Owner: devforge · Related: `docs/REFACTORING_PLAN.md`, see `blueprint.yaml`
> 주의: 이 문서의 Phase 1~3 진행률은 2026-05 기준 스냅샷이다. **현재 코드/아키텍처 정본은
> `docs/REFACTORING_PLAN.md`(+`docs/ARCHITECTURE.md`), phase 추적은 루트 `blueprint.yaml`**이다.

## Phase 1: Basic Infrastructure + MCP Server (Complete, 2026-05-14)

- [x] Podman Quadlet containers (devforge-api, devforge-swap, devforge-qwen, postgres)
- [x] PostgreSQL 16 + pg_trgm + JSONB meta
- [x] MCP SSE server (mem_save, mem_search)
- [ ] POST /ingest (batch conversation storage)
- [x] Python CLI (search/save/recent/worklog add/recent/search)
- [x] GET /stats (7-section dashboard)
- [x] daily pg_dump + monthly restore test
- [x] worklog_entries DB + DB tasks Kanban task tracker (cli.py task list)
- [x] auto_commit_guard.py auto-commit + session_context.py context injection
- [ ] collect_turns.py 15min auto-collect (Claude Code + Copilot sessions)
- [x] link_turns.py KST nightly matching + orphan detection
- [x] activity_log table + commit/stage/review logging
- [x] auto_commit_guard.py git commit → activity_log dual-write
- [x] cli.py activity recent/stats dashboard

## Phase 1.5: LLM Inference Infrastructure (Complete, 2026-05-23)

- [ ] 2-Container architecture — Podman A(3B:8082) + Podman B(mode-switchable:8081-8082)
- [x] Mode switching system (normal / batch / code)
- [x] code_mod_pipeline.py — Deprecated (removed 2026-06-06)
- [x] Prompt ablation — code slicing 89% token reduction
- [x] review_worker.py — 3-LLM debate pipeline (3B + Llama-3B parallel → Phi-4-mini arbitration)
- [x] review_facts DB + performance metrics (prompt_tokens, gen_tokens, gen_rate, cache_hit)
- [x] RateEstimator dynamic timeout
- [x] Slack notification integration (stage completion DM)
- [x] Reference tracking — lib/refs.py (GitHub API 5 projects + internal grep, 15min cycle)
- [x] DB references table + state_collector/main.py integration

## Phase 2: Intelligence & Quality (Active, 2026-05-24)

Tier 1 must complete before Tier 2 begins; Tiers 2 and 3 can overlap.

### Tier 1 — Stabilization (Complete, 2026-05-24)

- [x] LiteLLM removal decision (was failed, unused) → removed
- [x] devforge-llm removal decision (was failed, replaced by swap) → removed
- [x] journald log retention config (MaxRetentionSec=30day)
- [x] Language pipeline guardrails — `lib/text_quality.py` (script purity validation for Korean output, token budget enforcement 10~500 chars, think-tag artifact detection)
- [x] update_handover.py context selection — quality-score-based prioritization of high-fidelity turns

### Tier 2 — Vector Intelligence (Active, ~70%)

**Korean Search Pipeline (FTS5 BM25 + Dense Embedding + RRF Hybrid)** — Qwen3-Embedding-8B (4096d, local).

- [x] pgvector extension installed
- [x] `lib/text_cleaner.py` — Kiwi 기반 한국어 정규화 + 형태소 분석 + 어휘 추출
- [x] Cleaned text columns (user_turn_clean, text_clean, thinking_clean, tokens jsonb)
- [x] Turn watcher 자동 clean 처리 (INSERT 시점)
- [x] SQLite FTS5 search index — contentless model, BM25 weighted (5/2/3/1)
- [x] FTS5 실시간 증분 동기화 (turn_watcher.sync_fts5)
- [x] `lib/search/hybrid.py` — RRF k=60 하이브리드 검색 (BM25 + Dense)
- [x] Strong signal short-circuit — BM25 top-1 ≤ -8.0 & gap ≥ 0.15 → dense 생략
- [x] `cli.py search bm25 <query>` — FTS5 BM25 키워드 검색
- [x] `cli.py search hybrid <query>` — BM25 + Dense RRF fusion (코드 완료)
- [x] 월 1회 FTS5 rebuild (night_cycle.sh, 1st only)
- [x] Day-cycle 자동 증분 임베딩 (embed_batch.py, day_cycle.sh 내장)
- [~] Dense embedding via Qwen3-Embedding-8B (4096d, `embedding_f16`) — **실행 중** (74/8,766)
- [ ] CLI search --wing/--room filtering
- [ ] embed_turns.py Gemini → deprecated (migrated to Qwen 8B)
- [ ] MCP mem_search hybrid search upgrade (FTS5 + Dense + RRF)

### Tier 3 — Advanced Search & Ops

- [ ] Monthly container image update — local LLM version check, minor auto-pull, major Slack report
- [ ] Snyk/CISA vulnerability auto-scan (container images, 3 only)
- [ ] MemPalace auto-classification — LLM-based wing/room assignment
- [ ] Cross-lingual Wikipedia anchor corpus (lower priority)
- [ ] search --augmented — DuckDuckGo + local LLM inference
- [ ] Web UI — Conversation search dashboard (FastAPI + simple frontend)
- [ ] Web UI — Model performance dashboard (review_facts stats visualization)
- [ ] Web UI — activity_log real-time feed

## Phase 3: Someday/Maybe

- [ ] Chrome Extension → direct server submission (currently Mac-relayed)
- [ ] iOS Shortcuts integration
- [ ] Multi-LLM routing (additional model integration)
- [ ] User decision rationale tracking — structured decision logging with evidence chain
- [ ] Back-translation fidelity check — when fast translation API available

## Overall Progress

```
Phase 1  ████████████████████ 100%
Phase 1.5 ████████████████████ 100%
Phase 2  ████████████████░░░░ 70%
Phase 3  ░░░░░░░░░░░░░░░░░░░░ 0%
```












