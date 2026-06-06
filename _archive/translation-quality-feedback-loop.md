# Translation Quality Feedback Loop — Research Report

Prepared 2026-05-24. Research question: Can `source_lang` + cross-lingual embedding evolve into a self-improving translation quality loop?

## 1. The Core Question

> Does adding `source_lang` one column actually enable continuous improvement, or does it just enable measurement?

The research answer is clear: **measurement alone does not improve quality**. A column lets you tag language. A feedback loop lets you act on what you measure. The 2025-2026 research converges on a specific architecture pattern for self-improving translation quality.

## 2. State of the Art: 3 Key Projects

### 2.1 TEaR — Translate → Estimate → Refine (NAACL 2025)

**Repo**: [fzp0424/self_correct_mt](https://github.com/fzp0424/self_correct_mt) (50 stars, published NAACL 2025)

Architecture:
```
Translate → Estimate → Refine → (repeat)
   ↑                              ↓
   └───── error feedback loop ────┘
```

Key findings:
- Same LLM performs both translation AND self-evaluation simultaneously
- Uses only self-feedback — no external models or reference translations required
- Continuous error reduction across iterative rounds
- MQM (Multidimensional Quality Metrics) scores improve each round
- Different estimation strategies yield different correction effectiveness

**Relevance to DevForge**: The same model (Qwen3-4B or 32B) that translates Korean→English can evaluate its own output and correct errors. This is the "self-refinement" pattern, and it requires zero additional infrastructure.

### 2.2 COMET / xCOMET — Reference-Free Quality Estimation

**Repo**: [Unbabel/COMET](https://github.com/Unbabel/COMET) (752 stars, active 2026-04-21)

Three model families:

| Model | Input | Output | Params |
|-------|-------|--------|--------|
| `wmt22-comet-da` | source + MT + reference | score 0-1 | XLM-R 560M |
| `wmt23-cometkiwi-da-xxl` | source + MT (NO reference) | score 0-1 | XLM-R-XXL 10.7B |
| `Unbabel/XCOMET-XXL` | source + MT (NO reference) | score + error spans + severity | 10.7B |

Key insight for DevForge: **COMETKiwi and xCOMET are reference-free**. They predict translation quality without needing a human translation as ground truth. This is exactly what DevForge needs — we don't have reference translations for user input.

xCOMET-XXL would be impractical on 22GB RAM, but the distilled version `XCOMET-lite` (278M params, mdeberta-v3-base, ~38x smaller) could potentially run on the ARM server.

### 2.3 DCSQE — Distribution-Controlled Synthetic QE (2025)

**Repo**: [NJUNLP/njuqe](https://github.com/NJUNLP/njuqe) (12 stars, updated 2025-09)

Key innovation: Synthetic QE data generation with controlled distribution to address domain shift. Outperforms COMETKiwi in both supervised and unsupervised settings.

**Relevance to DevForge**: The "unsupervised" mode is relevant — it means the QE model can be bootstrapped without labeled training data, which matches DevForge's situation (no human-translated references available).

## 3. The Feedback Loop Architecture

The 2025-2026 literature converges on a 3-stage architecture for self-improving translation quality:

```
┌─────────────────────────────────────────────────┐
│              Stage 1: MEASURE                    │
│  source_lang column → tag turns by language     │
│  cross-lingual embedding → cosine_similarity    │
│  COMETKiwi-light or LaBSE → QE score            │
│  Output: per-turn quality score (0-1)           │
└──────────────────────┬──────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────┐
│              Stage 2: DIAGNOSE                   │
│  Weekly bottom-20 turns by quality score        │
│  xCOMET pattern → error span detection          │
│  Classify errors:                                │
│    - Script leakage (Hanja→Korean)               │
│    - Semantic drift (meaning changed)            │
│    - Missing/added info (over/under translation) │
│    - Terminology inconsistency                   │
│  Output: error type distribution per period      │
└──────────────────────┬──────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────┐
│              Stage 3: CORRECT                     │
│  TEaR pattern → feed error back to LLM           │
│  Prompt revision (system prompt update)          │
│  Terminology glossary update (DB)                │
│  Model selection (try different model for ko→en) │
│  Output: improved translation for next cycle     │
└─────────────────────────────────────────────────┘
```

## 4. DevForge-Specific Architecture

Given the ARM server constraints (22GB RAM, no GPU), the most practical path is:

### Stage 1 — Lightweight QE (Tier 2)

```python
# lib/translation_quality.py
# Uses LaBSE (multilingual sentence embeddings) or Gemini embedding-embedding-001
# Both are already available — Gemini via API, LaBSE could run locally (small model)

def estimate_translation_quality(source_ko: str, target_en: str) -> float:
    """Reference-free quality estimation using cross-lingual embeddings."""
    vec_ko = embed(source_ko)  # 768-dim via Gemini embedding-embedding-001
    vec_en = embed(target_en)
    return cosine_similarity(vec_ko, vec_en)

def weekly_quality_report(db_conn) -> dict:
    """Sample recent turns, score them, return bottom-20 for review."""
    ...
```

### Stage 2 — Error Classification (Tier 2-3)

Use the existing review_worker.py pattern for error analysis:

```python
# Extends review_worker.py fact extraction pipeline
# Instead of extracting facts, it classifies translation errors
ERROR_CLASSIFY_PROMPT = """Analyze this Korean→English translation:

Korean source: {source_ko}
English translation: {target_en}
Quality score: {score}

Identify specific errors (MQM typology):
1. Accuracy: mistranslation, omission, addition, untranslated
2. Fluency: grammar, spelling, style
3. Terminology: wrong term for domain

Output: JSON array of errors with spans and severity (minor/major/critical)."""
```

### Stage 3 — Correction Loop (Tier 3)

TEaR pattern adapted for DevForge:

```python
def refine_translation(source_ko: str, initial_en: str, errors: list) -> str:
    """Feed errors back to LLM for self-correction."""
    prompt = f"""The following Korean→English translation has errors:

Korean: {source_ko}
Current translation: {initial_en}
Errors found: {json.dumps(errors)}

Provide a corrected translation. Output ONLY the corrected English text."""
    return call_32b(prompt)
```

## 5. Schema Evolution Plan

Not just one column — a phased approach:

```sql
-- Tier 1 (now): Minimal
ALTER TABLE turns ADD COLUMN source_lang text;
-- Populate: infer from user_turn Unicode range (Hangul → 'ko', Latin → 'en')

-- Tier 2 (with cross-lingual embedding):
ALTER TABLE turns ADD COLUMN translation_score float;
-- Computed: cosine_similarity(embed_ko, embed_en)
-- Index for weekly bottom-N queries
CREATE INDEX idx_turns_tscore ON turns (translation_score)
    WHERE translation_score IS NOT NULL;

-- Tier 3 (with error classification):
CREATE TABLE translation_errors (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    turn_id uuid REFERENCES turns(id),
    error_type text,        -- 'accuracy', 'fluency', 'terminology'
    error_span_start int,   -- char offset in target text
    error_span_end int,
    severity text,          -- 'minor', 'major', 'critical'
    created_at timestamptz DEFAULT now()
);
```

## 6. What the Research Says About "Does It Actually Improve?"

| Source | Finding |
|--------|---------|
| TEaR (NAACL 2025) | MQM scores improve each iteration — the loop works |
| WALAR (2026) | RL with QE reward signal improves 1400+ language directions |
| WMT 2025 Consensus | Reference-free QE good at system-level ranking, weaker at segment-level |
| COMETKiwi (Unbabel) | Strong across low-resource pairs, but not a sole decision-maker |
| Document-Level MT (2026) | LaBSE cosine similarity filtering + iterative refinement reduces hallucinations |

The **consensus**: feedback loops work, but they work better at the system level (detecting that quality is trending down) than at the individual segment level (correcting a specific bad translation). DevForge should aim for both: system-level monitoring as the primary signal, with individual correction as the secondary improvement mechanism.

## 7. Practical Recommendations

### P0: Minimal Viable Feedback Loop (Tier 1-2, this month)

1. Add `source_lang` column, backfill from Unicode analysis of `user_turn`
2. Write `lib/translation_quality.py` with `cosine_similarity(embed_ko, embed_en)`
3. Add `translation_score` column, populate for recent turns (can reuse Gemini embeddings)
4. Weekly manual check: bottom-10 turns by score, human spot-check

### P1: Automated Error Classification (Tier 2, 2-4 weeks)

5. Extend review_worker.py pattern for translation error classification
6. `translation_errors` table
7. Weekly auto-report: error type trends, worst-affected language directions

### P2: Self-Correction (Tier 3, 1-3 months)

8. TEaR-style refinement: feed classified errors back to 32B for correction
9. Prompt revision based on error patterns (update language pipeline system prompts)
10. Terminology glossary (DB table + LLM-assisted curation from Wikipedia Q-items)

### P3: Offline COMETKiwi (Tier 3+, when GPU available)

11. Deploy XCOMET-lite (278M params) for reference-free QE — more calibrated than raw cosine similarity
12. Replace cosine similarity with MQM-correlated scores

## 8. The One-Column Answer

Your intuition is correct but incomplete. `source_lang` is the **catalyst**, not the solution:

```
source_lang (1 column)
    → enables quality measurement (cosine similarity)
    → enables error diagnosis (bottom-N sampling by score)
    → enables correction (TEaR self-refinement)
    → enables monitoring (weekly quality trend dashboard)
    → quality ↑ over time
```

Without the measurement→diagnosis→correction loop, `source_lang` is just a tag. With it, the system gains a continuous improvement mechanism that gets better every week.

The minimal investment (1 column + ~100 lines of Python for cosine similarity scoring) unlocks the entire feedback architecture. That's the right place to start.

Sources:
- [TEaR: Self-Refinement for LLM Translation — fzp0424/self_correct_mt](https://github.com/fzp0424/self_correct_mt) (NAACL 2025)
- [COMET/xCOMET: Neural MT Evaluation — Unbabel/COMET](https://github.com/Unbabel/COMET) (752 stars, active 2026)
- [NJUQE: Quality Estimation Toolkit — NJUNLP/njuqe](https://github.com/NJUNLP/njuqe)
- [WALAR: RL for Multilingual Translation — arXiv:2603.13045](https://arxiv.org/abs/2603.13045)
- [Document-Level MT via Filtered Synthetic Corpora — arXiv:2603.22186](https://arxiv.org/abs/2603.22186)
- [model-radar#5: Real-world Translation Lessons](https://github.com/srclight/model-radar/issues/5)
