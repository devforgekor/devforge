# Translation Quality in Multi-Model LLM Pipelines — Research Report

Prepared 2026-05-24. Sources: model-radar#5 (500K+ translation calls), model-radar#3, Interfaze IEEE CAI 2026, CLM/SPRL U Manchester 2025, MDCure ACL 2025, ICML 2025 contamination study.

## 1. Why Translation Quality Matters for Inter-Model Communication

In a multi-model pipeline like DevForge (Korean input → English internal processing → Korean output), every model along the chain consumes output produced by an upstream model. When the upstream output contains **language leakage**, **script contamination**, **think-tag artifacts**, or **empty/malformed content**, the downstream model inherits corrupted context. The damage compounds at each stage.

model-radar#5 identified this concretely across 500K+ translation calls:

| Failure Mode | Observed Rate | Downstream Impact |
|---|---|---|
| Empty content despite HTTP 200 | ~2% (GPT-OSS-120B) | Silent zero-scores, missing handover fields |
| Think-tag wrapping (Qwen3 class) | ~15% of Qwen outputs | max_tokens exhausted before real content |
| Script leakage (Hanja→Korean) | 6.7% (MiniMax M2.5) | Wrong-script characters in target language |
| Prompt-echo (instructions in output) | ~3% | Structural corruption of handoff format |
| Cross-directional contamination | Varies | Memorized patterns from one language bleed into another |

For DevForge specifically, the language pipeline (Korean→English→Korean) introduces **two translation surfaces** where quality degradation can occur:
1. **Inbound**: User Korean → AI internal English (misinterpretation risk)
2. **Outbound**: AI English response → User Korean (communication fidelity risk)

Additionally, the review pipeline (Qwen3-4B + Llama-3B → Phi-4-mini) introduces **three inter-model handoff surfaces** where structured output quality directly affects downstream arbitration accuracy.

## 2. Mapping model-radar Lessons to DevForge

### Lesson 1: Ping ≠ Functional → Relevance: HIGH

**model-radar finding**: GPT-OSS-120B returned HTTP 200 with valid JSON structure but empty `content: ""` on real prompts. Status ping said "UP", actual behavior was "broken."

**DevForge equivalent risk**: The 32B model (llama.cpp) returns HTTP 200 on `/health` but can produce empty or truncated responses under load. T07 Stage 4 and T10 Stage 4 both exhibited multi-hour generation times with connection drops.

**Recommendation**: Add a `verify` step before pipeline stages that sends a 1-token probe and validates non-empty response. The warmup check in `code_mod_pipeline.py` already does a variant of this — formalize it as `_verify_model_ready()`.

### Lesson 2: Think-tags Break Structured Output → Relevance: HIGH

**model-radar finding**: Qwen3 32B wraps responses in `<think>...</think>`, hitting max_tokens before producing actual answer. Detected via `ask()` with `max_tokens=20`.

**DevForge relevance**: Qwen3-4B (devforge-qwen container) uses `/no_think` flag already. But Llama-3B (8082) and Phi-4-mini (8081) do not have this protection. If any model in the review pipeline emits think-style reasoning into what should be structured JSON output, the downstream arbitrator (Phi-4-mini) receives malformed input.

**Recommendation**:
- Verify Qwen3-4B `/no_think` is working (inspect actual output for `<think>` tags)
- Add think-tag stripping as a post-processing step in `review_worker.py` for all models
- Track `uses_think_tags` as a model trait in the MODELS registry

### Lesson 5: Script Purity Validation → Relevance: MEDIUM-HIGH

**model-radar finding**: MiniMax M2.5 leaked Hanja into Korean (6.7%), Chinese into Amharic, Cyrillic into Greek. Structurally valid responses, wrong-script characters.

**DevForge relevance**: The Korean↔English language pipeline is vulnerable to:
- CJK Hanja leakage into English output (면접 being translated as "myeonjeob" vs "interview")
- English technical terms surviving untranslated in Korean output (OK, but needs consistency)
- Mixed-script responses confusing downstream models

**Recommendation**:
```python
# Script purity check for Korean output
import unicodedata

def script_purity(text: str, allowed: set) -> float:
    """Returns fraction of characters in allowed Unicode script ranges."""
    chars = [c for c in text if c.isalpha()]
    if not chars:
        return 1.0
    return sum(1 for c in chars if unicodedata.name(c, '').startswith(tuple(allowed))) / len(chars)

# Korean: Hangul + Latin (for technical terms) + CJK (limited)
KOREAN_ALLOWED = {'HANGUL', 'LATIN', 'CJK'}
```

