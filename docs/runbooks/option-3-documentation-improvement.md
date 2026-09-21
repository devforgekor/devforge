# Option 3: 문서화 개선 가이드

**작성일:** 2026-09-21  
**대상:** AI 에이전트 또는 문서 관리자  
**소요 시간:** 30분-1시간  
**난이도:** Low  
**목적:** 리팩토링 진행 상황 추적 및 보안 개선 문서화

---

## 작업 계획

### Task 1: REFACTORING_STATUS.yaml 생성 (20분)

**목표:** 기계 판독 가능한 진행 상황 추적 파일

```yaml
# docs/refactoring/REFACTORING_STATUS.yaml
version: 1.0
last_updated: 2026-09-21
canonical_plan: docs/REFACTORING_PLAN.md
canonical_appendix: docs/REFACTORING_PLAN-appendix.md

current_phase:
  number: 0
  name: Foundation + Characterization Tests
  week: "Week 1-2"
  status: active
  started: 2026-09-13
  progress_percent: 40
  
phase_0:
  week_1:
    status: in_progress
    progress_percent: 60
    completed:
      - pyproject.toml + pip install -e . working
      - src/devforge/ skeleton (7 directories)
      - core/config.py (partial, BaseSettings pattern)
      - core/logging.py (partial, structlog integration)
      - ports/extract.py (LLMPort Protocol defined)
      - application/extract_pipeline.py (initial orchestration)
      - domain subdirectories created (model_management, pipeline, turn_collection, watchdog)
    
    in_progress:
      - task: ConfigRegistry completion
        description: Merge 5 config files into unified ConfigRegistry
        files:
          - src/devforge/core/config.py
        blocked_by: []
      
      - task: Paths abstraction
        description: Resolve 40 hardcoded paths to centralized Paths class
        files:
          - src/devforge/core/paths.py
          - data/hardcoded_paths.csv (inventory)
        blocked_by: []
    
    pending:
      - task: DatabaseGateway
        description: SQLAlchemy 2.0 async pool with Alembic migrations
        files:
          - src/devforge/core/database.py
          - alembic/env.py
          - alembic/versions/
        blocked_by: [ConfigRegistry]
      
      - task: Logging standardization
        description: Complete structlog + JSON output to stdout
        files:
          - src/devforge/core/logging.py
        blocked_by: []
      
      - task: Exception hierarchy
        description: Custom exception classes for error handling
        files:
          - src/devforge/core/exceptions.py
        blocked_by: []
      
      - task: import-linter CI gate
        description: Architecture enforcement via import rules
        files:
          - pyproject.toml ([tool.importlinter])
          - .github/workflows/ci.yml
        blocked_by: [DatabaseGateway, Paths abstraction]
  
  week_2:
    status: pending
    progress_percent: 0
    pending:
      - task: Characterization test 1
        description: day_cycle.sh batch scheduling logic (scanned count = 10)
        files:
          - tests/characterization/test_day_cycle.py
          - tests/fixtures/llm_recordings/day_cycle_*.json
      
      - task: Characterization test 2
        description: check_all_llm T1/T2 probe (port 8082 → 200 OK)
        files:
          - tests/characterization/test_check_all_llm.py
      
      - task: Characterization test 3
        description: call_llm response format + content freeze
        files:
          - tests/characterization/test_call_llm.py
          - tests/fixtures/llm_recordings/call_llm_*.json
      
      - task: Characterization test 4
        description: watchdog 60s loop (liveness_ts update)
        files:
          - tests/characterization/test_watchdog.py
      
      - task: Characterization test 5
        description: text_clean language detection (Korean text sanitization)
        files:
          - tests/characterization/test_text_clean.py

phase_1:
  status: planned
  week: "Week 3-4"

phase_1_5:
  status: planned
  week: "Week 4.5 (2 days)"

archived_docs:
  - docs/_archive/plans/phases.md
  - docs/_archive/plans/refactoring-roadmap.md
  - docs/_archive/plans/azure-golden-image-rebuild-handover.md
  - docs/_archive/plans/code-size-refactoring.md
  - docs/_archive/plans/mcp-consolidation-server-side.md
  - docs/_archive/plans/track-b-migration.md
  - docs/_archive/plans/14b-comparison-test-plan.md
  - docs/_archive/plans/conversation-extraction-research.md
  - docs/_archive/plans/day-night-restructure-plan.md
  - docs/_archive/plans/day-night-split-handoff-plan.md
  - docs/_archive/plans/mcp-sse-server.md
  - docs/_archive/plans/slack-chatops-design.md

documents:
  active:
    - docs/REFACTORING_PLAN.md (v1.4 Final, 17주)
    - docs/REFACTORING_PLAN-appendix.md (Phase 4-8)
    - blueprint.yaml (Phase 0 추가됨)
  
  reports:
    - /tmp/refactoring-status-review.md (점검 결과 2026-09-21)
    - /tmp/refactoring-plan-review.md (오류 점검 2026-09-21)
    - /tmp/refactoring-docs-cleanup-plan.md (정리 계획 2026-09-21)

git_commits:
  - hash: ed6971f
    date: 2026-09-21
    message: "docs(archive): move completed and old plan docs to _archive"
    files_changed: 12
  
  - hash: 733139f
    date: 2026-09-21
    message: "docs(refactoring): update Phase 0 progress and fix week count"
    files_changed: 2

notes:
  - Phase 0 시작일은 src/devforge/ 최초 생성일(2026-09-13) 기준
  - 주차 합계 오류 수정: 16주 → 17주 (Phase −1: 0.5주 + Phase 1.5: 0.5주)
  - 12개 구식 문서 아카이빙 완료 (2026-09-21)
```

