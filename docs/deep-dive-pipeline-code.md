# Deep Dive: Pipeline Code Analysis

> 2026-07-22 — All pipeline scripts (scripts/pipelines/) reviewed against current day_cycle.sh flow

## 1. Pipeline File Inventory (28 scripts)

### 1.1 Production Status (14 scripts)

| File | Lines | Path (callers) | Dependencies |
|------|-------|---------------|-------------|
| `extract.py` | ~1,090 | day_cycle.sh → subprocess | extract_llm.py, extract_verify.py, lib.pod_manager, lib.watchdog.checker |
| `extract_llm.py` | ~350 | extract.py — submodule | lib.llm_client, lib.model_registry, lib.pod_manager |
| `extract_verify.py` | ~400 | extract.py — submodule | lib.llm_client (call_llm, reranker_nli_verdict) |
| `embed_batch.py` | ~400 | day_cycle.sh → subprocess | lib.pod_manager, lib.infra.preflight |
| `fts5_refresh.py` | ~70 | day_cycle.sh → subprocess | lib.search.local_index |
| `reranker_recover.py` | ~160 | day_cycle.sh → subprocess | lib.llm_client |
| `text_clean.py` | ~340 | day_cycle.sh → subprocess | lib.text_cleaner |
| `review_consumer.py` | ~400 | day_cycle.sh → subprocess | lib.llm_client |
| `proxy_reviewer.py` | ~450 | night_cycle.sh → subprocess | lib.llm_client, lib.notify |
| `worklog_generator.py` | ~430 | day_cycle.sh → subprocess | lib.db, lib.notify |
| `prj_cycle.py` | ~150 | systemd timer | lib.db |
| `night_debate_pipeline.py` | ~200 | night_cycle.py | lib.pipeline_common |
| `review.py` | ~100 | n/a | lib.db |

### 1.2 Experimental Status (9 scripts)

| File | Lines | Path | Notes |
|------|-------|------|-------|
| `enrich.py` | ~1,200 | day_cycle.sh → subprocess | preflight → ensure_sequential_dual |
| `night_cycle.py` | ~400 | night_cycle.sh → subprocess | P-R-J debate pipeline |
| `night_cycle_pipeline.py` | ~100 | n/a | Experimental night pipeline |
| `day_verify.py` | ~640 | **NOT CALLED** | Dead code — no caller in day_cycle.sh |
| `entity_scan.py` | ~500 | day_cycle.sh → subprocess | Pattern-based entity extraction |
| `post_extract_supplement.py` | ~400 | day_cycle.sh → subprocess | Uses extract_llm |
| `raw_consumer.py` | ~110 | turn_watcher → subprocess | Polls for raw → pending |
| `review_facts_dedupe.py` | ~150 | n/a | Deduplication utility |

### 1.3 Deprecated Status (1 script)

| File | Lines | Notes |
|------|-------|-------|
| `polish_batch.py` | ~360 | Merged into text_clean.py. Should be archived. |

### 1.4 Key Finding: All Pipeline Scripts Are Already Python

**Every pipeline script is already Python.** Zero bash pipeline scripts. The only bash remaining
is orchestration (day_cycle.sh, night_cycle.sh) and library (model_ctl.sh).

---

## 2. Pipeline Data Flow (End-to-End)

```
turn_watcher (jsonl → DB, raw insert)
  → raw_consumer (raw → clean → pending)
  → day_cycle.sh orchestrates:
      ├─ text_clean.py        (pending → cleaned)
      ├─ fts5_refresh.py      (cleaned → scanned via FTS5)
      ├─ entity_scan.py       (scanned, deterministic entities)
      ├─ extract.py           (scanned → extracted+verified)
      │   ├─ _preflight_gate()
      │   ├─ _launch_reranker()
      │   ├─ Phase 1: extract (section-major KV cache batch)
      │   ├─ Phase 2: _llm_nli_verify (NLI self-verify v1)
      │   ├─ Phase 3: _llm_nli_verify2 (NLI self-verify v2)
      │   └─ Phase 4: _quality_check_facts + DB store
      ├─ reranker_recover.py  (re-score RERANKER_ERROR facts)
      ├─ post_extract_supplement.py (offline missing-fact LLM)
      ├─ enrich.py            (verified → enriched)
      │   ├─ preflight_checks()
      │   ├─ ensure_sequential_dual()
      │   ├─ ThreadPool dual model A/B round-robin
      │   ├─ TLDR NLI verify + entity grounding
      │   └─ DB store (review_facts + pipeline_state)
      └─ embed_batch.py       (enriched → embedded)
          ├─ orphan cleanup
          ├─ preflight_checks + ensure_model
          └─ dynamic batching
```

