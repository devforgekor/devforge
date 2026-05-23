# Activity Log — Unified Event Recording Architecture

**Version**: 1.3
**Date**: 2026-05-22
**Status**: APPROVED (peer review incorporated — 4 reviewers, see Section 10)
**Language**: English (machine-readable). This document is designed for AI agent review.
**Purpose**: Merge `worklog_entries` + pipeline traces + observations into a single event table, with async 3-stage nightly relay support.

---

## 0. Source Context (unedited excerpts from existing docs)

### 0.1 From PLAN.md (v2.0, 2026-05-18)

> ```
> ### 3.2 Current Gap
> `worklog_entries` table has NO git commit tracking column. Current recording is manual via `cli.py worklog add`.
>
> ### 3.4 Phase 2: B-Plan (After v1.1 stability — 2-4 weeks)
> **Goal**: Full worklog reconciliation with LLM-enhanced summaries.
> **Implementation**: Single `worklog_reconcile.py` (~300 lines)
> ```

### 0.2 From worklog-v1.1-to-b-plan-roadmap.md (2026-05-18)

> B-Plan was designed as 7 modules: worklog_config.py, git_scanner.py, llm_caller.py, db_saver.py, lock_manager.py, worklog_reconcile.py, systemd service.
> Decision D2 (PLAN.md): "B-Plan: 1 file, not 7 — review_worker.py pattern proven"

### 0.3 Current infrastructure (verified 2026-05-22)

```
Host: OCI ARM (24GB RAM), Oracle Linux 9.7, 4-core
Runtime: Podman rootless, Quadlet systemd
Database: PostgreSQL 16 (devforge_app)
NO LiteLLM — removed 2026-05-19. All LLM calls use direct llama.cpp HTTP API.

Running containers:
  devforge-api :8000     — MCP SSE, /ingest, /stats, /slack/*
  devforge-swap :8081-2 — Podman B (mode-switchable, dual-server)
  data-pod → postgres :5432

Podman B — 3 modes (dual-server-entrypoint.sh, current-mode.env):
  MODE=normal  → :8081 Phi-4-mini-instruct.Q8_0 (4.5GB)
                  :8082 Llama-3.2-3B-Instruct.Q8_0 (3.6GB)
  MODE=batch   → :8081 phi-4-Q4_K_M (8.3GB, 14B-class review)
  MODE=code    → :8081 Qwen2.5-Coder-32B-Instruct-IQ4_XS (16.5GB)
                  mlock=1 + CAP_IPC_LOCK prevents swap thrashing
                  All other LLM containers + review timers stopped to free 24GB RAM
                  swap_mode.sh: stops swap-normal/batch, review-worker, nightly timers

Podman A — Qwen3-4B Debate Analyst (:8080, 6G MemoryMax):
  ExecStartPre blocks startup during MODE=code (32B needs all available RAM)
  Used by review_worker.py Phase 1 (extract) when available

Swap schedule (swap_mode.sh + systemd timers):
  Daytime: Podman B stays in normal mode (Phi-4-mini + Llama-3.2-3B)
  Nightly pipeline: swap-batch.timer → MODE=batch for review, MODE=code for 32B tasks
  After pipeline completes: swap-normal.timer → returns to normal mode

Active timers (verified 2026-05-22):
  devforge-collect.timer  — 15min — turn collection from all AI sources
  review-worker.timer     — aligned with nightly pipeline windows
  swap-batch.timer        — switches Podman B to batch/code mode for nightly pipeline
  swap-normal.timer       — returns Podman B to normal after pipeline completes
  devforge-nightly.timer  — 18:01 UTC (link turns ↔ worklog)
  devforge-backup.timer   — 20:00 UTC (DB snapshots)

Key files:
  session_guard.py    — git log scanning, commit recording
  review_worker.py    — Phase 1 (Podman A :8080 extract) + Phase 2 (Podman B :8081 verify)
  test_code_mod.py    — 4-stage pipeline (Podman B :8081, MODE=code, Qwen32B)
  swap_mode.sh        — mode switching (normal/batch/code), stops/restarts timers

Agent ecosystem (AGENT_MAP SSOT at scripts/lib/agents.py):
  claude-code — Anthropic API (DeepSeek v4-pro via api.deepseek.com)
  copilot     — GitHub Copilot CLI (local Node.js)
  gemini      — Google Gemini (free tier, key rotation via gemini_proxy.py)
  qwen        — Qwen Code CLI (local)
  aider       — Aider CLI (DeepSeek v4-flash via api.deepseek.com)
  All agent turns collected by devforge-collect.timer → turns table → review_worker.py
  Activity events (commits, stages, reviews) are system-detected, NOT agent-reported — eliminates the "agent forgot to record" bottleneck.
```

---

## 1. Problem Statement

### 1.1 Three separate recording systems, same fundamental pattern

