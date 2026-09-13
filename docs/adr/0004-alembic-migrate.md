# Alembic Migration Decision Record (ADR-0004)

## Status
Accepted (Phase 0)

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