**저장:**
```bash
# 위 내용을 파일로 저장
cat > /opt/projects/server/docs/refactoring/REFACTORING_STATUS.yaml << 'EOF'
[위 YAML 내용]
EOF
```

---

### Task 2: Secret Injection Hardening 문서 생성 (15분)

```bash
mkdir -p /opt/projects/server/docs/security
```

```markdown
# docs/security/secret-injection-hardening.md

# Secret Injection Hardening

**Status:** Planned (Stage 3)  
**Origin:** refactoring-roadmap.md §5.1  
**Trigger:** WebObsidian EnvironmentFile quoting bug (2026-09-19)  
**Updated:** 2026-09-21

---

## Problem Statement

**Current State (Stage 2 ✅):**
- KV secrets → `kv-fetch-env.py env --keys` → tmpfs EnvironmentFile → container env
- tmpfs prevents disk persistence (no plaintext files)
- **Problem:** secrets visible in process environment (`ps e`, `/proc/<pid>/environ`, `podman exec env`)

**Example:**
```bash
$ podman exec webobsidian env | grep -c "="
109  # All 109 KV secrets exposed!
```

---

## Solution: LoadCredential / podman --secret

### Stage 3 Goal

| Item | Current (EnvironmentFile) | Target (LoadCredential) |
|------|---------------------------|-------------------------|
| **Storage** | tmpfs → env vars | tmpfs → credential files → app reads file |
| **Visibility** | ✅ tmpfs, ❌ /proc/*/environ | ✅ tmpfs, ✅ /proc/*/environ (not visible) |
| **Access** | Any process can read via /proc | Only systemd + app process |
| **Rotation** | Restart to reload | Restart to reload |

---

## Implementation Plan

### Approach 1: systemd LoadCredential= (Recommended)

**For system services (cashbook, fastapi, mcp):**

```ini
# Before
[Service]
ExecStart=/opt/projects/server/scripts/deploy/kv-fetch-env.py /usr/bin/python3 -m uvicorn main:app

