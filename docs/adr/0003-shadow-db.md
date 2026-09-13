# Shadow DB Decision Record (ADR-0003)

## Status
Proposed (Phase 1.5)

## Context
During refactoring, parallel execution (shadow) requires:
- Comparison of old vs new pipeline outputs
- No conflicts on production DB
- Deterministic replay of LLM responses

## Decision
- Shadow DB schema: `devforge_shadow` (separate schema in same PostgreSQL instance)
- `turns_shadow` table: read-only view from `turns` with `pipeline_state` column
- Replay fixture: `tests/fixtures/llm_recordings/` with 4 captured LLM responses
- `ExtractPipeline` supports `replay_mode=True` for fixture-based testing

## Consequences
- Phase 3.5 parallel validation can run both pipelines without DB conflicts
- LLM non-determinism eliminated in shadow tests via replay
- Shadow DB can be dropped after cutover
