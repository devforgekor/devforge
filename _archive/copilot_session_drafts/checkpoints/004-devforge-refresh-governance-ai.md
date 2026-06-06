<overview>
The user's conversation spanned two major initiatives: (1) finalizing DevForge's reference tracking and version refresh governance policy (v1.2.1), then implementing automated systems (RSS monitor + systemd timers), and (2) designing a new centralized AI conversation storage system to capture decision rationale across multiple AI platforms and devices. The approach combined policy refinement via iterative feedback, practical automation deployment using systemd timers and Python, and comprehensive system architecture design with subsequent validation against implementation realities.
</overview>

<history>
1. **User requested reference tracking policy v1.0 review and refinement**
   - Submitted initial YAML document with stability gates, migration difficulty levels, and 4-month refresh procedure
   - Identified issues: vague breaking-change criteria, implicit health check requirements, missing emergency CVE response
   - Action: Created v1.1 with explicit breaking-change examples, concrete health_check_criteria (unit_active, healthcheck_passes, api_response, log_stability), and emergency CVE bypass procedure
   - Outcome: Policy structure solid, but health checks still duplicated across sections

2. **User requested DRY refactor of health checks**
   - Consolidated health_check_suite into single definition at document top, referenced throughout
   - Delivered v1.2.1 with 100% elimination of duplication
   - Outcome: Clean, maintainable policy document

3. **User corrected refresh cycle schedule**
   - Initially implemented 4-month cycle as 9/1, 1/1, 5/1 UTC
   - Corrected to 4/15, 8/15, 12/15 UTC 00:00 (exactly 4-month intervals, mid-month timing)
   - Updated systemd timer OnCalendar directives and policy metadata
   - Outcome: Confirmed next refresh is 2026-08-15 (3 months from current date 2026-05-13)

4. **User requested implementation of "next steps"**
   - Phase 1: Created reference-watchlist.md v1.0 with baseline snapshot of all 9 references (Podman 5.6.0, systemd 252, postgres:2026.05.13, litellm:2026.05.13, etc.)
   - Phase 2: Wrote rss-monitor Python script (6.3KB, executable) to poll RSS feeds weekly and log to systemd journal with REFERENCE tag
   - Phase 3: Created systemd timers: reference-monitor.timer (Mon 10:00 UTC) and devforge-refresh-reminder.timer (4/15, 8/15, 12/15 00:00 UTC)
   - All enabled, reloaded, and verified operational (next runs: Mon 2026-05-18 10:00 GMT and Sat 2026-08-15 00:00 GMT)
   - Outcome: DevForge now has fully automated reference tracking in place

5. **User introduced AI conversation centralization system design**
   - Submitted comprehensive v1.0 design document with architecture, DB schema (PostgreSQL + pgvector), MCP server endpoints, FastAPI REST APIs, desktop/mobile client strategies
   - Document includes Wing-Room-Conversation hierarchy (inspired by MemPalace), decision tracking, and multi-device sync via local SQLite + server

6. **User requested design review and validation**
   - Conducted systematic review against implementation realities and current tech landscape
   - Identified 6 critical/high/medium priority issues:
     1. MCP implementation incorrectly specified as HTTP (should be stdio-based)
     2. Embedding cost strategy underdefined (should optimize to decisions only)
     3. Mobile implementation unrealistic (iOS webview app requires App Store approval, 3-6 months)
     4. seedling relationship unclear (complete separation vs integration)
     5. Security details missing (JWT, TLS, rate limiting code examples needed)
     6. Local SQLite schema undefined (no synced flag, no server_id mapping)
   - Provided concrete code examples and refactored roadmap (Phase 1: 4-6 weeks core backend, Phase 2: 2-3 weeks mobile, Phase 3: ongoing automation)
   - Outcome: Design validated as 8.0/10 feasibility, strongly recommended for implementation with fixes applied
</history>

<work_done>
**Files created:**
- `/opt/projects/server/docs/reference-tracking-policy.yaml` - v1.2.1 final (23KB, 484 insertions)
- `/opt/projects/server/docs/reference-watchlist.md` - v1.0 baseline (9.3KB, 9 references documented)
- `/home/opc/.config/devforge/bin/rss-monitor` - Python RSS monitor script (6.3KB, executable)
- `/home/opc/.config/systemd/user/reference-monitor.timer` - Weekly RSS polling schedule
- `/home/opc/.config/systemd/user/reference-monitor.service` - RSS monitor service definition
- `/home/opc/.config/systemd/user/devforge-refresh-reminder.timer` - 4-month refresh cycle reminder
- `/home/opc/.config/systemd/user/devforge-refresh-reminder.service` - Refresh reminder service

**Files modified:**
- `/opt/workspace/seedling/docs/worklog.json` - Added 2 entries (policy v1.2.1 completion, Phase 2 automation setup)
- `/opt/workspace/seedling/docs/worklog_entries.json` - Updated with rolling 20-entry history

