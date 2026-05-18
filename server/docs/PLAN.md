# DevForge — Unified Plan (Machine-Readable)

**Version**: 2.0
**Date**: 2026-05-18
**Status**: ACTIVE
**Language**: English (machine-readable). User-facing text remains Korean per common-rule.md.

---

## 1. Current System State

### 1.1 Infrastructure (verified 2026-05-18)

```
ai-pod (16GB)                    data-pod (2GB)
├─ litellm :4000                 └─ postgres :5432
├─ devforge-llm :8080
└─ devforge-api :8000

Caddy (host network)
├─ /api/* → litellm:4000
├─ /devforge/* → localhost:8000
└─ netdata → localhost:19999
```

- **Host**: OCI ARM (24GB RAM), Oracle Linux 9.7, 4-core
- **Runtime**: Podman rootless, Quadlet systemd
- **Storage**: /mnt/lv_db (30GB, PostgreSQL), /mnt/secure_meta (4.5GB, backups)
- **Network**: devforge-net bridge (10.89.0.0/24)

### 1.2 Timers (active)

| Timer | Interval | Purpose |
|-------|----------|---------|
| devforge-collect.timer | 15min | Collect turns from all AI sources |
| review-worker.timer | 30min (:05, :35) | 2-phase LLM review pipeline |
| devforge-nightly.timer | 18:01 UTC | Link turns ↔ worklog entries |
| devforge-backup.timer | 20:00 UTC | DB snapshots |
| devforge-restore-test.timer | Monthly | Backup integrity verification |

### 1.3 Models (verified working, llama.cpp b7834)

| Key | Model | Size | Arch | Prompt Eval | Generation | Role |
|-----|-------|------|------|-------------|------------|------|
| qwen14 | Qwen2.5-Coder-14B-Instruct Q8_0 | 15GB | qwen2 | 1.51 t/s | 1.82 t/s | Extract (current default) |
| phi4 | Phi-4 Q8_0 | 15GB | phi3 | 4.80 t/s | 1.73 t/s | Verify |
| deepseek_r1 | DeepSeek-R1-Distill-Qwen-14B Q8_0 | 15GB | qwen2 | - | 1.76 t/s | Rejected (CoT overhead) |
| yi_coder_9b | Yi-Coder-9B-Chat Q8_0 | 8.8GB | llama | 3.68 t/s | 1.37 t/s | Candidate extract |
| mistral_nemo_12b | Mistral-Nemo-Instruct-2407 Q8_0 | 13GB | llama | 6.75 t/s | 2.63 t/s | Candidate extract (testing) |

### 1.4 Database

- **review_facts**: 2-phase LLM review results (turn_id, fact_index, evidence, verdict, extract_model, verify_model)
- **worklog_entries**: Daily work summaries (missing git_commit_hash column — see Phase 1 below)
- **turns**: Individual conversation turns with agent attribution
- **obs_dec**: Decision observations keyed by turn_id

---

## 2. Active Projects (2026-05-18)

### 2.1 Review Worker Pipeline (OPERATIONAL)

`review_worker.py` — 2-phase LLM review:
- **Phase 1 (extract)**: Model extracts self-contained facts from conversation turns
- **Phase 2 (verify)**: Phi-4 cross-validates facts against original turn content
- **Scope**: 24h rolling window, checkpoint-based incremental processing
- **Status**: 8 production runs completed, timer enabled
- **Latest results (8th run, Qwen14B extract)**: 48 facts, 47 valid (97.9%), 1 hallucinated

### 2.2 Extraction Model Selection (IN PROGRESS)

Comparing Mistral-Nemo-12B vs Qwen14B as extraction model:
- **9th run** (Mistral extract): Running now — same 10 turns as Qwen14B 8th run
- **Criteria**: Fact extraction quality, hallucination rate, JSON compliance
- **Qwen14B known issues**: 14.3% hallucination rate (1st run), non-deterministic (temp=0.0), 1 JSON parse failure
- **Mistral advantages**: Faster (2.63 vs 1.82 t/s gen), better long-evidence handling
- **Mistral issues observed**: JSON escape errors (1/17 turns failed), slower prompt eval than Phi-4

