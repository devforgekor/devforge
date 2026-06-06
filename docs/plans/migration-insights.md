# Worklog Auto-Reconciliation: v1.1 → B-Plan Evolution Roadmap

**Final Decision**: Immediate deploy v1.1 + 2 weeks monitoring + stability proof → B-Plan cutover

---

## Full Timeline

```
┌─ PHASE 1: v1.1 Immediate Deployment (This week)
│  ├─ Duration: 1-2 hours
│  ├─ Action: Fix quote bug in session_guard.py + add DB UNIQUE constraint
│  ├─ Target: Production deployment
│  └─ Success Metric: Continuous 100% success record (0 quote errors)
│
├─ PHASE 2: Monitoring & Stability Proof (1-2 weeks)
│  ├─ Duration: 14 days
│  ├─ Action: Daily worklog recording rate monitoring (verify 100% achievement)
│  ├─ Target: systemd timer normal operation + DB stability confirmed
│  └─ Success Metric: 
│      • Total commit scan rate 98%+ (miss rate within 2%)
│      • 0 DB duplicate contamination
│      • systemd auto-retry normal operation
│
├─ PHASE 3: B-Plan Environment Validation (Next week)
│  ├─ Duration: 30-60 minutes (Phase 0 of B-Plan)
│  ├─ Action: Execute 5 environment validations
│  │   1. Git repository validate
│  │   2. PostgreSQL connectivity + schema
│  │   3. worklog_entries table structure check
│  │   4. Local LLM (localhost:4000 LiteLLM) connection test
│  │   5. systemd timer settings re-verification
│  ├─ Target: B-Plan implementation readiness
│  └─ Success Metric: 5/5 checks passed
│
├─ PHASE 4: B-Plan Code Implementation (Next week - Next month)
│  ├─ Duration: 4.5 hours (AI agent writing basis)
│  ├─ Module: worklog_config.py, git_scanner.py, llm_caller.py, db_saver.py, etc.
│  ├─ Target: Staging environment deployment
│  └─ Success Metric: All Phase 1-3 unit tests passed
│
├─ PHASE 5: B-Plan Staging Tests (Next month)
│  ├─ Duration: 45 minutes (Integration tests + performance tests)
│  ├─ Action: Unit tests + integration tests + load tests
│  ├─ Target: 100% stability confirmed in staging
│  └─ Success Metric: 300 commits × LLM call all successful + 0 timeouts
│
└─ PHASE 6: B-Plan Production Cutover (Mid next month)
   ├─ Duration: 2-3 hours (Migration + health check)
   ├─ Action: v1.1 → B-Plan switch + existing log migration
   ├─ Target: Production operation
   └─ Success Metric: 24-hour error rate < 1%
```

---

## PHASE 1: v1.1 Immediate Deployment

### Work Items

#### 1.1 session_guard.py — SQL injection prevention + commit logging added
```python
# review_worker.py:165-167 reference — DB string escape
def _esc_sql(s: str) -> str:
    return s.replace("'", "''").replace("\\", "\\\\")

# Always escape when inserting commit messages
sha = _esc_sql(sha)
message = _esc_sql(message[:100])
sql = f"INSERT INTO worklog_entries (date, title, git_commit_hash, summary, agent) ..."
# Safe even when commit message contains single quotes(')
```

#### 1.2 DB Schema Change (commit_hash column + UNIQUE constraint)
```sql
-- Step 1: Add column (only if nonexistent)
ALTER TABLE worklog_entries ADD COLUMN IF NOT EXISTS git_commit_hash TEXT;

-- Step 2: Dedup UNIQUE constraint
ALTER TABLE worklog_entries ADD UNIQUE (date, git_commit_hash);

-- Step 3: Partial index (nullable — for manual records)
CREATE UNIQUE INDEX IF NOT EXISTS idx_wl_commit_hash
  ON worklog_entries(git_commit_hash)
  WHERE git_commit_hash IS NOT NULL;
```

#### 1.3 systemd environment variable settings
```ini
# Currently operating timers:
#   review-worker.timer → review-worker.service (review_worker.py)
#   devforge-qwen-worker.timer → devforge-qwen-worker.service (old, to be deprecated)
# v1.1 application target: Apply to appropriate service depending on session_guard.py invocation

[Service]
SuccessExitStatus=0
Restart=on-failure
RestartForceExitStatus=1
RestartMaxAttempts=3
RestartSec=60
StandardOutput=journal
StandardError=journal
```
Note: `SuccessExitStatus=2` excluded — may incorrectly treat lock collision skip as success.