**Git commits (in /opt/projects/server):**
- 4705839: Add reference-tracking-policy.yaml v1.2.1 (DRY refactor)
- 1ddb36f: Add reference-watchlist.md v1.0 (baseline snapshot)
- fdf2b16: Fix refresh cycle to 4/8/12 on 15th UTC
- b613190: Confirm next refresh date is 2026-08-15

**Tasks completed:**
- [x] Refined reference-tracking-policy.yaml from v1.0 → v1.2.1
- [x] Consolidated health_check_suite (eliminated duplication)
- [x] Corrected 4-month refresh cycle schedule (4/15, 8/15, 12/15)
- [x] Created reference-watchlist.md baseline
- [x] Implemented rss-monitor script (Python, RSS parsing, local cache, graceful error handling)
- [x] Created systemd timers (enabled, verified operational)
- [x] Updated worklog entries (rolling 3-entry window maintained)
- [x] Designed AI conversation centralization system (v1.0 architecture complete)
- [x] Conducted comprehensive design review and validation

**Current state:**
- DevForge reference tracking: **OPERATIONAL** (weekly RSS monitoring live, 4-month refresh cycle scheduled)
- AI conversation system design: **COMPLETE, VALIDATED** (8.0/10 feasibility, ready for Phase 1 implementation with 6 identified refinements)
- Systemd timers: Active and waiting (next RSS scan: Mon 2026-05-18 10:00 UTC; next refresh reminder: Sat 2026-08-15 00:00 UTC)
</work_done>

<technical_details>

### Reference Tracking Policy (v1.2.1)

- **health_check_suite DRY pattern**: Defined once at document top (4 checks: unit_active, healthcheck_passes, api_response, log_stability), referenced in step 6c and rollback procedures. Eliminates documentation debt and ensures consistency.
- **4-month refresh timing**: Scheduled for 15th UTC at 00:00 (not cycle start date). Provides buffer across timezones, avoids year-end congestion, maintains exact 4-month intervals.
- **Breaking-change criteria**: All stability gates now explicitly include "No breaking change to [component] syntax" with concrete examples (Quadlet Volume→Mount migration, systemd OnCalendar format shifts).
- **Emergency CVE bypass**: Separate procedure outside 4-month cycle; CRITICAL CVEs trigger immediate evaluation tree (patch available → execute, no patch workaround available → apply, neither → isolate).

### RSS Monitor Implementation

- **stdio vs HTTP**: MCP protocol is stdio-based (JSON-RPC over stdin/stdout), not HTTP. Current design mistakenly specified `curl` command; needs `mcp.server.Server` library wrapper.
- **Feed format agility**: Parser handles both Atom and RSS formats via `ElementTree` with namespace-aware queries.
- **Duplicate suppression**: Local cache (`.rss-cache/`) stores last 5 items per reference; only logs new items, preventing journal spam.
- **Network resilience**: Failed fetches return exit code 2 (not 1) so systemd timer doesn't mark as failed on transient errors.
- **Cost optimization**: Currently logs all version discoveries; future: filter to only gate-relevant changes.

### AI Conversation Centralization System (Critical Issues)

1. **MCP Architecture Mismatch**: Design specified HTTP REST endpoints for MCP; correct approach is:
   - MCP Server: Python `mcp.server.Server` (stdio-based, Claude Code communicates via json-rpc)
   - FastAPI: Separate REST layer for mobile/web UI (not MCP traffic)
   - Two distinct communication channels, not one

2. **Embedding Cost Explosion Risk**: Naive approach (embed all messages) would cost ~$60-600/year depending on volume. Mitigation strategy:
   - Tier 1: Always embed decisions (100% coverage, highest value)
   - Tier 2: Sample other messages (1 per 100) for vector search
   - Tier 3: User-explicit "remember this" for immediate indexing
   - Estimated: $0.60-2/year (vs $600 without optimization)

3. **Mobile Reality Check**: 
   - iOS webview app requires App Store submission (3-6 month turnaround, complex review)
   - Android browser extensions only recently supported (2024+)
   - Phase 1 must focus on search UI (2-4 weeks) vs auto-capture (6+ months)
   - Phase 2: Manual upload via iOS Shortcuts + Android form
   - Phase 3: Auto-capture when MCP mobile clients mature

4. **Unresolved Architectural Decision**: Is this a new system entirely separate from seedling, or integrated into seedling.db?
   - **Recommendation: Complete separation** (independent DB, independent ops, seedling connects via MCP client)
   - Rationale: Decoupled evolution, independent backup/recovery, microservices-ready

5. **Security Implementation Gap**: Design document states "add JWT" but provides no code. Needs:
   - JWT token generation/validation in FastAPI
   - HTTPS/TLS enforcement
   - Rate limiting middleware
   - Audit logging for all API calls
   - User data isolation by user_id

