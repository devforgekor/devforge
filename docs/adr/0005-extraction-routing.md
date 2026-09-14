# Extraction & Pipeline Routing Decision Record (ADR-0005)

## Status
Proposed (2026-09-14) — recommendation; not yet implemented.

## Context
`day_cycle` performs extract → verify → enrich → embed as a single heavy batch on a
4-core ARM CPU-only host using a local 8B model. ARM decode is memory-bandwidth-bound,
so this is inherently slow (`docs/reports/architecture-validation.md`). Meanwhile the
host already has cloud routes (OpenRouter RR, DeepSeek/Anthropic/Gemini proxies, Azure
Qwen). Industry practice for structured extraction is: deterministic-first, LLM-second,
strict schema, evidence binding, hybrid local/cloud routing, and asynchronous tiers.

Evidence: `docs/reports/industry-standard-comparison-20260914.md`.

## Decision
Re-architect extraction as a **tiered, routed, post-correction-oriented** pipeline:

1. **Deterministic prefilter** (regex/rules) handles high-certainty input first (~40–60%).
2. **Routine extraction** uses a small local model with **constrained decoding** (GBNF/
   XGrammar) — schema validity ≥99% — only for the ambiguous remainder.
3. **Hard cases escalate to cloud** small/frontier models with **structured outputs**
   (JSON Schema / `strict` Pydantic). Track **escalation rate** as a first-class metric.
4. Every extraction is validated: schema + **evidence binding** + confidence gate, with
   **one retry then quarantine** (fail-closed).
5. Heavy extraction runs in an **asynchronous batch tier**, never on the critical path.
6. Server's primary role shifts toward **verification/post-correction** (NLI grounding,
   dedup, entity resolution, provenance) where the agent/client already produced structure.

## Consequences
- Faster pipeline; frontier spend only for genuinely hard inputs.
- Requires an evalset (field-level precision/recall) and drift detection.
- Adds a routing/validation layer (`ports/inference` already exists; add clarity adapter + router).
- Supersedes the "single local-8B batch" assumption in `REFACTORING_PLAN` Phase 3.