# After
[Service]
ExecStartPre=/opt/projects/server/scripts/deploy/kv-to-credential.sh CASHBOOK-API-KEY /run/user/1000/credentials/cashbook_key
LoadCredential=cashbook_key:/run/user/1000/credentials/cashbook_key
ExecStart=/usr/bin/python3 -m uvicorn main:app
# App reads from: $CREDENTIALS_DIRECTORY/cashbook_key
```

**App code change:**
```python
# Before
API_KEY = os.getenv("CASHBOOK_API_KEY")

# After
creds_dir = os.getenv("CREDENTIALS_DIRECTORY")
if creds_dir:
    API_KEY = Path(creds_dir, "cashbook_key").read_text().strip()
```

### Approach 2: podman --secret

**For Quadlet containers (postgres):**

```ini
# containers/devforge-postgres.container
[Service]
ExecStartPre=/opt/projects/server/scripts/deploy/kv-to-credential.sh POSTGRES-PASSWORD /run/user/1000/secrets/postgres_pw
Environment=POSTGRES_PASSWORD_FILE=/run/secrets/postgres_pw
# Quadlet auto-mounts /run/user/1000/secrets/ → /run/secrets/ in container
```

---

## Migration Sequence

| Service | Sensitive Keys | Code Change Required | Priority | Status |
|---------|----------------|---------------------|----------|--------|
| **cashbook** | CASHBOOK_API_KEY | Yes (main.py) | P3 | ⬜ Planned |
| **postgres** | POSTGRES_PASSWORD | No (POSTGRES_PASSWORD_FILE support) | P1 | ⬜ Planned |
| **webobsidian** | MASTER_PASSWORD | Yes (config loading) | P1 | ⬜ Planned |
| **fastapi** | Multiple (10+ keys) | Yes (core/config.py) | P2 | ⬜ Planned |
| **mcp** | DB credentials | Yes (connection string) | P2 | ⬜ Planned |

**Recommendation:** Start with **postgres** (native support) → **webobsidian** (single key) → **fastapi/mcp** (multiple keys)

---

## Rollback Plan

If LoadCredential causes issues:

```bash
# 1. Restore previous service definition
git checkout HEAD~1 ~/.config/systemd/user/<service>.service

# 2. Reload and restart
systemctl --user daemon-reload
systemctl --user restart <service>.service
```

---

## Verification

### Before (Stage 2)
```bash
PID=$(systemctl --user show -p MainPID --value cashbook.service)
sudo cat /proc/$PID/environ | tr '\0' '\n' | grep CASHBOOK_API_KEY
# Output: CASHBOOK_API_KEY=4280873  ← Visible!
```

### After (Stage 3)
```bash
PID=$(systemctl --user show -p MainPID --value cashbook.service)
sudo cat /proc/$PID/environ | tr '\0' '\n' | grep CASHBOOK_API_KEY
# Output: (empty)  ← Not visible!

# Verify credential file
sudo ls -l /proc/$PID/fd/ | grep credentials
# Expected: link to /run/user/1000/credentials/cashbook_key
```

---

## References

- systemd.exec(5): LoadCredential=
- Podman secrets: https://docs.podman.io/en/latest/markdown/podman-secret.1.html
- refactoring-roadmap.md §5.1 (archived)

---

**Next Actions:**
1. Implement kv-to-credential.sh helper script
2. Choose pilot service (postgres recommended)
3. Test in development environment
4. Rollout to production services sequentially
```

**저장:**
```bash
cat > /opt/projects/server/docs/security/secret-injection-hardening.md << 'EOF'
[위 Markdown 내용]
EOF
```

---

### Task 3: Phase 0 작업 로그 작성 (15분)

