# DevForge Server — Status & Plan

## Phase 1: Basic Infrastructure + MCP Server (Complete, 2026-05-14)

- [x] Podman Quadlet containers (devforge-api, devforge-swap, devforge-qwen, postgres)
- [x] PostgreSQL 16 + pg_trgm + JSONB meta
- [x] MCP SSE server (mem_save, mem_search)
- [x] POST /ingest (batch conversation storage)
- [x] Python CLI (search/save/recent/worklog add/recent/search)
- [x] GET /stats (7-section dashboard)
- [x] daily pg_dump + monthly restore test
- [x] worklog_entries DB + tasks.yaml Kanban task tracker
- [x] auto_commit_guard.py auto-commit + session_context.py context injection
- [x] collect_turns.py 15min auto-collect (Claude Code + Copilot sessions)
- [x] link_turns.py KST nightly matching + orphan detection
- [x] activity_log table + commit/stage/review logging
- [x] auto_commit_guard.py git commit → activity_log dual-write
- [x] cli.py activity recent/stats dashboard

## Phase 1.5: LLM Inference Infrastructure (Complete, 2026-05-23)

- [x] 2-Container architecture — Podman A(Qwen3-4B:8080) + Podman B(mode-switchable:8081-8082)
- [x] Mode switching system (normal / batch / code)
- [x] code_mod_pipeline.py — 32B 4-stage pipeline (ANALYZE→PLAN→IMPL→PACKAGE)
- [x] Prompt ablation — code slicing 89% token reduction
- [x] review_worker.py — 3-LLM debate pipeline (Qwen3-4B + Llama-3B parallel → Phi-4-mini arbitration)
- [x] review_facts DB + performance metrics (prompt_tokens, gen_tokens, gen_rate, cache_hit)
- [x] RateEstimator dynamic timeout
- [x] Slack notification integration (stage completion DM)
- [x] Reference tracking — lib/refs.py (GitHub API 5 projects + internal grep, 15min cycle)
- [x] DB references table + state_collector/main.py integration

## Phase 2: Intelligence & Quality (Active, 2026-05-24)

Phase 2 is structured in 3 tiers. Tier 1 must complete before Tier 2 begins; Tiers 2 and 3 can overlap.

### Tier 1 — Stabilization (This Week)

- [x] LiteLLM removal decision (was failed, unused) → removed (2026-05-19)
- [x] devforge-llm removal decision (was failed, replaced by swap) → removed (2026-05-19)
- [ ] Qwen3-4B (Podman A) re-enable — currently down after 32B code mode
- [ ] review-worker.timer re-enable — 3-LLM debate pipeline reactivation
- [ ] swap-batch.timer + swap-normal.timer re-enable — mode auto-switching
- [x] journald log retention config (MaxRetentionSec=30day)
- [x] Language pipeline guardrails — `lib/text_quality.py` (script purity validation for Korean output, token budget enforcement 10~500 chars, think-tag artifact detection)
- [ ] update_handover.py context selection — quality-score-based prioritization of high-fidelity turns
- [ ] T01-T16 32B batch test results → apply verified diffs (currently T11 in progress)

### Tier 2 — Vector Intelligence (2-4 Weeks)

> Research references: `docs/translation-quality-report.md` (model-radar lessons), `docs/translation-quality-feedback-loop.md` (TEaR + xCOMET + DCSQE feedback architecture). LLM selection for translation tasks deferred pending Qwen/Phi-14B/32B quality comparison test results.

- [x] pgvector extension installed (vector 0.8.2) + turns.embedding vector(768) column
- [x] HNSW index on turns.embedding (vector_cosine_ops)
- [x] embed_turns.py operational — 1015/4180 turns embedded (Gemini embedding-embedding-001)
- [ ] embed_turns.py complete remaining 3165 turns
- [ ] Cross-lingual Wikipedia anchor corpus — ko.wikipedia + en.wikipedia embeddings keyed by shared Q-item (Wikidata ID), pgvector table
- [ ] Translation quality estimation — cosine_similarity(embed_ko, embed_en) using Wikipedia Q-item anchor as ground truth
- [ ] embed_turns.py cost optimization — skip trivial turns (< 20 chars), prioritize decisions/observations
- [ ] MemPalace auto-classification — LLM-based wing/room assignment (follows review_worker parallel extraction pattern)
- [ ] CLI search --semantic (pgvector ANN + ILIKE hybrid)
- [ ] CLI search --wing/--room filtering
- [ ] MCP mem_search vector search upgrade (pgvector ANN)
- [ ] search --augmented — DuckDuckGo + local LLM inference pipeline
- [ ] Ref: `/opt/projects/server/docs/translation-quality-report.md` (model-radar lessons applied)

### Tier 3 — Operations & Visibility (1-3 Months)

- [x] rss-monitor.service — GitHub RSS periodic polling (all 9 references)
- [x] 4-month refresh cycle alert → automated (first run 2026-08-15)
- [ ] Snyk/CISA vulnerability auto-scan (container images)
- [ ] refresh-log.md auto-recording
- [ ] Web UI — Conversation search dashboard (FastAPI + simple frontend)
- [ ] Web UI — Model performance dashboard (review_facts stats visualization)
- [ ] Web UI — activity_log real-time feed

## Phase 3: Someday/Maybe

- [ ] Chrome Extension → direct server submission (currently Mac-relayed)
- [ ] iOS Shortcuts integration
- [ ] Multi-LLM routing (additional model integration)
- [ ] User decision rationale tracking — structured decision logging with evidence chain
- [ ] Back-translation fidelity check — when fast translation API available
- [ ] T01-T16 32B code modification full re-test (intermittent execution)
