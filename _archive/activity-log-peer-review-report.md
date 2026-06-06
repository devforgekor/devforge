# Peer Review Synthesis — activity-log-unified-plan v1.1

**Date**: 2026-05-22
**Reviewers**: 4 AI agents
**Outcome**: APPROVED with revisions incorporated

---

## 1. Accepted Changes (17 items)

### Schema

| # | Change | Source | Reason |
|---|--------|--------|--------|
| 1 | `parent_id BIGINT` self-referencing FK added | R2, R3 | Machine-to-machine DAG causal tracking. Phi-4→14B→32B inheritance chain in 3-stage relay directly queryable in DB |
| 2 | `trace_id TEXT` added | R3 | Forensic tracking UUID for execution chains. Multiple run_ids linked via single trace even when crossing |
| 3 | `queue_status TEXT DEFAULT 'unprocessed'` added | R3 | Async queue state control. Each stage marks `unprocessed`→`consumed` |
| 4 | `exec_status TEXT DEFAULT 'DONE'` added | R3 | INIT/RUN/DONE/FAIL execution status. Separated from summary_status concern |
| 5 | Split into 3 status fields: `summary_status` / `queue_status` / `exec_status` | R3 | v1.0 tried expressing summary+execution+queue state in single `status` field — concern mixing |
| 6 | `idx_activity_stage_unique` added: UNIQUE(run_id, type, parent_id) | R4 | Pipeline duplicate INSERT prevention |
| 7 | `idx_activity_body_gin` added: GIN(body) | R4 | Metric search within body JSONB |
| 8 | `idx_activity_queue` added: (queue_status, created_at) | Self-added | Async queue polling query optimization |
| 9 | `idx_activity_trace`, `idx_activity_parent` added | Self-added | Forensic tracking query optimization |

### Logic / Data flow

| # | Change | Source | Reason |
|---|--------|--------|--------|
| 10 | Summarizer SELECT includes `body` field | R3 | **Fatal data starvation bug fix.** v1.0 SELECTed only title+summary, excluded body → LLM unable to see actual stage result content |
| 11 | `_insert_activity_stage()` writes meaningful text in `summary` | R3 | v1.0 had summary='' → impossible to summarize from title only. Auto-generated per-stage descriptions |
| 12 | `created_at < NOW() - INTERVAL '5 minutes'` guard added | R1 | Prevents summarizer from reading in-progress raw events (race condition) |
| 13 | JSON parse failure → `summary_status='parse_failed'` + exit 0 | R1 | v1.0 had exit 1 → systemd retry → same failure infinite loop. Mark failed, retry with short prompt next cycle |

### Timer

| # | Change | Source | Reason |
|---|--------|--------|--------|
| 14 | Timer: KST 18:01 → **KST 07:30** | R2 | After overnight 3-stage relay completes (~23:00-02:00), morning briefing value before admin arrives |

### Documentation

| # | Change | Source | Reason |
|---|--------|--------|--------|
| 15 | Slack Operator integration explicitly noted in Query Layer | R2 | v1.0 only mentioned CLI; added `/devforge result` → activity_log query → Block Kit message flow |
| 16 | Section 2.3 Event flow: full rewrite from synchronous to async 3-stage relay | R3 | v1.0 based on old review_worker.py (extract→verify in single process). Changed to distributed queue consumption structure matching actual architecture |
| 17 | Section 4.4: review_worker.py records `run_id`, `trace_id`, `queue_status='unprocessed'` | R3 | v1.0 only left single log without run_id → 14B unable to consume |

---

## 2. Accepted — Q1~Q5 Peer Consensus

| Question | Decision | For | Against | Rationale |
|----------|----------|-----|---------|-----------|
| Q1: 14B vs 32B | **Qwen14B** | 4 | 0 | Always running + summarization is information compression (not creative generation) + removes 32B swap window dependency |
| Q2: Grouping criteria | **LLM delegate + date boundary rule** | 4 | 0 | run_id priority grouping, LLM decides within same date. Prevents split across midnight-crossing jobs |
| Q3: observations absorption | **Keep separate** | 4 | 0 | Time-series metrics vs event logs — different data shapes |
| Q4: review_facts recording | **Run-level summary only** | 4 | 0 | Individual facts in review_facts, activity_log for aggregation only |
| Q5: Exit codes | **0/1 only** | 4 | 0 | systemd retry + journald error logging sufficient |

---

## 3. Rejected Changes (3 items)

| # | Suggestion | Source | Reason for rejection |
|---|------------|--------|---------------------|
| R1 | `_sanitize_for_log()` PII masking function | R4 | **Unnecessary premature optimization.** activity_log stored in internal-network PostgreSQL, body JSONB is already system-internal data (code diffs, token counts). No code path places API keys/passwords in body. Add later if needed |
| R2 | Summarizer 32B fallback option | R1 | **Architecture simplicity first.** Podman B :8081 (phi-4-mini/phi-4) alone is sufficient. Fallback logic only increases complexity. 32B is MODE=code only, does not exist during MODE=batch/normal hours |
| R3 | Embed Section 9 monitoring queries into activity_summarizer.py | R4 | **Separation of concerns.** Monitoring is handled by motd-gen.timer / gen_server_state.py. Summarizer performs pure summarization+INSERT only |

---

## 4. Noted — Implementation Guidance (3 items)

Reference notes for implementation. Plan document modification not required.

| # | Note | Source |
|---|------|--------|
| N1 | `git_commit_hash` UNIQUE constraint applies only to `type='commit'` — stage/review linked via run_id regardless of commit hash | R1 |
| N2 | After Phase A deployment, monitor `raw → summarized` conversion rate for 1 week before cutover | R4 |
| N3 | As activity logs accumulate, consider body trimming or partitioning for rows >90 days old with `summary_status='summarized'` | R4 |

---

## 5. Version History

| Version | Date | Status |
|---------|------|--------|
| v1.0 | 2026-05-22 | Initial proposal |
| v1.1 | 2026-05-22 | 17 accepted changes from 4 peer reviews incorporated |
| v1.1a | 2026-05-22 | Infrastructure correction: LiteLLM references removed (was deleted 2026-05-19). Summarizer endpoint corrected: Podman B :8081 (phi-4-mini/phi-4), not Podman A :8080 (Qwen3-4B is intermittently stopped). Actual container modes documented (normal/batch/code). |

---

*Report generated for human decision-maker review. Implementation ready per v1.1.*