```markdown
# docs/refactoring/phase0-work-log.md

# Phase 0 Work Log — Week 1-2

**Phase:** 0 (Foundation + Characterization Tests)  
**Started:** 2026-09-13  
**Target Completion:** 2026-09-27 (2 weeks)  
**Current Status:** 🟡 In Progress (~40%)

---

## Week 1 (2026-09-13 ~ 2026-09-20)

### 2026-09-13 (Day 1)
- ✅ Created pyproject.toml (src-layout, dependencies)
- ✅ Created src/devforge/ skeleton (7 directories)
- ✅ Initial commit: refactor(phase-0): initialize src-layout structure

### 2026-09-14 ~ 2026-09-16
- ✅ core/config.py: BaseSettings pattern (partial)
- ✅ core/logging.py: structlog integration (partial)
- ✅ ports/extract.py: LLMPort Protocol
- ✅ domain/ subdirectories: model_management, pipeline, turn_collection, watchdog

### 2026-09-17 ~ 2026-09-20
- 🟡 ConfigRegistry: 5 config files merge (in progress)
- 🟡 Paths abstraction: hardcoded paths inventory (in progress)

### 2026-09-21 (Documentation Day)
- ✅ Archived 12 old plan documents
- ✅ Fixed REFACTORING_PLAN.md: 16주 → 17주
- ✅ Added Phase 0 progress to REFACTORING_PLAN.md
- ✅ Updated blueprint.yaml: Phase 0 (active, ~40%)
- ✅ Git commits: ed6971f, 733139f

---

## Week 2 (2026-09-23 ~ 2026-09-27) — Planned

### Pending Tasks
- ⬜ Complete ConfigRegistry (5 config files)
- ⬜ Complete Paths abstraction (40 hardcoded paths)
- ⬜ Implement DatabaseGateway (SQLAlchemy 2.0 async)
- ⬜ Add core/exceptions.py
- ⬜ Setup import-linter CI gate
- ⬜ Write 5 characterization tests

### Estimated Timeline
- Mon-Tue: ConfigRegistry + Paths
- Wed: DatabaseGateway + Alembic
- Thu: import-linter + exceptions
- Fri: Characterization tests (5개)

---

## Blockers

None currently.

---

## Notes

- pyproject.toml uses src-layout (PEP 420)
- import-linter will enforce: layers contract, domain independence
- Characterization tests require LLM recording fixtures (Phase −1)

---

**Next Update:** 2026-09-27 (Week 2 complete)
```

**저장:**
```bash
cat > /opt/projects/server/docs/refactoring/phase0-work-log.md << 'EOF'
[위 Markdown 내용]
EOF
```

---

## 검증 체크리스트

- [ ] REFACTORING_STATUS.yaml 생성 완료
- [ ] secret-injection-hardening.md 생성 완료
- [ ] phase0-work-log.md 생성 완료
- [ ] 모든 파일이 올바른 경로에 저장됨
- [ ] YAML 파일 구문 검증 (`python3 -c "import yaml; yaml.safe_load(open('docs/refactoring/REFACTORING_STATUS.yaml'))"`)
- [ ] Git 커밋

---

## Git 커밋

```bash
cd /opt/projects/server
git add \
  docs/refactoring/REFACTORING_STATUS.yaml \
  docs/security/secret-injection-hardening.md \
  docs/refactoring/phase0-work-log.md

git commit -m "docs: add refactoring progress tracking and security docs

New documents:
- REFACTORING_STATUS.yaml: machine-readable progress tracker (Phase 0: 40%)
- security/secret-injection-hardening.md: LoadCredential migration plan
- reports/phase0-work-log.md: Week 1-2 work log

Purpose:
- Enable automated progress queries
- Document security hardening roadmap (from refactoring-roadmap.md §5.1)
- Track daily Phase 0 activities

Related: REFACTORING_PLAN.md v1.4, blueprint.yaml Phase 0

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## 다음 단계

- REFACTORING_STATUS.yaml을 주간 단위로 업데이트
- Phase 0 완료 시 status를 "complete"로 변경
- MCP 도구로 진행률 조회 기능 추가 (선택)

**소요 시간:** 약 50분 (YAML 20분 + 보안 문서 15분 + 작업 로그 15분)