6. **Local SQLite Sync Gap**: sync_agent.py references undefined schema. Required:
   ```sql
   CREATE TABLE memories (
       id TEXT PRIMARY KEY,
       content TEXT,
       tag TEXT CHECK (tag IN ('decision', 'fact', 'preference', 'note')),
       synced BOOLEAN DEFAULT 0,
       server_id TEXT
   );
   ```
   Without `server_id`, no way to map back after successful upload.

### Unresolved Questions

- Should local SQLite double-encrypt sensitive content, or rely on filesystem permissions?
- How to handle offline composition of decisions? (User drafts decision without network → what schema?)
- Should "Wing" and "Room" be user-created or pre-defined taxonomy?
- Is email-based export of decisions needed for compliance/archival?

</technical_details>

<important_files>

- **`/opt/projects/server/docs/reference-tracking-policy.yaml`**
  - Why: North star document for DevForge version governance; defines all stability gates, migration difficulty levels, refresh procedure, emergency response
  - Changes: v1.2.1 final (health_check_suite DRY refactor, breaking-change examples, emergency CVE section, explicit 4-month refresh timing)
  - Key sections: metadata (lines 1-19), health_check_suite (lines 21-47), refresh_procedure (lines 260-380)

- **`/opt/projects/server/docs/reference-watchlist.md`**
  - Why: Operational baseline snapshot; tracks current deployed versions and gate status for each of 9 references
  - Changes: Created v1.0 from scratch; documents Podman 5.6.0, systemd 252, postgres/litellm 2026.05.13 images with PASS/PASS/PASS status
  - Key sections: Overview table (lines 6-15), detailed reference sections (lines 19-150), next refresh date: 2026-08-15

- **`/home/opc/.config/devforge/bin/rss-monitor`**
  - Why: Automated weekly reference discovery; feeds to journal with REFERENCE tag for operational awareness
  - Changes: Created executable Python script with graceful error handling, local cache, YAML policy reader
  - Key functions: fetch_rss (RSS/Atom parsing), monitor_reference (dedup logic), main (policy loading)

- **`/home/opc/.config/systemd/user/reference-monitor.timer` and `.service`**
  - Why: Automation trigger for weekly RSS scans; integrated with systemd logging
  - Changes: Timer OnCalendar=Mon *-*-* 10:00:00 UTC, service runs rss-monitor script
  - Status: Enabled, active, next run 2026-05-18 10:00 GMT

- **`/home/opc/.config/systemd/user/devforge-refresh-reminder.timer` and `.service`**
  - Why: 4-month cycle reminder; fires 2026-08-15, 2026-12-15, 2027-04-15 (and repeating)
  - Changes: OnCalendar *-04-15, *-08-15, *-12-15 00:00:00 UTC; logs REFRESH tag to journal
  - Status: Enabled, active, next run 2026-08-15 00:00 GMT

- **`/opt/workspace/seedling/docs/worklog.json` and `.../worklog_entries.json`**
  - Why: Operational tracking and session history
  - Changes: Added 2 entries (policy v1.2.1, Phase 2 automation), maintained rolling 3-entry window in main file, 20-entry overflow in _entries
  - Key metadata: dates, tags (DevForge, 운영정책, 거버넌스)

</important_files>

<next_steps>

**Completed and Stable:**
- DevForge reference tracking infrastructure (fully deployed, operational)
- reference-tracking-policy.yaml v1.2.1 (finalized)
- Next refresh scheduled: 2026-08-15

**Pending: AI Conversation Centralization System (Design Phase Complete)**

**Priority 1 - Design Fixes (before implementation):**
- Correct MCP architecture (stdio-based Server class, separate from FastAPI)
- Add security implementation code examples (JWT, TLS, rate limiting)
- Define realistic mobile roadmap (Phase 1: search UI only, 2-4 weeks)
- Add embedding cost optimization strategy (decisions always, others sampled)
- Clarify seedling separation vs integration decision
- Define local SQLite schema with synced flag and server_id mapping

**Priority 2 - Phase 1 Implementation (4-6 weeks, if proceeding):**
1. PostgreSQL `memories` database setup
2. FastAPI app scaffolding (security layer)
3. MCP server implementation (mem_save, mem_search tools)
4. REST API endpoints (5 core: conversations, messages, decisions, search, sync)
5. Claude Code MCP integration test
6. Local sync_agent.py (SQLite → server)
7. React search UI (responsive, PWA-ready)
8. Deployment and HTTPS setup

**Decision Point:**
User should decide: 
1. Continue with DevForge reference tracking only (current momentum, stable), or
2. Proceed with Phase 1 of AI conversation system (apply 6 design fixes first)

**If proceeding with conversation system:**
- Estimated start: 1-2 weeks for design refinements
- Estimated Phase 1 completion: 6-10 weeks from now
- First operational test: Basic mem_save/mem_search with Claude Code
- Measurable outcome: "Save decision, search past decisions from Claude Code session"

</next_steps>