<overview>
The user conducted a comprehensive operational consolidation of DevForge server infrastructure, driven by the goal to eliminate hybrid complexity and establish single-policy governance. The work transitioned from a fragmented architecture (systemd system + user + cron, Quadlet abandoned, mixed UTC/KST, latest tags, scattered secrets) to a unified model (Quadlet-only, user systemd consolidation, internal UTC, fixed local tags, EnvironmentFile-based secrets). The approach was iterative: audit → identify conflicts → research → decide → apply → validate → document → plan future updates.
</overview>

<history>
1. **User requested full system complexity audit**
   - Analyzed DevForge structure: Quadlet/systemd/backup scripts/timezone mixing
   - Discovered hybrid layering (systemd system + user + cron) and time policy conflicts
   - Outcome: identified 3 core complexity sources needing consolidation

2. **User requested policy simplification**
   - Proposed Quadlet-only runtime, user systemd consolidation, UTC internal + KST user-facing, fixed tags, EnvironmentFile secrets
   - User confirmed: "Quadlet운영으로 변경. 고정 태그 버전으로 가자. 시간은 타이머도 utc로 통일"
   - Outcome: policies finalized as north star

3. **User requested implementation of all changes**
   - Modified Quadlet files (pod/container definitions with ServiceName, EnvironmentFile, Pull=never, fixed tags)
   - Created /home/opc/.config/devforge/secrets.env (chmod 600, single injection point)
   - Updated litellm config.yaml to use os.environ/* references
   - Migrated devforge-backup/restore from system to user systemd
   - Outcome: runtime fully operational, all 5 policies applied

4. **User requested legacy cleanup & document sync**
   - Archived legacy generated-systemd units to README
   - Added transient healthcheck filter to gen_server_state.py
   - Updated CLAUDE.yaml/handover.yaml/blueprint.yaml with latest policies
   - Synced worklog.json entries (top 3 retained)
   - Outcome: no structural conflicts remain, documentation aligned

5. **User requested final complexity validation**
   - Rescanned: unit topology, timers, secrets, timestamps, config references
   - Result: all policies confirmed operational; system stable

6. **User requested alignment of all remaining rule/document conflicts**
   - Modified gen_server_state.py: TZ=timezone.utc, collect_container_flags fallback
   - Updated blueprint.yaml: removed latest tags, host-networking language, systemd-system PostgreSQL references
   - Updated handover.yaml: reframed Quadlet-was-abandoned as historical/superseded
   - Updated DevForge_v3.0_FINAL.md: marked as historical, corrected management policy
   - Outcome: zero conflicts detected on final scan

7. **User introduced reference-tracking-policy.yaml for 4-month refresh governance**
   - Created v1.0 policy document with stability gates, migration difficulty levels, refresh procedure
   - User requested revision for "breaking change to Quadlet syntax" gate + better rollback criteria
   - Delivered v1.1: added breaking-change check to all version gates, concretized health_check_criteria (unit_active, healthcheck_passes, api_response, log_stability)
   - Outcome: governance framework ready for future updates
</history>

<work_done>
**Files modified:**
- `/opt/projects/server/scripts/gen_server_state.py` — TZ changed to UTC; collect_container_flags() now tries container-<name>.service fallback
- `/opt/projects/server/CLAUDE.yaml` — LLM flags/target_flags updated to current Quadlet values; rules added for Quadlet-only, EnvironmentFile secrets, UTC policy, transient healthcheck filter
- `/opt/projects/server/blueprint.yaml` — removed :latest tag references, replaced "host networking" with "devforge-net", updated systemd layers to user-based, added breaking-change criteria
- `/opt/projects/server/handover.yaml` — reframed generate-systemd history as "historical/superseded", clarified Quadlet-only policy
- `/opt/projects/server/DevForge_v3.0_FINAL.md` — marked as historical document, corrected management policy from generate-systemd to Quadlet
- `/home/opc/.config/systemd/user/_legacy-generate-systemd/README.txt` — added archive annotation
- `/opt/workspace/seedling/docs/worklog.json` — added 2 new entries (latest ops finalization + final policy sync), kept rolling 3-entry window
- `/opt/workspace/seedling/docs/worklog_entries.json` — same updates for full history

**Files created (policy document):**
- `reference-tracking-policy.yaml` v1.1 — comprehensive version-tracking governance including stability gates, migration difficulty classification, 4-month refresh procedure, health verification criteria

**Tasks completed:**
- [x] Identified and consolidated hybrid systemd layers (system → user only for devforge)
- [x] Unified timezone policy (internal UTC, user-facing KST)
- [x] Applied fixed local tags + Pull=never across all containers
- [x] Established single EnvironmentFile secrets injection point
- [x] Cleaned legacy generated-systemd artifacts
- [x] Added transient healthcheck filtering
- [x] Resolved all document conflicts (removed contradictory language, marked historical items)
- [x] Validated final system state against all policies
- [x] Established reference-tracking and 4-month refresh governance

**Current state:**
- All 7 devforge units confirmed active (user systemd only)
- Timers running with UTC schedule (backup 20:00, restore-test 20:30 on day 1)
- No system-level devforge units
- Secrets: /home/opc/.config/devforge/secrets.env (chmod 600)
- gen_server_state.py timestamps in UTC (+00:00)
- Quadlet files with fixed tags, Pull=never, EnvironmentFile injection
- Zero failed units; no policy conflicts detected
</work_done>

<technical_details>
- **Quadlet standardization**: Replaces deprecated `podman generate systemd`; `.pod` + `.container` files in ~/.config/containers/systemd/ are sole source of truth for container orchestration. ServiceName= must be explicitly set to avoid generator mismatches.

- **User systemd consolidation**: devforge-backup/restore moved from /etc/systemd/system to ~/.config/systemd/user/ for easier operability and Linger=yes persistence. system-level units completely removed.

- **UTC internal policy**: gen_server_state.py now uses TZ=timezone.utc for all state/changelog timestamps. User-facing MOTD/CLI remains KST-converted. This eliminates timezone ambiguity in operational logs.

- **Fixed tag strategy**: Images use localhost/devforge-{service}:YYYY.MM.DD (e.g., localhost/devforge-llama:2026.05.13), never :latest. Pull=never enforces local-only, preventing surprise pulls during restarts.

- **Secrets injection**: Single EnvironmentFile=~/.config/devforge/secrets.env (chmod 600) replaces scattered env vars. LiteLLM config updated to reference os.environ/KEY syntax at runtime.

- **Transient healthcheck filtering**: gen_server_state.py now filters service discovery to exclude transient/conmon/run-[hex] patterns, eliminating noisy transient units from monitoring.

- **Breaking-change evaluation**: v1.1 reference-tracking-policy now includes explicit "No breaking change to Quadlet/Timer syntax" criterion in all version gates. Migration difficulty (LOW/MEDIUM/HIGH) now drives refresh-window planning.

- **Health verification concrete criteria**: Rollback decisions now use measurable checks: unit_active (systemctl is-active), healthcheck_passes (3 consecutive), api_response (200 in <5s), log_stability (no ERROR/CRITICAL in 2min).

- **Unresolved**: rss-monitor.service automation not yet deployed; currently reference tracking is manual. Next 4-month refresh (2026-09-01) will be first real test of policy application.
</technical_details>

<important_files>
- `/opt/projects/server/CLAUDE.yaml`
  - Why: Current ops rules entry point; read-first document for any operations session
  - Changes: Updated LLM flags to current Quadlet, added Quadlet-only/UTC/EnvironmentFile rules
  - Key sections: lines 44-64 (containers), 138-162 (rules)

- `/opt/projects/server/scripts/gen_server_state.py`
  - Why: Automated state collection + MOTD generation; enforces internal UTC policy
  - Changes: TZ=timezone.utc (line 29), collect_container_flags() fallback to container-<name>.service (lines 88-91)
  - Key sections: lines 1-40 (config), 59-85 (discover_services with transient filter), 256-338 (timestamp handling)

- `/home/opc/.config/containers/systemd/` (*.pod, *.container files)
  - Why: Quadlet definitions; sole source of runtime orchestration
  - Changes: ServiceName= explicit, EnvironmentFile=/home/opc/.config/devforge/secrets.env, Image=localhost/..., Pull=never applied to all containers
  - Key files: pod-ai-pod.pod (8, 17), pod-data-pod.pod (8, 17), container-postgres.container (8, 9, 12)

- `/home/opc/.config/systemd/user/devforge-backup.timer` and `devforge-restore-test.timer`
  - Why: User systemd timers for backup/restore; drive automation
  - Changes: OnCalendar now UTC-only (lines 5: *-*-* 20:00:00 UTC, *-*-01 20:30:00 UTC)
  - Key sections: OnCalendar (line 5), RandomizedDelaySec (line 6)

- `/opt/workspace/seedling/docs/worklog.json` and `worklog_entries.json`
  - Why: Operational tracking; enables rollback to checkpoints
  - Changes: Added 2 new entries (ops finalization + final policy sync); kept rolling 3-entry window
  - Key sections: entries[0-2] in both files for latest decisions

- `reference-tracking-policy.yaml` v1.1 (planned creation)
  - Why: Governance framework for 4-month version refresh cycles
  - Status: Document created in conversation; not yet persisted to filesystem
  - Key sections: stability_gates (with breaking-change criteria), migration_difficulty_levels, refresh_procedure (with health_check_criteria)
</important_files>

<next_steps>
**Immediate tasks:**
1. Create `reference-tracking-policy.yaml` v1.1 in filesystem (suggested path: ~/.config/devforge/reference-tracking-policy.yaml or /opt/projects/server/docs/)
2. Document current reference baseline (podman version, systemd version, image tags, dates) to reference-watchlist.md as v0 snapshot

**Optional but recommended:**
3. Implement reference-monitor.service/.timer for automated RSS polling (script path: ~/.config/devforge/bin/rss-monitor)
4. Set calendar reminder for 2026-09-01 (next 4-month refresh day)

**First operational test:**
- 2026-09-01: Execute refresh_procedure step-by-step with first set of collected updates; validate health_check_criteria work as expected

**Known blockers:**
- None; all immediate consolidation work complete. System is operationally stable and policy-compliant.
</next_steps>