```
worklog_entries     ← Agent manual recording (unreliable, often missed)
                    ← v1.1 auto git-commit recording (works)

pipeline_traces     ← NOT IMPLEMENTED. Planned as file-based pipe-delimited log.
                    ← test_code_mod.py already captures per-stage JSON

observations        ← In schema.sql but NOT IMPLEMENTED in code.
```

All three are the same thing: **an event happened, record what/when/who/result**.

### 1.1a Agent ecosystem — current vs target

**Current**: 5 AI agents operate on the server (claude-code, copilot, gemini, qwen, aider). Each agent is supposed to self-report completed work via `cli.py worklog add` or MCP `mem_save`. In practice, agents often miss this step — worklog coverage is inconsistent.

**Target**: Agents are **removed from the recording loop entirely**. The system detects events independently of agent cooperation:

```
Agent does work (any agent)     System detects         activity_log records
─────────────────────────────   ────────────────────   ─────────────────────
git commit (any source)    →    session_guard.py   →   type=commit, agent=...
pipeline stage complete    →    code_mod_pipeline.py →  type=stage
review run complete        →    review_worker.py   →   type=review
all raw events             →    summarizer.py      →   type=summary (daily)
human wants to record      →    cli.py activity add →  type=manual
```

The `agent` column in activity_log is for **attribution** (who did the work), not **recording** (who wrote the log). Agents never touch activity_log directly. System timers + local LLMs handle everything.

### 1.2 The B-Plan 7-module design is over-engineered

| B-Plan module | Already exists in |
|---------------|-------------------|
| worklog_config.py | `lib/db.py` + secrets.env pattern |
| git_scanner.py | `session_guard.py` already scans git log |
| llm_caller.py | `review_worker.py` call_llm() pattern |
| db_saver.py | `lib/db.py` psql() |
| lock_manager.py | systemd timer guarantees single execution |

### 1.3 Target: 3-stage async nightly relay

The nightly batch will run as a distributed async pipeline. Each stage is an independent process that reads the queue, processes pending items, and exits:

```
KST 23:00  Phi-4 (Podman B, normal mode)    → review existing code, emit findings
KST 23:35  Qwen14B (Podman B, normal mode)  → read Phi-4 findings, create draft plan
KST 00:00  Qwen32B (Podman B, swap mode)    → read plan, generate code diff
KST 07:30  activity_summarizer.py            → read ALL events, generate daily summary
KST 07:31  slack_operator.py                 → push summary to Slack
```

All stages share the same `activity_log` table as their communication queue. Each stage reads rows with `queue_status='unprocessed'` from the previous stage and writes its own rows.

---

## 2. Proposed Architecture

### 2.1 Single unified table: `activity_log`

```sql
CREATE TABLE IF NOT EXISTS activity_log (
    -- Primary
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Classification
    type            TEXT NOT NULL,  -- 'commit' | 'stage' | 'review' | 'summary' | 'manual'
    source          TEXT NOT NULL,  -- 'git' | 'pipeline' | 'review_worker' | 'cli'

    -- Human-readable
    title           TEXT NOT NULL,           -- One-line summary
    summary         TEXT NOT NULL DEFAULT '', -- Detail (LLM-generated or auto-filled)

    -- Attribution
    agent           TEXT,           -- AI tool name (nullable)
    model           TEXT,           -- Model name used (nullable)

    -- Flexible data
    body            JSONB DEFAULT '{}',

    -- Search / grouping
    tags            TEXT[] DEFAULT '{}',

    -- Linkage
    git_commit_hash TEXT,           -- NULL for non-commit events
    run_id          TEXT,           -- Shared across all stages of a nightly batch
    trace_id        TEXT,           -- Unique chain identifier for forensic tracking
    parent_id       BIGINT,         -- Self-referencing FK: which activity_log row triggered this one
    turn_ids        UUID[] DEFAULT '{}',

    -- Dual status fields (separate concerns)
    summary_status  TEXT NOT NULL DEFAULT 'raw',  -- 'raw' | 'summarized' | 'parse_failed'
    queue_status    TEXT NOT NULL DEFAULT 'unprocessed', -- 'unprocessed' | 'consumed'
    exec_status     TEXT NOT NULL DEFAULT 'DONE'  -- 'INIT' | 'RUN' | 'DONE' | 'FAIL'
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_type ON activity_log(type);
CREATE INDEX IF NOT EXISTS idx_activity_source ON activity_log(source);
CREATE INDEX IF NOT EXISTS idx_activity_tags ON activity_log USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_activity_body_gin ON activity_log USING GIN(body);
CREATE INDEX IF NOT EXISTS idx_activity_commit ON activity_log(git_commit_hash)
    WHERE git_commit_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_activity_run ON activity_log(run_id)
    WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_activity_trace ON activity_log(trace_id)
    WHERE trace_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_activity_parent ON activity_log(parent_id)
    WHERE parent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_activity_queue ON activity_log(queue_status, created_at)
    WHERE queue_status = 'unprocessed';

-- Unique constraints
CREATE UNIQUE INDEX IF NOT EXISTS idx_activity_commit_unique
    ON activity_log(git_commit_hash)
    WHERE git_commit_hash IS NOT NULL AND type = 'commit';
CREATE UNIQUE INDEX IF NOT EXISTS idx_activity_stage_unique
    ON activity_log(run_id, type, parent_id)
    WHERE run_id IS NOT NULL AND type = 'stage' AND exec_status = 'DONE';
-- exec_status = 'DONE' guard: allows retry after crash.
-- If a stage INSERTs then crashes before marking DONE, the retry
-- won't hit Unique Violation because the original row has exec_status='RUN' (or 'FAIL').
```

