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

## Phase 2: Enhancement (Planned)

### 2.1 Recovery / Stabilization (Immediate)
- [x] swap-batch.timer + swap-normal.timer reactivated
- [x] review-worker.timer reactivated
- [x] LiteLLM recovery or removal decision (was failed, unused) → removed (2026-05-19)
- [x] devforge-llm recovery or removal decision (was failed, replaced by swap) → removed (2026-05-19)
- [ ] journald log retention config (MaxRetentionSec=30day)

### 2.2 Semantic Search (pgvector)
- [x] pgvector extension installed + turns.text embedding vector column
- [x] Embedding generation (embed_turns.py, local Qwen or API)
- [x] CLI search --semantic (pgvector ANN + ILIKE hybrid)
- [ ] MCP mem_search vector search upgrade

### 2.3 MemPalace Classification
- [ ] wing/room category schema definition
- [ ] Auto-classification (LLM-based, similar pattern to review_worker)
- [ ] CLI search --wing/--room filtering
- [ ] Classification results activity_log recording

### 2.4 Search-Augmented Integration
- [ ] DuckDuckGo search → LLM inference pipeline
- [ ] /opt/projects/qwen-cli based extension (if available)
- [ ] cli.py search --augmented (search + LLM analysis results)
- [ ] Ref: memory/qwen-cli-reference.md (2026-05-14)

### 2.5 Reference Tracking Upgrade
- [x] rss-monitor.service — GitHub RSS periodic polling (all 9 reference-watchlist.md items)
- [ ] Snyk/CISA vulnerability auto-scan (container images)
- [x] 4-month refresh cycle alert → automated (first run 2026-08-15)
- [ ] refresh-log.md recording automation

### 2.6 Web UI
- [ ] Conversation search dashboard (FastAPI + simple frontend)
- [ ] Model performance dashboard (review_facts stats visualization)
- [ ] activity_log real-time feed

## Phase 3: Someday/Maybe

- [ ] Chrome Extension → direct server submission (currently Mac-relayed)
- [ ] iOS Shortcuts integration
- [ ] Multi-LLM routing (additional model integration)
- [ ] T01~T08 32B code modification full re-test (intermittent execution)