### Deployment Commands
```bash
# 1. Apply changes
cd /opt/projects/server
git add scripts/session_guard.py
git commit -m "fix: add git_commit_hash column, SQL escaping, commit logging"

# 2. DB migration (using podman exec)
podman exec -i postgres psql -U postgres -d devforge_app << 'SQL'
ALTER TABLE worklog_entries ADD COLUMN IF NOT EXISTS git_commit_hash TEXT;
ALTER TABLE worklog_entries ADD UNIQUE (date, git_commit_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wl_commit_hash ON worklog_entries(git_commit_hash) WHERE git_commit_hash IS NOT NULL;
SQL

# 3. systemd reload & service restart
systemctl --user daemon-reload
systemctl --user restart review-worker.service
```

---

## PHASE 2: Monitoring & Stability Proof (2 weeks)

### Daily Metrics to Check

#### Daily Checklist (KST 09:00)
```bash
# Verify yesterday's commit records
podman exec -i postgres psql -U postgres -d devforge_app -c \
  "SELECT COUNT(*), COUNT(DISTINCT git_commit_hash) FROM worklog_entries \
   WHERE date = CURRENT_DATE - INTERVAL '1 day';"

# Check error logs
journalctl --user -u review-worker.service -p err --since "24 hours ago"

# Check duplicate records
podman exec -i postgres psql -U postgres -d devforge_app -c \
  "SELECT git_commit_hash, COUNT(*) FROM worklog_entries \
   GROUP BY git_commit_hash HAVING COUNT(*) > 1;"
```

#### Success Criteria
| Metric | Target | Weekly Report |
|------|------|----------|
| Commit recording rate | 98%+ | Mon-Fri daily records 5/5 or above |
| Duplicate contamination | 0 | Weekly duplicate records 0 |
| Error recovery rate | 100% | systemd retry success 3+ times |

### Weekly Report (Friday 17:00)
```yaml
week_1_monitoring:
  total_commits_scanned: 127
  commits_logged: 125
  miss_rate: 1.6%
  duplicate_records: 0
  errors_recovered: 2
  systemd_restart_count: 1
  status: "PASS - Stability confirmed"
```

---

## Issues Discoverable in PHASE 2 & Responses

### Potential Issue 1: Insufficient Quote Escaping
```
Symptom: Korean/special character commit message recording failure
Response: Use SQL parameterization or json.dumps()
```

### Potential Issue 2: DB Concurrency Conflict
```
Symptom: Same commit recorded twice
Response: Verify UNIQUE constraint works + application-level dedup
```

### Potential Issue 3: systemd Timer Drift
```
Symptom: 15-minute timer actually runs at 20+ minute intervals
Response: journalctl timestamp analysis + accuracy verification
```

---

## PHASE 3: B-Plan Environment Validation

### P0-1: Git Repository Validation
```bash
# Verify DevForge server git log accessibility
cd /opt/projects/server
git log --oneline --since="24 hours ago" | wc -l
# Expected: Minimum 5 commits
```

### P0-2: PostgreSQL Connection + Schema
```bash
# DB accessibility + worklog_entries table check
podman exec -i postgres psql -U postgres -d devforge_app -c "\d worklog_entries;"
# Expected: columns: id, date, git_commit_hash, title, agent, created_at
```

### P0-3: worklog_entries Schema Validation
```sql
-- Check columns required by B-Plan
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name = 'worklog_entries'
ORDER BY ordinal_position;

-- Check if additional columns needed
-- Required by B-Plan: git_commit_hash (UNIQUE INDEX), llm_summary (nullable)
```

### P0-4: Local LLM Connection Test
```bash
# LiteLLM router (localhost:4000) health check — all LLM calls MUST use this endpoint
curl -s http://localhost:4000/health | jq .

# llama.cpp backend (localhost:8080) health check
curl -s http://localhost:8080/health | jq .

# Simple API call test (timeout 30s)
curl -X POST http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer devforge-litellm-key" \
  -d '{
    "model": "qwen2.5-coder-14b",
    "messages": [{"role": "user", "content": "Test"}],
    "max_tokens": 100
  }' \
  --max-time 30
```

### P0-5: systemd Timer Re-verification
```bash
# Check existing timers
systemctl --user list-timers review* devforge*

# Confirm existing timer is 15-minute interval
systemctl --user cat review-worker.timer

# Review feasibility of creating new timer for B-Plan
# (Separation of concerns: v1.1 uses existing timer, B-Plan uses separate timer)
```