### 2.2 type enum semantics

| type | Meaning | Example title | source | queue_status usage |
|------|---------|---------------|--------|--------------------|
| `commit` | Git commit detected | "fix: add mlock to devforge-swap" | git | N/A (informational) |
| `review` | Phi-4 code review finding | "review: slack_operator.py has dedup opportunity" | review_worker | `unprocessed` → 14B consumes → `consumed` |
| `stage` | Pipeline stage output | "plan: extract _encode_payload helper" | pipeline | `unprocessed` → 32B consumes → `consumed` |
| `summary` | LLM daily summary | "2026-05-22: 8 commits, 4 stages, 3 reviews" | cli | N/A (final output) |
| `manual` | Human-entered worklog | "Slack integration debugged and deployed" | cli | N/A (informational) |

### 2.3 Event flow — Async 3-stage nightly relay + summarization

```
╔══════════════════════════════════════════════════════════════════════╗
║                         NIGHTLY BATCH (KST)                         ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║  ⏰ 23:00  STAGE 1: Phi-4 (code review)                              ║
║  ┌────────────────────────────────────────────────────────┐          ║
║  │ 1. Generate run_id = "RUN_20260522"                     │          ║
║  │ 2. Scan target files for issues                         │          ║
║  │ 3. For each finding:                                    │          ║
║  │    INSERT activity_log                                  │          ║
║  │      type='review', run_id=..., trace_id=UUID,          │          ║
║  │      queue_status='unprocessed', exec_status='DONE',    │          ║
║  │      summary=<finding description>,                     │          ║
║  │      body={full finding details}                        │          ║
║  │ 4. Exit 0 (container stays for next stage)              │          ║
║  └────────────────────────────────────────────────────────┘          ║
║                              │                                       ║
║                              │ queue_status = 'unprocessed'          ║
║                              ▼                                       ║
║  ⏰ 23:35  STAGE 2: Qwen14B (draft plan)                             ║
║  ┌────────────────────────────────────────────────────────┐          ║
║  │ 1. SELECT * FROM activity_log                          │          ║
║  │    WHERE type='review' AND queue_status='unprocessed'  │          ║
║  │    AND run_id = $RUN_ID                                │          ║
║  │ 2. For each unprocessed review:                         │          ║
║  │    a. Read body for finding details                     │          ║
║  │    b. Generate implementation plan via LLM              │          ║
║  │    c. INSERT activity_log                              │          ║
║  │       type='stage', run_id=..., parent_id=<review.id>, │          ║
║  │       queue_status='unprocessed',                       │          ║
║  │       summary=<plan summary>, body={full plan}          │          ║
║  │    d. UPDATE source review SET queue_status='consumed' │          ║
║  │ 3. Exit 0                                              │          ║
║  └────────────────────────────────────────────────────────┘          ║
║                              │                                       ║
║                              │ queue_status = 'unprocessed'          ║
║                              ▼                                       ║
║  ⏰ 00:00  STAGE 3: Qwen32B (code generation)                        ║
║  ┌────────────────────────────────────────────────────────┐          ║
║  │ 1. swap_mode.sh code (switch to 32B mode)              │          ║
║  │ 2. SELECT * FROM activity_log                          │          ║
║  │    WHERE type='stage' AND queue_status='unprocessed'   │          ║
║  │    AND run_id = $RUN_ID                                │          ║
║  │ 3. For each unprocessed plan:                           │          ║
║  │    a. Read body for plan details                        │          ║
║  │    b. Generate code diff via 32B LLM                    │          ║
║  │    c. INSERT activity_log                              │          ║
║  │       type='stage', run_id=..., parent_id=<plan.id>,   │          ║
║  │       queue_status='consumed',  -- final output, no     │          ║
║  │       downstream consumer                               │          ║
║  │       summary=<diff summary>, body={full diff}          │          ║
║  │    d. UPDATE source plan SET queue_status='consumed'   │          ║
║  │ 4. swap_mode.sh normal (return to normal mode)         │          ║
║  │ 5. Exit 0                                              │          ║
║  └────────────────────────────────────────────────────────┘          ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝
                              │
                              │ All events recorded (summary_status='raw')
                              ▼
╔══════════════════════════════════════════════════════════════════════╗
║  ⏰ 07:30  SUMMARIZATION: activity_summarizer.py                     ║
╠══════════════════════════════════════════════════════════════════════╣
║  ┌────────────────────────────────────────────────────────┐          ║
║  │ 1. SELECT id, type, source, title, summary, body,      │          ║
║  │        agent, model, created_at, tags                   │          ║
║  │    FROM activity_log                                   │          ║
║  │    WHERE summary_status = 'raw'                         │          ║
║  │    AND created_at > NOW() - INTERVAL '24 hours'        │          ║
║  │    AND created_at < NOW() - INTERVAL '5 minutes'       │          ║
║  │    ORDER BY created_at ASC                             │          ║
║  │                                                        │          ║
║  │    NOTE: body IS included in SELECT (not just title).  │          ║
║  │    This prevents data starvation — the LLM sees the    │          ║
║  │    full content of each event before summarizing.      │          ║
║  │                                                        │          ║
║  │ 2. Build prompt with title + summary + body for each   │          ║
║  │    event. Group by run_id.                              │          ║
║  │                                                        │          ║
║  │ 3. Call llama.cpp on Podman B (:8081, always running)  │          ║
║  │    Model: phi-4-mini or phi-4 (depends on current mode) │          ║
║  │    Temp: 0.0, Max tokens: 1024                          │          ║
║  │                                                        │          ║
║  │ 4. Parse JSON. On failure: mark source rows             │          ║
║  │    summary_status='parse_failed', exit 0 (no retry loop)│         ║
║  │                                                        │          ║
║  │ 5. INSERT summary row (type='summary', source='cli')   │          ║
║  │ 6. UPDATE source rows SET summary_status='summarized'  │          ║
║  └────────────────────────────────────────────────────────┘          ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝
                              │
                              ▼
╔══════════════════════════════════════════════════════════════════════╗
║  ⏰ 07:31  NOTIFICATION: slack_operator.py                           ║
╠══════════════════════════════════════════════════════════════════════╣
║  ┌────────────────────────────────────────────────────────┐          ║
║  │ /devforge result command:                               │          ║
║  │   SELECT * FROM activity_log                           │          ║
║  │   WHERE run_id = today's run                           │          ║
║  │   ORDER BY created_at                                  │          ║
║  │   → Format as Slack Block Kit message                  │          ║
║  │   → Post to configured DM channel                      │          ║
║  └────────────────────────────────────────────────────────┘          ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝
```

