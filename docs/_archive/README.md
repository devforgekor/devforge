# DevForge Docs Archive

Archived docs — superseded by code/DB as SSOT.

## Archival Log

| File | Archived | Why | Replaced By |
|------|----------|-----|-------------|
| `eval_report_20260603.md` | 2026-06-06 | Experiment data in `experiment_registry` DB | `cli.py experiment list` |
| `optimization-baseline.yaml` | 2026-06-06 | Experiment data in `experiment_registry` DB | `cli.py experiment list` |
| `optimization-concurrent.yaml` | 2026-06-06 | Experiment data in `experiment_registry` DB | `cli.py experiment list` |
| `optimization-phase1-results.yaml` | 2026-06-06 | Experiment data in `experiment_registry` DB | `cli.py experiment list` |
| `optimization-summary.yaml` | 2026-06-06 | Experiment data in `experiment_registry` DB | `cli.py experiment list` |
| `plan-experiment-registry.md` | 2026-06-06 | All steps implemented (bench_llm.py + cli.py + DB) | `scripts/bench_llm.py`, `scripts/cli.py` |
| `tasks.yaml` | 2026-06-06 | Tasks tracked in DB | `cli.py task list` |

### plans/
| File | Archived | Why | Replaced By |
|------|----------|-----|-------------|
| `naming-audit.md` | 2026-06-06 | Naming violations fixed, lint rules enforce | `scripts/lib/lint_rules.py`, `cli.py lint` |
| `tasks.yaml` | 2026-06-06 | Tasks tracked in DB | `cli.py task list` |
| `master-plan.md` | 2026-06-06 | Stale (May 2026), code + infra.md are SSOT | Code + `infrastructure.md` |
| `migration-insights.md` | 2026-06-06 | Migration completed, code is current | Code |
| `implementation-notes.md` | 2026-06-06 | Pending plan, never implemented | N/A (abandoned) |

### specs/
| File | Archived | Why | Replaced By |
|------|----------|-----|-------------|
| `system-design.yaml` | 2026-06-06 | Redundant — code + `infrastructure.md` + `schema.sql` cover all | `infrastructure.md`, `docs/specs/schema.sql` |

## Deletion Criteria
- Do NOT delete unless confirmed unreferenced by any code path for 90+ days.
- Check `grep -r 'ARCHIVED_FILE'` before deleting.
- `tasks.yaml` variants are archived but kept for reference — DB migration ensured no data loss.