### Lesson 6: Incremental Saves Prevent Data Loss → Relevance: ALREADY APPLIED

`code_mod_pipeline.py` already saves each task as `local32b_task{id}_{timestamp}.json`. The review pipeline also saves incrementally via `review_facts` DB inserts. No action needed.

### Lesson 8: Translation vs Evaluation Use Different Models → Relevance: HIGH

**model-radar finding**:

| Task | Best Models | Key Metric |
|---|---|---|
| Translation | MiniMax M2.5, Kimi K2, DeepSeek V3.1 | Throughput, script purity |
| Evaluation | Kimi K2, Qwen3 235B, Llama 4 Scout | Inter-rater agreement, parse rate |

**DevForge mapping**: The review pipeline (Qwen3-4B + Llama-3B → Phi-4-mini) conflates two different tasks under the same models:
- **Fact extraction** (translation-like): parse unstructured Korean dialogue → structured English facts
- **Fact verification** (evaluation-like): compare extracted facts, detect hallucinations

These have different quality requirements, but currently share the same small models. The 3-LLM debate added parallel extraction + arbitration, which is architecturally correct per Lesson 8.

**Recommendation**: Consider whether the extraction models (Qwen3-4B, Llama-3B) are best suited for Korean→English extraction, or whether a dedicated translation-capable model would produce higher-fidelity facts.

### Lesson 9: Back-Translation as Quality Check → Relevance: MEDIUM

**model-radar finding**: Translate → back-translate via **different** model → compute gloss overlap.

```python
English: "father, head of household"
  → Model A → German: "Vater, Haupt eines Haushalts"
  → Model B → English: "father, head of a household"
  → Overlap: 100%
```

**DevForge application**: Could verify Korean→English translation fidelity in the language pipeline:
```python
# Inbound check (Korean → English → Korean)
user_input_ko = "슬랙 연동 버그 수정했어"
internal_en = translate_to_english(user_input_ko)  # AI's internal understanding
back_ko = different_model_translate_to_korean(internal_en)
fidelity = compute_overlap(user_input_ko, back_ko)
# If fidelity < threshold → flag for clarification
```

**Current limitation**: DevForge doesn't have a dedicated translation model. The 32B could serve this role but latency (~1 tok/s) makes it impractical for real-time use. This is a TBD item for when DevForge has API access to a fast translation model.

## 3. Interfaze: Structured Context as Inter-Model Handoff Currency

The Interfaze paper (IEEE CAI 2026, arxiv 2602.04101) provides the strongest theoretical framework for what "translation quality" means in a multi-model pipeline. Key insight:

> Large LLMs never see raw pixels, waveforms, or full websites — they only see distilled `c(x)` structured context produced by specialist models.

The structured output schema uses four fields with **fixed token budgets**:
- `observations`: textual statements
- `entities`: typed spans
- `relations`: links between entities
- `provenance`: URLs, hashes, timestamps

**This is directly analogous to DevForge's inter-model handoff**: Qwen3-4B extracts facts → Phi-4-mini arbitrates. The quality of `c(x)` (extracted facts) determines the quality of the final decision. If the extractor produces a "translation" of user utterances into fact structures that is inaccurate, the arbitrator cannot recover.

### Applying Token Budgets to DevForge Handoff

The `review_facts` table could enforce structural quality:

```sql
-- Current (no budget constraint)
fact_text TEXT

-- Proposed (with quality guard)
fact_text TEXT CHECK (length(fact_text) BETWEEN 10 AND 500),
fact_entities TEXT,  -- JSON array of typed spans
fact_provenance TEXT -- source turn_id, timestamp, model
```

This enforces that extracted facts are neither too short (empty/truncated) nor too long (think-tag noise, prompt echo).

## 4. CLM/SPRL: Tag-Based Handover Protocols

The Collaborative Language Model whitepaper (U Manchester, 2025) proposes explicit `@handover`, `@path`, `@module`, `@beat` tags for inter-model coordination:

```
@module: extractor
@path: turn-parse
@handover: arbitrator
@beat: 1

[extracted facts in structured format]
```

For DevForge, this would make the review pipeline's inter-model communication **explicit and auditable** rather than implicit in the prompt chain. The current design (parallel extraction → compare_facts → Phi-4-mini arbitration) is architecturally sound but opaque — there's no structured handoff marker that allows debugging which model produced which fact.

**Recommendation**: Add `source_model` and `extraction_id` to `review_facts` rows (already partially done with `extract_model` column). Add `handover_version` to support protocol evolution.

## 5. Research Consensus: What Matters for Pipeline Quality