### 2.4 Parent-child lineage (DAG structure)

The `parent_id` column enables machines to trace causality:

```
activity_log row (id=1)          Phi-4 finding
  type='review'
  run_id='RUN_20260522'
  trace_id='trace_abc'
  parent_id=NULL                 ← root node
  queue_status='unprocessed'

        ↓ consumed by Qwen14B

activity_log row (id=5)          Qwen14B plan
  type='stage'
  run_id='RUN_20260522'
  trace_id='trace_abc'           ← same trace
  parent_id=1                    ← points to Phi-4 finding
  queue_status='unprocessed'

        ↓ consumed by Qwen32B

activity_log row (id=9)          Qwen32B code diff
  type='stage'
  run_id='RUN_20260522'
  trace_id='trace_abc'           ← same trace
  parent_id=5                    ← points to Qwen14B plan
  queue_status='unprocessed'
```

This forms a Directed Acyclic Graph (DAG). Any machine can query the full chain in one CTE:

```sql
WITH RECURSIVE chain AS (
    SELECT *, 0 AS depth FROM activity_log WHERE id = 9  -- start from result
    UNION ALL
    SELECT a.*, c.depth + 1
    FROM activity_log a
    JOIN chain c ON a.id = c.parent_id
)
SELECT * FROM chain ORDER BY depth DESC;
```

---

## 3. Implementation Plan

### 3.1 Files to modify

| # | File | Action | Lines | Description |
|---|------|--------|-------|-------------|
| 1 | `docs/schema.sql` | Append | +60 | Add activity_log DDL (Section 2.1) |
| 2 | `api/db.py` | Append | +60 | Add activity_log to SCHEMA_SQL |
| 3 | `scripts/session_guard.py` | Edit | ~10 changed | INSERT target: worklog_entries → activity_log (dual-write during migration) |
| 4 | `scripts/code_mod_pipeline.py` | Rename + Edit | ~30 added | `_insert_activity_stage()` with meaningful summary + body. Renamed from test_code_mod.py per code-as-documentation principle. |
| 5 | `scripts/review_worker.py` | Edit | ~50 added | Phase 1: INSERT type='review' with queue_status='unprocessed'. Phase 2 (NEW): SELECT unprocessed reviews, generate plans via Qwen14B, INSERT type='stage' with parent_id, UPDATE reviews to 'consumed'. This is Stage 2 of the nightly relay. |
| 5b | `scripts/code_mod_pipeline.py` | Edit | — | Stage 3: reads Stage 2 plans (type='stage', queue_status='unprocessed'), generates code diffs. INSERTS with queue_status='consumed' (final output). |

