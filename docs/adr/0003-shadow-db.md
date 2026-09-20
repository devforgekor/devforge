# Shadow DB Decision Record (ADR-0003)

## Status
Applied (Phase 3.5 prep) — `devforge_shadow` schema is applied to the instance (`turns_shadow` view + `review_facts_shadow`); diff harness `scripts/shadow_diff.py` landed and self-verified (copy 100 → diff=0). Awaiting a refactored-pipeline run for the 2-week diff=0 cutover gate.

## Context
During refactoring, parallel execution (shadow) requires:
- Comparison of old vs new pipeline outputs
- No conflicts on production DB
- Deterministic replay of LLM responses

## Decision
- Shadow DB schema: `devforge_shadow` — applied (`sql/shadow_schema.sql`)
- `turns_shadow`: read-only view over `public.turns` (applied)
- `review_facts_shadow`: separate output table for diffing (applied)
- Diff harness: `scripts/shadow_diff.py` — compares `public.review_facts` vs `devforge_shadow.review_facts_shadow` by `(turn_id, fact_index, extract_model)` over `fact_type/evidence/verdict/nli_verdict`; exit 0 iff diff=0 (Phase C gate). `--model` filter supported.
- Replay fixture: `tests/fixtures/llm_recordings/` (harness: `tests/fixtures/replay_harness.py`); `extract_llm.json` uses a `REPLAY_NEEDED` placeholder that `ExtractPipeline._replay_extract()` turns into deterministic synthetic facts
- `ExtractPipeline` supports replay via `DEVFORGE_LLM_REPLAY=1`

## Consequences
- Phase 3.5 parallel validation can run both pipelines without DB conflicts
- LLM non-determinism eliminated in shadow tests via replay
- Shadow DB can be dropped after cutover (`DROP SCHEMA devforge_shadow CASCADE;`)
- **Gap**: refactored pipeline (`application/orchestrator` + `pipeline_stages`) is still a stub, so the real shadow run producing `review_facts_shadow` for the diff=0 gate is pending Phase C implementation.