### PHASE 3 Success Criteria
```yaml
p0_validation_checklist:
  - git_log_access: ✅ 20 commits found
  - postgresql_connected: ✅ worklog_entries table exists
  - schema_validated: ✅ 7 columns matched
  - llm_healthcheck: ✅ response_time < 10s
  - timer_verified: ✅ 15-minute interval confirmed
  status: "READY_FOR_PHASE_4"
```

---

## PHASE 4: B-Plan Code Implementation

### Module Breakdown (AI agent writing target)

| Module | File | Lines | Role | Depends On |
|------|-----|-------|------|------|
| Config | worklog_config.py | 150 | Environment settings load | None |
| Git Scanner | git_scanner.py | 200 | git log parsing | Config |
| LLM Caller | llm_caller.py | 250 | LocalLLM API call | Config |
| DB Saver | db_saver.py | 200 | PostgreSQL INSERT | Config |
| Lock Manager | lock_manager.py | 150 | PID-based concurrency control | Config |
| Main Orchestrator | worklog_reconcile.py | 200 | Overall coordination | All |
| Systemd Service | worklog-reconcile.service | 20 | Timer settings | None |

### Protocol Specification (based on MACHINE-READABLE-SPEC.md)

#### Exit Codes
```
0 = Success (All commits recorded)
1 = Transient Error (Retryable, systemd auto-handling)
2 = Skip (Lock collision, normal skip)
3 = Alert (DB down, manual intervention required)
4 = Permanent Error (Config error, must be resolved)
5 = Git Broken (Repository corrupted, urgent)
```

#### JSON Logging
```json
{
  "timestamp": "2026-05-25T14:30:00Z",
  "level": "INFO",
  "component": "git_scanner",
  "function": "scan_recent_commits",
  "event_type": "scan_complete",
  "message": "Scanned 23 commits in last 15 minutes",
  "data": {
    "commit_count": 23,
    "time_window_minutes": 15,
    "duplicate_filtered": 2
  }
}
```

---

## PHASE 5: B-Plan Staging Tests

### Unit Tests (15 min)
```bash
pytest tests/test_git_scanner.py -v
pytest tests/test_llm_caller.py -v
pytest tests/test_db_saver.py -v
pytest tests/test_lock_manager.py -v
```

### Integration Tests (20 min)
```bash
# Full pipeline execution in staging environment
python3 scripts/worklog_reconcile.py --mode=staging --dry-run

# Actual recording test (isolated staging DB)
python3 scripts/worklog_reconcile.py --mode=staging
```

### Performance Tests (10 min)
```bash
# 300 commits × LLM summary processing
# Expected time: 300 × 5s(LLM) = 1500s = 25min (within 30min timer window)
# Confirm 0 timeouts
```

---

## PHASE 6: B-Plan Production Cutover

### Cutover Checklist
```yaml
pre_cutover:
  - backup_current_v1_1: "✅ git stash + DB snapshot"
  - staging_tests_passed: "✅ All tests passed"
  - monitoring_metrics_collected: "✅ 2 weeks data collected"
  - rollback_plan_documented: "✅ recovery.sh written"

cutover_steps:
  - 01_stop_v1_1_timer: "systemctl --user stop review-worker.timer"
  - 02_deploy_b_plan_code: "git pull + systemd files deployed"
  - 03_run_db_migration: "worklog_reconcile.py --init-schema"
  - 04_start_b_plan_timer: "systemctl --user enable/start worklog-reconcile.timer"
  - 05_monitor_first_run: "journalctl -f -u worklog-reconcile.service"
  - 06_verify_24h: "Monitor 24 hours, error rate < 1%"

rollback_steps:
  - 01_stop_b_plan: "systemctl --user stop worklog-reconcile.timer"
  - 02_restore_v1_1: "git checkout <v1.1-commit>"
  - 03_restart_old_timer: "systemctl --user start review-worker.timer"
```

---

## Current Position (2026-05-18)

- ✅ B-Plan design complete (MACHINE-READABLE-SPEC.md, INDEX.md)
- ✅ 3 system blind spot analysis complete
- ⏳ **Next**: PHASE 1 (v1.1 deployment) approval & execution

---

## Reference Documents

- **Full design**: `/opt/projects/server/docs/worklog-b-plan-complete.tar.gz` (Azure Blob link)
- **Machine protocol**: `MACHINE-READABLE-SPEC.md`
- **Execution guide**: `DETAILED-IMPLEMENTATION-PLAN.md`
- **Master index**: `INDEX.md`

---

**Decision**: v1.1 deployment → 2 weeks monitoring → B-Plan cutover  
**Approval needed**: PHASE 1 start (session_guard.py modification + DB UNIQUE constraint)