### 2.1 pipeline_state Transitions

```
raw ──[raw_consumer]──→ pending
pending ──[day_cycle: text_clean]──→ batching
batching ──[day_cycle: text_clean]──→ cleaned
cleaned ──[day_cycle: fts5+entity]──→ scanned
scanned ──[day_cycle: extract.py]──→ extracted+verified
extracted+verified ──[day_cycle: enrich.py]──→ enriched
enriched ──[day_cycle: embed_batch.py]──→ embedded
```

Note: `day_verify.py` formerly handled `extracted → verified` but this was merged into
`extract.py` Phase 2-3. The state `extracted+verified` is a single state.

---

## 3. extract.py Self-Sufficiency (Critical Finding)

### 3.1 What extract.py Handles Independently

| Function | Details |
|----------|---------|
| **Preflight gate** (`_preflight_gate()`) | Model file check, memory budget (4GB or 12GB), :8082 health + auto-restart, :8080 reranker + auto-launch, DB connectivity |
| **Model pod start** | `_ensure_model_pod("day-extractor", skip_if_healthy=True)` |
| **Reranker launch** (`_launch_reranker()`) | Same pattern as day_cycle.sh version |
| **NLI verify v1** | `_llm_nli_verify` in extract_verify.py |
| **NLI verify v2** | `_llm_nli_verify2` — second pass with corrected evidence |
| **Quality check** | `_quality_check_facts` — fact-level quality scoring |
| **8082 auto-recovery** | `_call_with_8082_retry` in extract_llm.py — connection error detection + recovery |
| **Column migration** | `ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS` for `nli_llm2`, `quality_checks` |

### 3.2 What day_cycle.sh Still Does Around extract.py

```
day_cycle.sh lines 274-316:
  ├── Budget gate (NEED_EXTRACT: pipeline_state='scanned')
  ├── echo "=== Day Extract ==="
  ├── python3 extract.py                        ← extract.py handles everything
  ├── Noise marker (UPDATE turns SET pipeline_state)
  ├── NEUTRAL auto-resolve (4-hour inactivity)
  ├── Reranker recovery: python3 reranker_recover.py
  └── Post-extract supplement: python3 post_extract_supplement.py
```

The bash layer around extract.py is **6 operations**, all of which are:
- Simple DB queries (COUNT, UPDATE)
- python3 subprocess calls
- curl to Slack

---

## 4. enrich.py Analysis

### 4.1 Key Architecture

| Aspect | Detail |
|--------|--------|
| Status | experimental |
| Lines | ~1,200 |
| Model start | `preflight_checks()` → `ensure_sequential_dual("day-enricher", "day-enricher-b")` |
| LLM strategy | ThreadPool with dual model A/B round-robin |
| Short turn optimization | < SHORT_TURN_THRESHOLD → single-token intent classification (no LLM) |
| Solo turns | > MAX_CHARS_SOLO (5000) → separate pool, longer timeout |
| TLDR NLI | `_verify_tldr` → CONTRADICTION → `_fix_contradiction_tldr` retry → source fallback |
| Entity grounding | `_verify_entities` — rejects hallucinated entities |
| DB storage | `_insert_enrich_fact` — JSONB enrich_data in review_facts |

### 4.2 Bash Independence

enrich.py is **fully independent** of bash:
- No subprocess calls
- All DB via lib.db
- All model management via lib.pod_manager
- No python3 -c inline needed

---

## 5. embed_batch.py Analysis

### 5.1 Key Architecture

| Aspect | Detail |
|--------|--------|
| Status | production |
| Lines | ~400 |
| Model start | `preflight_checks()` + `ensure_model("embeder", skip_if_healthy=True)` |
| Prerun cleanup | Orphaned embedding chunk removal (DELETE FROM embeddings WHERE turn missing) |
| Fallback | :8080 primary, :8081 fallback if inference unavailable |
| Dynamic batching | Token-budget-based per batch (SLOT_CTX ~3300 tok) |
| Modes | Turns (default), Facts (--facts), Feedback (--feedback) |

### 5.2 Bash Independence

Fully independent. Single call: `python3 embed_batch.py` (plus --facts, --feedback flags).

---

## 6. Dead Code Discovery

### 6.1 day_verify.py — No Longer Called

| Evidence | Detail |
|----------|--------|
| Status | `experimental` (was `production` before pipeline merge) |
| day_cycle.sh call | Removed — no `python3 day_verify.py` in current day_cycle.sh |
| Responsibility | Predicate NLI with Veritas-8B — now handled by extract.py `_llm_nli_verify` |
| _launch_reranker() | 3rd copy! Same pattern as day_cycle.sh and extract.py |
| Size | ~640 lines |
| **Recommendation** | Archive to `_archive/pipelines/day_verify.py` |

