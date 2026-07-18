# Extraction Quality Issues (2026-07-17)

## Test Setup
- Model: Qwen3-8B-Q8_0 (day-extractor, ctx=4096)
- Pipeline: extract → embed(NLI+judge) → refine → store
- Source: fb4bcc06 (text=927 chars, ETL performance/API/SSL narrative)
- Facts extracted: 12

---

## Issue 1: Causal Direction Reversal (caused_by)

**Fact #9:**
```
Source: "SSL certificate renewal failed silently causing the API to serve expired certs for 6 hours."
Extracted: SSL certificate renewal → caused_by → API to serve expired certs for 6 hours
```
Meaning: "SSL certificate renewal was caused by API serving expired certs" — **WRONG**.

**Root cause**: Model uses `caused_by` without checking whether subject is cause or effect. The predicate naming convention (`caused`/`caused_by`) is a passive/active distinction the model doesn't reliably enforce.

**Related**: Fact #1 (`ETL pipeline processing time → caused_by → missing index on event_logs`) got it correct — inconsistent behavior.

---

## Issue 2: Numerical Value Omission

| Source text | Expected | Extracted |
|---|---|---|
| "503 errors for **12%** of requests" | `object: "503 errors for 12% of requests"` | `object: "503 errors"` (12% missing) |
| "Error rate dropped to **0.1%**" | Include in object/qualifier | Not included |
| "increased from **45 minutes** to 3 hours" | Include baseline | Not included |

---

## Issue 3: Subject-Object Tautology (resolved_via)

**Fact #2:**
```
Source: "A composite index on (created_at, event_type) resolved it"
Extracted: composite index on (created_at, event_type) → resolved_via → composite index on (created_at, event_type)
```
Subject and object are IDENTICAL — the predicate `resolved_via` should connect issue → solution, not repeat the same entity.

**Related**: Fact #5, #8 have weird subject phrases instead of clean entity names.

---

## Issue 4: Subject Drift (entity → phrase)

**Fact #8:**
```
Source: "Fix increased pool to 200 and added HikariCP."
Extracted: pool and HikariCP → resolved_via → pool increased to 200
```
Subject should be "Connection pool to the inventory service" but the model used the verb phrase "pool and HikariCP" as subject.

---

## Issue 5: NLI Verify Fails Semantic Checks

Despite enhanced NLI prompt with causal direction + numerical checks, ALL 12 facts passed as ENTAILMENT.

| Error type | Should flag | NLI result |
|---|---|---|
| Reversed causal direction (#9) | CONTRADICTION | ENTAILMENT ❌ |
| Subject tautology (#2) | CONTRADICTION or NEUTRAL | ENTAILMENT ❌ |
| Missing numbers (#6 object) | NEUTRAL | ENTAILMENT ❌ |

---

## Attempted Fixes (already applied)

1. **Prompt-level**: Added `CAUSAL DIRECTION — critically important` section with `caused`/`caused_by` examples to both user/text 8B prompts
2. **Numerical completeness**: Added "Include ALL numerical values: percentages (12%), durations (6 hours), baselines" to OBJECT rule
3. **NLI verify enhancement**: Added steps for causal direction check and numerical value exact match

**Result**: Partial improvement (6 hours added, subject fixed for #11) but core issues persist.