### 3.2 Files to create

| # | File | Lines | Description |
|---|------|-------|-------------|
| 6 | `scripts/activity_summarizer.py` | ~250 | Daily LLM summarization at KST 07:30 |
| 7 | `~/.config/containers/systemd/activity-summarizer.timer` | ~15 | systemd timer (KST 07:30 = 22:30 UTC previous day) |

### 3.3 Files to extend

| # | File | Lines | Description |
|---|------|-------|-------------|
| 8 | `scripts/cli.py` | ~80 added | `activity` subcommand group (recent, search, stats, add, summarize) |

### 3.4 What gets REMOVED from scope (vs old B-Plan)

```
REMOVED: worklog_config.py     → lib/db.py + secrets.env
REMOVED: git_scanner.py        → session_guard.py already does this
REMOVED: llm_caller.py         → review_worker.py call_llm() pattern
REMOVED: db_saver.py           → lib/db.py psql()
REMOVED: lock_manager.py       → systemd timer guarantees single execution
REMOVED: exit codes 2-5        → 0/1 only (systemd handles retry)
```

### 3.5 Code-as-documentation — Preventing doc/code mismatch

The 2026-05-18 `code-as-documentation-review.md` (PEP 8 + Django + Celery + Flask + Python stdlib analysis) established 4 operational rules for DevForge. These rules are the mechanism that prevents the plan document from drifting out of sync with reality:

#### Rule 1: Naming is documentation (PEP 8)
> 함수명/파일명이 곧 문서. `db.psql()`은 설명이 필요 없음. 주석은 **why**만, **what**은 이름으로.

**Applied here**: `test_code_mod.py` (770 lines) is misnamed. Despite `test_` prefix, it is a production 4-stage pipeline (ANALYZE → PLAN → IMPL → PACKAGE) generating real code diffs via Qwen32B. The `test_` prefix is a historical artifact — it now runs as part of the nightly pipeline and INSERTs directly into activity_log.

| Current name | New name | Reason |
|-------------|----------|--------|
| `test_code_mod.py` | `code_mod_pipeline.py` | Production pipeline, not a test. Name documents actual role. |

All downstream references (systemd units, swap_mode.sh, import paths, this document) updated accordingly.

#### Rule 2: Code is the SSOT, not the plan document
> PLAN.md Section 7은 `scripts/` 아래 3개 파일만 상정하지만, 실제로는 `lib/` 디렉토리가 6개 모듈로 이미 운영 중. 문서보다 코드가 현실.

**Applied here**: This document describes **intent and architecture**. The actual implementation is the authoritative source. When code and document disagree, the code is correct. This document must be updated within the same session as any implementation that changes the architecture.

#### Rule 3: DRY — Single authoritative source per knowledge unit
> "Every piece of knowledge must have a single, unambiguous, authoritative representation." — The Pragmatic Programmer

**Applied here**: `activity_log` replaces 3 separate recording systems (worklog_entries + pipeline_traces + observations). `lib/db.py` psql() replaces 10 divergent `_psql()` definitions. The summarizer is 1 file (~250 lines), not 7 B-Plan modules.