Synthesizing model-radar#5 + Interfaze + CLM/SPRL + MDCure + ICML 2025:

| Rank | Factor | Impact on Downstream | DevForge Status |
|---|---|---|---|
| 1 | **Structured output compliance** | Malformed JSON/empty fields break all downstream parsing | OK — review_facts has schema |
| 2 | **Script/language purity** | Wrong-script chars confuse downstream tokenizers | No check in place |
| 3 | **Token budget enforcement** | Truncated output loses information; bloated output wastes context | No enforcement |
| 4 | **Think-tag / artifact stripping** | Reasoning artifacts leak into structured fields | /no_think set, not verified |
| 5 | **Functional verification (not just ping)** | Models return 200 but empty content | Warmup check exists |
| 6 | **Provenance tracking** | Can't trace errors back to source model | extract_model column exists |
| 7 | **Incremental persistence** | Process crash loses all progress | Already applied |
| 8 | **Back-translation validation** | Catches semantic drift in translation | Not implemented |

## 6. Prioritized Recommendations for DevForge

### P0 (Do Now — Prevents Silent Corruption)

1. **Add script purity check to Korean output path** — Validate that user-facing Korean text contains only Hangul + Latin + allowed CJK. Flag mixed-script responses before delivery.
   - File: `lib/text_quality.py` (new)
   - Integration: `gen_motd.py` output path, Slack DM sender in review_worker.py
   - Effort: ~30 lines

2. **Verify Qwen3-4B /no_think effectiveness** — Sample 10 actual Qwen outputs from review_facts table, grep for `<think>`, confirm zero occurrences.
   - Command: `psql ... -c "SELECT fact_text FROM review_facts WHERE extract_model='qwen3-4b' ORDER BY created_at DESC LIMIT 10"`

### P1 (Do Soon — Improves Downstream Reliability)

3. **Add `_verify_model_ready()` to code_mod_pipeline.py** — Before each stage, send a 1-token probe with structured output requirement. If response is empty or `<think>`-wrapped, log and retry.
   - Replacement for current warmup-only check
   - Catches the T07/T10 Stage 4 class of failures earlier

4. **Add token budget to review_facts extraction** — Enforce min/max fact_text length at DB level. Reject facts shorter than 10 chars (empty/truncated) or longer than 500 chars (think-tag noise, prompt echo).
   - ALTER TABLE with CHECK constraint
   - Update review_worker.py to filter before INSERT

### P2 (Later — Architecture Improvement)

5. **Back-translation fidelity check** — When DevForge has API access to a fast translation model, verify inbound Korean→English translation quality by back-translating internal English understanding and comparing with original Korean input. Flag turns with fidelity < 0.7 for human review.

6. **Structured handoff tags** — Adopt `@handover` / `@module` style tags in review_worker.py's inter-model prompts for auditability.

## 7. Key Insight

The 2025-2026 research converges on one finding: **structured output quality at each pipeline stage matters more than the downstream model's capability.** A perfect arbitrator model cannot recover from malformed extractor output. The quality floor is set by the weakest link in the chain.

For DevForge, this means investment in output quality validation (script purity, token budgets, think-tag stripping) at the Qwen3-4B / Llama-3B extraction stage yields compound benefits downstream — better facts → better arbitration → more accurate handover → better session context for all future AI agents.

This aligns with the user's intuition that "improving user-facing communication models improves all downstream LLM communication." The causal chain is: extractor output quality → structured fact fidelity → arbitrator accuracy → handover document quality → session context completeness → next agent comprehension.

Sources:
- [model-radar#5 — Lessons learned: real-world translation & evaluation across 2 projects](https://github.com/srclight/model-radar/issues/5)
- [model-radar#3 — Expose judge tools + batch_run and verified-alive scan](https://github.com/srclight/model-radar/issues/3)
- [Interfaze: The Future of AI is built on Task-Specific Small Models (IEEE CAI 2026)](https://ar5iv.labs.arxiv.org/html/2602.04101)
- [CLM Whitepaper — From LLMs to a Structural Collaboration Language Protocol (U Manchester 2025)](https://research.manchester.ac.uk/en/publications/collaborative-language-model-clm-whitepaper-from-llms-to-a-struct-2/)
- [MDCure: A Scalable Pipeline for Multi-Document Instruction-Following (ACL 2025)](https://aclanthology.org/2025.acl-long.1418/)
- [Overestimation in LLM Evaluation: Data Contamination's Impact on Machine Translation (ICML 2025)](https://icml.cc/virtual/2025/poster/45530)
