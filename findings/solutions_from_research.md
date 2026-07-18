# Web Research-Based Solutions (2026-07-17)

## Issue 1: Causal Direction Reversal

**Research**: ReCITE (ACL 2026) — direction reversals <1.1% when models identify correct relations. But our model picks wrong _predicate name_ (`caused_by` vs `caused`).

**Best practice**: ClawMem v0.8.5 uses a tight 13-predicate vocabulary enforced at parser level. Hindsight project: explicit per-predicate documentation ("caused_by: this fact WAS CAUSED BY the target").

**Action**:
- Post-extraction causal direction verifier — extract `caused`/`caused_by` facts, parse evidence with regex `(X) caused (Y)` / `(Y) caused by (X)`, confirm subject aligns.

---

## Issue 2: Numerical Value Omission

**Research**: MINEA (2024) — iterative extraction improves completeness. Extracting Numeric Assertions (IJCNLP 2025) — fine-tuned 280M model outperforms GPT-4 zero-shot for numerical extraction.

**Action**:
- Add `_validate_numerical_completeness(evidence, object)` — extract all numbers/percentages/durations from evidence, verify each appears in object or qualifiers.

---

## Issue 3: Subject-Object Tautology

**Research**: Validity-Fidelity Gap (2025) — structural validity (valid JSON) can be perfect while content fidelity silently fails. The model knows the format but "forgets" content constraints.

**Action**:
- For `resolved_via` predicate: add post-check `subject != object`. If identical, rewrite using evidence parsing (subject = issue entity, object = solution entity).

---

## Issue 4: Subject Drift (entity → phrase)

**Research**: ODKE+ (2025) — ontology-guided prompting restricts entity types. ClawMem — canonical entity IDs from source text.

**Action**:
- Add subject grounding: verify each subject exists verbatim in source text. Flag "pool and HikariCP" phrases as non-entities.

---

## Issue 5: NLI Verify Blindness

**Research**: Financial report triplet extraction (2026) — hybrid regex+LLM judge reduces hallucination false positives 65.2% → 1.6%. KGCQual — dependency-parse ideal graph comparison is more reliable than LLM judge.

**Action**:
- Extract key terms from evidence (numbers, entity names, predicate), match against source deterministically. If evidence numbers ≠ source numbers → CONTRADICTION.