### 6.2 polish_batch.py — Deprecated

| Evidence | Detail |
|----------|--------|
| Status | `deprecated` |
| Merged into | text_clean.py (unified preprocessing) |
| Size | ~360 lines |
| **Recommendation** | Archive to `_archive/pipelines/polish_batch.py` |

### 6.3 _launch_reranker() — Triple Duplication

| Location | Lines | Status |
|----------|-------|--------|
| day_cycle.sh | 140-166 | ACTIVE (but redundant) |
| extract.py | 1050-1081 | ACTIVE (preferred) |
| day_verify.py | ~551 | DEAD (archived with day_verify.py) |

Since extract.py `_preflight_gate()` handles reranker launch, day_cycle.sh's version is
effectively dead code. However, the day_cycle.sh version runs *before* extract.py, so it
can still be triggered if the reranker isn't already running when day_cycle.sh reaches
the extract.py call. The `_preflight_gate()` in extract.py is the safety net.

---

## 7. Remaining Bash Analysis (Priority Re-ranking)

### 7.1 Bash Script Scoring (Updated with Deep Dive Findings)

```
Scoring: DB=*2, py3c=*3, curl=*1, funcs=*1, subprocess python3=*1

Script                 Lines  DB py3c curl func sub   Score  Priority
─────────────────────────────────────────────────────────────────────
day_cycle.sh            462   19   2   3    5    14    77    HIGH
night_cycle.sh          245    0   3   1    6     4    16    HIGH
model_ctl.sh            240    0   0   0   10     0    10    HIGH (lib)
run_baseline_monitor.sh  83    1   4   0    0     1    15    MED
weekly_enrich_rebuild.sh 57    0   2   0    0     2     8    LOW
system_sync.sh           42    0   0   1    0     2     3    EXCLUDED
auto_mode.sh            271    0   0   0    4     0     4    LOW
─────────────────────────────────────────────────────────────────────
```

Note: I added `sub` (subprocess call to python3 pipeline) as a scoring factor (=1 each)
since each call represents a bash reliability risk (timeout, error propagation) even though
the called script is already Python. day_cycle.sh has 14 subprocess calls.

### 7.2 Subprocess Safety Risk in day_cycle.sh

Every `python3 scripts/pipelines/xxx.py` in day_cycle.sh lacks timeout:
```bash
python3 "$PIPELINE_DIR/extract.py" 2>&1          # no timeout!
python3 "$PIPELINE_DIR/enrich.py" 2>&1            # no timeout!
python3 "$PIPELINE_DIR/embed_batch.py" 2>&1       # no timeout!
```

Bash relies on systemd `TimeoutSec` but systemd sends SIGTERM to the bash process, not to
the python3 subprocess directly. If python3 hangs, systemd kills bash but python3 becomes
orphaned. All pipeline scripts should handle their own timeouts (embed_batch already does
via `_liveness_heartbeat`, extract.py has internal timeouts via LLM client)

---

## 8. Recommendations

### 8.1 Immediate (Low Effort, High Impact)

1. **Archive dead code**: `day_verify.py` → `_archive/pipelines/`, `polish_batch.py` → `_archive/pipelines/`
2. **Document _launch_reranker() redundancy**: Day_cycle.sh version is backup, extract.py version is primary

### 8.2 Short-term (Phase 0)

3. **model_ctl.sh → Python** (1-2h): The only bash library remaining. All pipeline scripts
   depend on it. After migration, every pipeline becomes fully bash-independent.

### 8.3 Medium-term

4. **day_cycle.sh → Python** (deferred, ROI reduced): 77 score but 14/14 subprocess calls
   are to already-Python scripts. Primary value is:
   - Consistent timeout handling (no orphaned python3 processes)
   - DB query unification (psql_json instead of raw SQL)
   - Budget gate integration with pipeline_state

5. **run_baseline_monitor.sh → Python** (0.5-1h): Highest python3-c density (4/83L)

### 8.4 Not Recommended

6. **night_cycle.sh**: Keep bash — the model switching logic (mode transition, inference
   swap, EXIT trap) is inherently shell-dependent and clearly correct in bash
7. **system_sync.sh, entrypoints, open-newhand.sh**: No migration benefit

### 8.5 Key Risk: subprocess Timeout

The single most important issue is **missing subprocess timeouts**. If this warrants
migration, the priority is day_cycle.sh (14 unprotected subprocess calls). A minimal fix
without full migration is to add `timeout` in bash:

```bash
timeout 600 python3 "$PIPELINE_DIR/extract.py" 2>&1 || true
```