#### Rule 4: Import path documents origin
> `import lib.db` → `db.psql(...)` 호출. 호출 지점마다 출처가 명시됨 (Hitchhiker's Guide).

**Applied here**: All new modules (`activity_summarizer.py`, updated `review_worker.py`, `code_mod_pipeline.py`) use `from lib.db import psql, esc_sql` — never local `_psql()` redefinitions.

### 3.6 common-rule.md cleanup — Agent worklog recording removal

Once activity_log is deployed, agents are **removed from the recording loop** (Section 1.1a). The agent-facing rules must be updated to match:

**Remove from Session End Checklist:**
```
- [ ] Run `python3 /opt/projects/server/scripts/cli.py worklog add "summary"`  ← DELETE
+ [ ] Verify activity_log has captured today's events (check cli.py activity recent)  ← REPLACE
```

**Update File Modification Priority:**
```
- **Append only:** changelog.yaml (new entries), worklog DB  ← BEFORE
+ **Append only:** changelog.yaml (new entries)               ← AFTER
```

**Rationale**: Agents no longer call `cli.py worklog add`. The system timers (collect, review-worker, nightly, activity-summarizer) handle all recording. If an agent reads common-rule.md and sees "you MUST run worklog add", it creates confusion — the agent tries to do something the system already handles.

---

## 4. Detailed Module Specifications

### 4.1 `activity_summarizer.py` — Core new module

**Design constraints:**
- Single file, ~250 lines maximum
- Uses existing `lib/db.py` psql() + esc_sql() for all DB operations
- Uses `http.client` pattern (NOT requests library) per project standards
- Direct llama.cpp endpoint at http://127.0.0.1:8081/v1/chat/completions (Podman B, always running)
- No LiteLLM — removed 2026-05-19. Direct llama.cpp call, same pattern as review_worker.py
- Model: phi-4-mini (normal mode) or phi-4 (batch mode) — both sufficient for JSON summarization
- Podman B is always available regardless of mode (normal/batch/code)
- Timer: KST 07:30 = 22:30 UTC (after nightly 3-stage relay completes, before admin checks)
- llama.cpp MUST be started with `-c 32768` (context size). 30-60 rows with body diffs easily reach 15k-20k tokens. Default context (2048/4096) will silently truncate the prompt.
- sys.exit(0) on success, sys.exit(1) on transient error

**Algorithm:**

```
1. QUERY raw events (INCLUDING body field — critical for data quality):
   SELECT id, type, source, title, summary, body,
          agent, model, created_at, run_id, tags
   FROM activity_log
   WHERE summary_status = 'raw'
     AND created_at < NOW() - INTERVAL '5 minutes'   -- avoid race with running jobs
   ORDER BY created_at ASC

2. If no raw events: exit 0 (nothing to summarize)

3. BUILD LLM PROMPT:
   System: "You are a work log summarizer. Output ONLY valid JSON array.
            Group events by run_id first. Within each day, group related
            events logically. Each entry: {date, title, summary, tags}.
            Ensure every day with events has at least one summary entry."
   User:   For each event, include: title, summary, body (truncated if >500 chars),
           type, source, agent, model.

4. CALL LLM:
   POST http://127.0.0.1:8081/v1/chat/completions
   Model: phi-4-mini (or phi-4 in batch window)
   Temperature: 0.0
   Max tokens: 1024
   Timeout: 600s

5. PARSE JSON response:
   - SUCCESS: For each summary entry -> INSERT type='summary'
   - FAILURE: Mark source rows summary_status='parse_failed', exit 0
     (On next run, 'parse_failed' rows are retried with shorter prompt:
      title-only extraction, no full body.)

6. INSERT summary rows:
   INSERT INTO activity_log (type, source, title, summary, tags, summary_status)
   VALUES ('summary', 'cli', <title>, <summary>, <tags>, 'done')

7. UPDATE activity_log SET summary_status = 'summarized'
   WHERE id IN (<raw_event_ids>) AND summary_status = 'raw'
```

**Error handling:**
- JSON parse failure -> SET summary_status='parse_failed', exit 0 (no infinite retry)
- On next run for parse_failed rows: use shorter prompt, extract title only
- LLM timeout (600s) -> exit 1 (systemd retry next cycle)
- DB connection failure -> exit 1

**No new dependencies.** Only imports: `json`, `os`, `sys`, `http.client`, `datetime`, `pathlib`, `lib.db.psql`, `lib.db.esc_sql`.

**CRITICAL — Parameterized queries**: `lib/db.py` MUST use parameterized queries (e.g., `psycopg2 cursor.execute(sql, params)`) for all INSERT operations involving LLM-generated content. Qwen32B-generated code diffs contain single quotes, double quotes, backslashes, and dollar signs that will escape past any lightweight `esc_sql()` function. Inline string interpolation with `esc_sql()` is NOT sufficient for JSONB body content. See Section 9 Risk Assessment for rationale.

### 4.2 `session_guard.py` changes

```python
# NEW: dual-write during migration
sql_activity = f"""INSERT INTO activity_log (type, source, title, summary,
    git_commit_hash, agent, summary_status)
VALUES ('commit', 'git', '{title}', '{summary}', '{sha}', '{agent}', 'raw')
ON CONFLICT (git_commit_hash) WHERE git_commit_hash IS NOT NULL AND type = 'commit'
DO NOTHING RETURNING id"""

sql_worklog = f"""INSERT INTO worklog_entries (date, title, git_commit_hash,
    summary, agent, status, kind)
VALUES ('{today}', '{title}', '{sha}', '{summary}', '{agent}', 'done', 'task')
ON CONFLICT (date, git_commit_hash) DO NOTHING RETURNING id"""
```

### 4.3 `code_mod_pipeline.py` changes (renamed from test_code_mod.py)

```python
def _insert_activity_stage(run_id: str, task: dict, stage_num: int,
                           stage_name: str, result: dict,
                           parent_id: int = None,
                           trace_id: str = None) -> bool:
    """Insert pipeline stage result into activity_log.
    CRITICAL: Writes meaningful summary text — NOT just title.
    The summarizer reads both title AND summary fields."""
    from lib.db import psql, esc_sql

    tokens = result.get('tokens', {})
    body = result.get('body', {})
    elapsed = result.get('elapsed_s', 0)

    # Title: one-line identifier
    title = f"Task {task['id']} Stage {stage_num} {stage_name} — "
    title += f"{tokens.get('completion', 0)} tokens, {elapsed:.0f}s"

    # Summary: meaningful description for downstream LLM consumption
    if stage_name == 'ANALYZE' and isinstance(body, dict):
        summary = f"Analysis: {body.get('change_type', 'unknown')} change — "
        summary += body.get('notes', '')[:200]
    elif stage_name == 'PLAN' and isinstance(body, dict):
        summary = f"Plan: {body.get('approach', 'unknown')} — "
        summary += f"{len(body.get('steps', []))} steps, "
        summary += f"{body.get('estimated_lines_changed', {}).get('added', 0)}+/"
        summary += f"{body.get('estimated_lines_changed', {}).get('removed', 0)}- lines"
    elif stage_name == 'IMPL' and isinstance(body, dict):
        diff_text = body.get('text', '')
        summary = f"Impl: {len(diff_text)} chars diff generated"
    elif stage_name == 'PACKAGE' and isinstance(body, dict):
        pkg = body
        summary = f"Package: confidence={pkg.get('confidence', 'N/A')}"
    else:
        summary = f"{stage_name} completed in {elapsed:.0f}s"

    body_json = json.dumps(result, ensure_ascii=False)
    parent_sql = str(parent_id) if parent_id else 'NULL'
    trace_sql = f"'{esc_sql(trace_id)}'" if trace_id else 'NULL'

    sql = f"""INSERT INTO activity_log (type, source, title, summary, body,
        run_id, parent_id, trace_id, summary_status, queue_status, exec_status)
    VALUES ('stage', 'pipeline', '{esc_sql(title)}', '{esc_sql(summary)}',
            '{esc_sql(body_json)}', '{esc_sql(run_id)}',
            {parent_sql}, {trace_sql},
            'raw', 'consumed', 'DONE')"""
    return psql(sql, timeout=10) is not None
```

### 4.4 `review_worker.py` changes — async relay awareness

```python
def _insert_activity_review(run_id: str, trace_id: str,
                             findings: list, model: str) -> int:
    """Insert review findings into activity_log for downstream consumption.
    Each finding is a separate row so the 14B stage can consume individually.
    Returns number of rows inserted."""
    from lib.db import psql, esc_sql
    count = 0
    for finding in findings:
        title = f"review: {finding.get('file', 'unknown')} — {finding.get('issue', '')[:80]}"
        summary = finding.get('description', '')[:500]
        body = json.dumps(finding, ensure_ascii=False)

        sql = f"""INSERT INTO activity_log (type, source, title, summary, body,
            run_id, trace_id, model, summary_status, queue_status, exec_status)
        VALUES ('review', 'review_worker', '{esc_sql(title)}',
                '{esc_sql(summary)}', '{esc_sql(body)}',
                '{esc_sql(run_id)}', '{esc_sql(trace_id)}', '{esc_sql(model)}',
                'raw', 'unprocessed', 'DONE')"""
        if psql(sql, timeout=10):
            count += 1
    return count
```

---

## 5. Migration Strategy

### Phase A: Deploy activity_log (immediate — 1 hour)

1. Run DDL to create `activity_log` table
2. Modify `session_guard.py` -> dual-write to both worklog_entries + activity_log
3. Rename + modify `code_mod_pipeline.py` (was test_code_mod.py) -> INSERT stage events with summary + body
4. Modify `review_worker.py` -> INSERT review events with queue_status
5. Deploy `activity_summarizer.py` + systemd timer (KST 07:30)

**Both tables receive data during this phase. No data loss.**

### Phase B: Parallel run (1 week)

1. Monitor activity_log insertion rate
2. Verify summarizer output quality (spot-check summaries vs raw body)
3. Backfill historical worklog_entries -> activity_log:
   ```sql
   INSERT INTO activity_log (created_at, type, source, title, summary,
                              agent, model, tags, git_commit_hash, turn_ids,
                              summary_status, queue_status, exec_status)
   SELECT created_at, 'manual', 'cli', title, summary, agent, model, tags,
          git_commit_hash, turn_ids, 'summarized', 'consumed', 'DONE'
   FROM worklog_entries
   ON CONFLICT DO NOTHING;
   ```

### Phase C: Cutover (after 1 week stability)

1. `cli.py worklog` -> alias to `cli.py activity`
2. Stop inserting to worklog_entries from session_guard.py
3. `worklog_entries` kept as read-only archive (no deletion)

---

## 6. Server Resource Impact

### 6.1 Storage

- Each activity_log row: ~600 bytes average (with body JSONB)
- Estimated: 30-60 rows/day (commits + stages + reviews + 1-2 summaries)
- ~18-36 KB/day, ~6.5-13 MB/year
- Negligible compared to turns table

### 6.2 CPU/Memory

- `activity_summarizer.py`: once daily, 3-5 minute LLM call
- Uses Podman B :8081 (phi-4-mini or phi-4, always running, direct llama.cpp)
- Prompt: ~3000 tokens input -> ~300 tokens output
- Memory impact: zero (uses existing LLM container)
- CPU impact: minimal (~5 min/day)

### 6.3 No new containers

All infrastructure already running:
- Podman B :8081 (phi-4-mini / phi-4) — always running, used by summarizer
- PostgreSQL (:5432) — data-pod

---

## 7. Success Criteria

| Metric | Target | Measurement |
|--------|--------|-------------|
| Commit recording rate | 100% | `SELECT COUNT(*) FROM activity_log WHERE type='commit' AND DATE(created_at)=CURRENT_DATE` |
| Stage recording rate | 100% | 4 rows per pipeline task execution |
| Stage summary quality | Non-empty | No stage rows with `summary=''` |
| Summarizer success rate | >95% | `summary_status != 'parse_failed'` ratio |
| Summarizer data completeness | 100% | Summarizer SELECT includes `body` field |
| Agent dependency | Zero | No agent API calls needed for recording |
| Queue processing | 100% | No rows left with `queue_status='unprocessed'` >24h |

---

## 8. Open Questions — Resolved

All questions from v1.0 resolved by peer review consensus:

| Q | Question | Resolution | Reviewers |
|---|----------|------------|-----------|
| Q1 | Which model for summarizer | **Podman B :8081** — phi-4-mini (normal) or phi-4 (batch window). Always available regardless of mode. Direct llama.cpp, no LiteLLM. | All 4 agreed — use always-available model. Podman A (:8080) excluded: intermittent availability (may be stopped; blocked during code mode). |
| Q2 | Grouping: by day vs by theme | **LLM decides with date boundary rule** — group by run_id first, ensure each day has >=1 entry | R1, R2, R4 agreed |
| Q3 | Absorb observations table? | **Keep separate** — different data shape (time-series metrics vs events) | All 4 agreed |
| Q4 | review_facts: run-level or per-fact? | **Run-level summary only** — facts stay in review_facts, summary in activity_log | All 4 agreed |
| Q5 | Exit codes: 0/1 or 0-5? | **0/1 only** — systemd handles retry, journald logs error details | All 4 agreed |

---

## 9. Risk Assessment

| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| LLM summarization hallucinates | Low (temp=0.0, structured prompt) | Medium | Raw events preserved in body JSONB; summary is derived |
| Data starvation (body not in SELECT) | **Eliminated** | — | v1.1 explicitly includes body in summarizer SELECT |
| Summarizer infinite retry loop | **Eliminated** | — | Parse failure -> `summary_status='parse_failed'` + exit 0 |
| Race: summarizer reads in-progress event | **Eliminated** | — | `created_at < NOW() - INTERVAL '5 minutes'` guard |
| Duplicate commit records | None | — | UNIQUE constraint |
| Duplicate stage records | None | — | UNIQUE constraint on (run_id, type, parent_id) with `exec_status='DONE'` guard — allows retry after crash |
| Queue stuck (unprocessed >24h) | Low | Medium | Monitoring query alerts if unprocessed rows >24h old |
| Migration breaks existing queries | Low | Low | Both tables exist during Phase B; cutover is controlled |
| Orphan raw rows (24h timeout) | **Eliminated** | — | v1.3 removed `created_at > NOW() - INTERVAL '24 hours'` from summarizer SELECT. All `summary_status='raw'` rows are processed regardless of age |
| SQL injection via LLM-generated diffs | **Eliminated** | — | v1.3 mandates parameterized queries in `lib/db.py`. Inline `esc_sql()` cannot handle arbitrary code diff special characters |
| Unique Violation on pipeline retry | **Eliminated** | — | v1.3 added `exec_status='DONE'` to unique index WHERE clause. Crashed stages leave `exec_status='RUN'` or `'FAIL'`, so retry INSERT does not collide |
| Context window truncation (prompt clipping) | **Eliminated** | — | v1.3 mandates `-c 32768` in llama.cpp args for summarizer model. 30-60 rows with body diffs reach 15k-20k tokens |

---

## 10. Peer Review History

| Review # | Date | Verdict | Key contributions |
|----------|------|---------|-------------------|
| 1 | 2026-05-23 | Approve with fixes | Race condition guard, parse_failed status, Q1/Q2 support |
| 2 | 2026-05-23 | Approve | parent_id for M2M lineage, KST 07:30 timer, Slack integration |
| 3 | 2026-05-23 | Approve with major fixes | Data starvation bug (body not in SELECT), queue_status field, run_id pipeline breakage, async relay redesign |
| 4 | 2026-05-23 | Approve with minor revisions | Additional indexes, sanitize function, monitoring queries, Q1-Q5 answers |

All feedback incorporated in v1.1. v1.2 added: code-as-documentation 4 rules + file rename (Section 3.5), common-rule.md agent worklog removal (Section 3.6). v1.3 added: 4 runtime defect fixes from microscopic audit — orphan raw row prevention, unique index exec_status guard, parameterized query mandate, context window -c 32768 requirement. Stage 2 execution owner clarified (review_worker.py Phase 2). Stage 3 queue_status corrected to 'consumed'.

---

*Generated: 2026-05-22 | Updated: 2026-05-22 (v1.3 — 4 runtime defects eliminated + Stage 2/3 fixes)*
*Machine-readable | For implementation*
