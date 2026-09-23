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
- **Gap (2026-09-23)**: `application/orchestrator.py`(골격)·`pipeline_stages/extract`는 존재하나, devforge가 소유하는 **embed** 단계는 미구현(Phase 3, D6=A) → `review_facts_shadow`를 생성하는 실제 shadow run과 2주 diff=0 게이트는 Phase 3 착수 후.
