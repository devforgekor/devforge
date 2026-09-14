# Alembic Migration Decision Record (ADR-0004)

## Status
Accepted (Phase 0)

## Baseline (2026-09-14)
The production DB had **no `alembic_version` table** (Alembic was never initialised),
so `alembic upgrade head` would collide with existing tables. Baseline procedure used:

1. `alembic/env.py` fixed to use the async engine (asyncpg, no psycopg2) and the
   correct `src/` path; autogenerate restricted to the 16 app tables and made
   **additive-only** via `include_object` (never proposes destructive drops).
2. `alembic upgrade head --sql` validated the fresh-DB chain (16 tables).
3. Live DB stamped: `alembic stamp head` → `alembic_version = 20260914_fix_initial_schema`.

> **Reconciled (2026-09-14).** The ORM (`domain/models.py`) was aligned to the live
> schema: BIGINT identity PKs, `REAL` floats, live nullability, FK `ondelete` semantics,
> index/unique-constraint names & opclasses, and removal of phantom FKs. `alembic check`
> against the live DB now reports **"No new upgrade operations detected."** (the only
> remaining notes are informational `gin_trgm_ops` expression-compare warnings).
> Migrations must be run where the DB is reachable (inside the `svc` pod / container).

## Context
Production database (`devforge_app`) has 16 tables. Refactoring adds columns:
- `turns.pipeline_state` (already exists in schema.sql, may need migration)
- `turns.source` (already exists)
- `review_facts.nli_llm`, `review_facts.nli_llm2` (new)

Schema changes must be non-breaking during 14-week migration.

## Decision
Use **expand/contract** migration pattern:

**Phase A (Expand)**: Add columns with defaults, never remove old columns
```sql
ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS nli_llm TEXT;
ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS nli_llm2 TEXT;
```

**Phase B (Contract)**: After 2-week monitoring, remove deprecated columns

Alembic migration `20260913_initial.py` captures current state. New migrations
will use `batch_alter_table` for SQLite compatibility during testing.

## Consequences
- Zero-downtime during Phase 3.5 parallel validation
- Rollback possible via `alembic downgrade`
- Schema matches `docs/specs/schema.sql` (source of truth)
- Live DB is now Alembic-tracked (`alembic current` → head); future changes use
  `alembic revision --autogenerate` + review (additive filter)