### 2.3 External API Integration (PLANNED)

Unused API keys available:
- **Gemini** (via gemini_rotate.py → `~/.local/bin/gemini`): Already used for aider, not HTTP API
- **Exa**: Semantic search API — could enhance fact verification
- **Brave**: Web search API — external knowledge cross-reference
- **Travily**: Alternative search API
- **You.com**: AI search API

Opportunity: Use external search APIs to cross-validate facts that local models flag as uncertain.

---

## 3. Worklog Reconciliation — Simplified v1.1 → B-Plan

### 3.1 Objective

Automatically record git commits to worklog_entries so every code change is tracked in DB.

### 3.2 Current Gap

`worklog_entries` table has NO git commit tracking column. Current recording is manual via `cli.py worklog add`.

### 3.3 Phase 1: v1.1 (Immediate — this week)

**Goal**: Add commit tracking with minimal code changes.

**Changes required**:
1. **DB schema**: Add `git_commit_hash TEXT` column + UNIQUE constraint
   ```sql
   ALTER TABLE worklog_entries ADD COLUMN IF NOT EXISTS git_commit_hash TEXT;
   ALTER TABLE worklog_entries ADD UNIQUE (date, git_commit_hash);
   CREATE UNIQUE INDEX IF NOT EXISTS idx_wl_commit_hash
     ON worklog_entries(git_commit_hash)
     WHERE git_commit_hash IS NOT NULL;
   ```
2. **session_guard.py**: Add `log_commits_to_worklog()` function
   - Scan `git log --since=24 hours ago`
   - INSERT with ON CONFLICT DO NOTHING
   - Use `_esc_sql()` for proper escaping (ref: review_worker.py:165-167)
3. **Timer**: Uses existing `review-worker.timer` (no new services)

**Risk**: LOW — idempotent schema change, simple code addition, 1-hour implementation

### 3.4 Phase 2: B-Plan (After v1.1 stability — 2-4 weeks)

**Goal**: Full worklog reconciliation with LLM-enhanced summaries.

