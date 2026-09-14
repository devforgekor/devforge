# Shadow DB Decision Record (ADR-0003)

## Status
Partial (Phase 1.5) — shadow DB SQL artifact + replay fixtures landed; schema not yet applied to the instance.

## Context
During refactoring, parallel execution (shadow) requires:
- Comparison of old vs new pipeline outputs
- No conflicts on production DB
- Deterministic replay of LLM responses

## Decision
- Shadow DB schema: `devforge_shadow` — artifact ready in `sql/shadow_schema.sql` (not yet applied)
- `turns_shadow`: read-only view over `public.turns` (in `sql/shadow_schema.sql`)
- `review_facts_shadow`: separate output table for diffing (in `sql/shadow_schema.sql`)
- Replay fixture: `tests/fixtures/llm_recordings/` (harness: `tests/fixtures/replay_harness.py`); `extract_llm.json` uses a `REPLAY_NEEDED` placeholder that `ExtractPipeline._replay_extract()` turns into deterministic synthetic facts
- `ExtractPipeline` supports replay via `DEVFORGE_LLM_REPLAY=1`

## Consequences
- Phase 3.5 parallel validation can run both pipelines without DB conflicts
- LLM non-determinism eliminated in shadow tests via replay
- Shadow DB can be dropped after cutover (`DROP SCHEMA devforge_shadow CASCADE;`)
- **Gap**: `sql/shadow_schema.sql` must be applied to the instance before Phase 3.5