**Key design decisions**:
- **LLM gateway**: LiteLLM (localhost:4000) — NOT localhost:8000 (that's devforge-api)
- **Model**: Use existing review_worker pipeline models (reuse, no new model loading)
- **Lock file**: NOT needed — systemd timer guarantees single execution
- **Module count**: 1-2 files max (not 7) — follow review_worker.py pattern
- **Exit codes**: 0=success, 1=transient error (retry), 2=skip
- **Idempotency**: `git_commit_hash UNIQUE` — safe to run multiple times
- **JSON logging**: stdout → journald (already the pattern)

**Implementation**: Single `worklog_reconcile.py` (~300 lines) that:
1. Scans `git log` for new commits
2. Optionally calls LLM for summary (copy-paste only, no generation)
3. INSERTs to worklog_entries with ON CONFLICT

---

## 4. Model Strategy

### 4.1 Tiered Architecture

```
Tier 1 (Local, Fast):  Mistral-Nemo-12B or Qwen14B
  ├─ Review pipeline extraction
  ├─ Worklog commit summarization
  └─ Deterministic classification

Tier 2 (Local, Accurate):  Phi-4
  └─ Fact verification (cross-validation)

Tier 3 (External, Broad):  Gemini / Exa / Brave
  ├─ Web search for fact validation
  ├─ Semantic search for context
  └─ Knowledge gap filling
```

### 4.2 Model Selection Matrix (decision pending 9th run results)

| Criterion | Qwen14B | Mistral-Nemo-12B | Yi-Coder-9B |
|-----------|---------|------------------|-------------|
| Speed (gen t/s) | 1.82 | **2.63** | 1.37 |
| Hallucination rate | 8.3% (97.9% valid) | TBD | TBD |
| JSON compliance | 1 failure observed | 1 failure observed | Not tested |
| Memory footprint | 15GB | 13GB | **8.8GB** |
| Long evidence handling | Truncation risk | Good | Unknown |

**Recommendation pending 9th run completion**: If Mistral hallucination rate ≤ Qwen14B, switch to Mistral (faster + better evidence). If worse, keep Qwen14B and tune prompt.

---

## 5. Timeline

```
Week 1 (May 18-24):  v1.1 Deployment
  ├─ Mon: Complete Mistral comparison → decide extract model
  ├─ Mon-Tue: Deploy v1.1 (git_commit_hash column + session_guard.py)
  ├─ Wed-Fri: Monitor commit recording rate (target: 100%)
  └─ Sun: Week 1 review

Week 2 (May 25-31):  Stabilization
  ├─ Verify 100% commit recording for 7 consecutive days
  ├─ Fix any edge cases (quote escaping, merge commits)
  └─ Fri: v1.1 stability sign-off

Week 3-4 (Jun 1-14):  B-Plan Implementation
  ├─ Write worklog_reconcile.py (single file, ~300 lines)
  ├─ Deploy to staging → test → production cutover
  └─ Disable old timer, enable worklog-reconcile.timer

Ongoing:
  ├─ Review pipeline: 30min timer, continuous
  ├─ Prompt tuning: Target <5% hallucination rate
  ├─ External API: Phase 1 Gemini integration for fact cross-check
  └─ FuseO1-DeepSeekR1: Test code-specialized merge variant
```

---

## 6. Key Decisions

| ID | Date | Decision | Rationale |
|----|------|----------|-----------|
| D1 | 2026-05-18 | LiteLLM stays as unified gateway (NOT bypassed) | Single entry point for aider + all tools; consistent auth |
| D2 | 2026-05-18 | B-Plan: 1 file, not 7 | 1-person dev — review_worker.py pattern proven |
| D3 | 2026-05-18 | No separate Lock Manager | systemd timer already guarantees single execution |
| D4 | 2026-05-18 | Mistral as extract candidate | Fastest generation (2.63 t/s), handles long evidence better |
| D5 | 2026-05-18 | Phi-4 remains verifier | Highest accuracy, fast prompt eval (4.80 t/s), proven reliability |
| D6 | 2026-05-18 | v1.1 first, B-Plan later | Quick win (1h) → stability baseline → informed B-Plan implementation |

---

## 7. File Map

```
/opt/projects/server/
├── docs/
│   ├── PLAN.md                          ← THIS FILE (unified plan, English)
│   ├── design.md                        ← Architecture reference
│   ├── phases.md                        ← Phase tracker (Phase 1 done, Phase 2 planned)
│   ├── tasks.yaml                       ← Kanban task tracker
│   └── worklog-v1.1-to-b-plan-roadmap.md ← Previous roadmap (archive reference)
├── scripts/
│   ├── lib/
│   │   ├── db.py                        ← Shared DB helpers (psql, esc_sql)
│   │   ├── agents.py                    ← Agent name normalization (SSOT)
│   │   └── qwen_executor.py / parser_*.py
│   ├── review_worker.py                 ← 2-phase LLM review pipeline (OPERATIONAL)
│   ├── session_guard.py                 ← Auto-commit safety net + commit logging
│   └── worklog_reconcile.py            ← B-Plan implementation (TO BE CREATED)
├── handover.yaml                        ← Session handover decisions
└── CLAUDE.yaml                          ← Server harness, entry points
```

---

## 8. Next Actions (Priority Order)

1. **Complete Mistral 9th run** → compare hallucination rate with Qwen14B 8th run → decide EXTRACT_MODEL
2. **Update review_worker.py** with final extract model choice → re-enable timer
3. **Deploy v1.1**: Add git_commit_hash column + extend session_guard.py
4. **Monitor 1 week**: Verify commit recording rate ≥ 98%
5. **Implement B-Plan**: worklog_reconcile.py after v1.1 stability proven

---

*Generated: 2026-05-18T13:35:00Z | Machine-readable | Updates via PR to docs/PLAN.md*
