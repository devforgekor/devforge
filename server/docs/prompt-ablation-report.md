# Prompt Ablation Analysis — DevForge 32B Code Mod Pipeline

## 1. GitHub Ecosystem Research

| Tool | Stars | Approach | Key Metric |
|------|-------|----------|------------|
| [DSPy](https://github.com/stanfordnlp/dspy) | 22k+ | MIPROv2 Bayesian optimization over instruction candidates | Task success rate |
| [LLMLingua](https://github.com/microsoft/LLMLingua) | 3.7k | BERT classifier trained via GPT-4 distillation, token-level compression | 20x compression |
| [PromptBench](https://github.com/microsoft/promptbench) | MS | 7 adversarial attack types, attention visualization, causal sensitivity | Prompt sensitivity score |
| [GEPA](https://github.com/gepa-ai/gepa) | — | LLM reflection on execution traces, evolutionary Pareto search | 90x cheaper than frontier |
| [Token Budget Negotiator](https://github.com/dakshjain-1616/token-budget-negotiator) | — | Greedy section ablation with rubric scoring | Token savings vs. quality threshold |
| [Pruner](https://github.com/heikki-laitala/pruner) | — | tree-sitter call graphs, structural code indexing | 15-62% cost reduction |
| [Promptomatix](https://github.com/SalesforceAIResearch/promptomatix) | Salesforce | L = L_perf + λ·exp(-λ·prompt_length) | 40-50% length reduction @ ~99% peak |

**Pattern**: Production tools all use **greedy section ablation** (drop one section → score quality → repeat) + **LLM judge** for automated quality assessment. No project does "prompt-output correlation counting" the way we discussed — but the ablation approach achieves the same goal more directly.

## 2. Current Pipeline Prompt Structure

### Token Budget Per Stage (T02 real data)

| Stage | Prompt Tokens | Output Tokens | Ratio | Dominant Component |
|-------|--------------|---------------|-------|--------------------|
| 1 ANALYZE | 5,454 | 92 | 59:1 | Code file (~4,500 tok) |
| 2 PLAN | 5,528 | 151 | 37:1 | Code file + Stage 1 JSON |
| 3 IMPLEMENT | 5,450 | 121 | 45:1 | Code file + Stage 2 JSON |
| 4 PACKAGE | 384 | 301 | 1.3:1 | Diff text only |
| **Total** | **16,816** | **665** | **25:1** | |

### Field Utilization Analysis (T02)

Stage 1 — ANALYZE (5 prompt fields → 5 output fields, 100% utilized):
```
Prompt: affected_sections, change_type, dependencies, risk_assessment, notes
Output: all 5 populated ✓
```

Stage 2 — PLAN (6 prompt fields → 6 output fields, 100% utilized):
```
Prompt: approach, steps, files_to_modify, estimated_lines_changed, backward_compatible, edge_cases
Output: all 6 populated ✓
```

Stage 4 — PACKAGE (6 prompt fields → 6 output fields, 100% utilized):
```
Prompt: task, analysis, diff, rationale, confidence, review_points
Output: all 6 populated ✓
```

**Finding**: Zero unused instruction fields. All prompted output fields are consistently populated. The issue is NOT unnecessary instructions — it's the **massive code file** being included 3 times (Stages 1-3).

## 3. Ablation Candidates

### A. CODE REPETITION (highest impact — ~13,500 tokens saved)

The code file (`slack_operator.py`, ~4,500 tok) is included in Stages 1, 2, 3. Stages 2-3 could reference Stage 1's analysis instead of re-reading the full file.

**Test**: Stage 2/3 receive only the affected functions (extracted from Stage 1's `affected_sections`), not the full file.

**Risk**: If Stage 1 misses an affected section, Stages 2-3 can't recover.

**Estimated savings**: 2 × 4,500 = 9,000 tokens across Stages 2-3 (~55% total reduction).

### B. SYSTEM_32B REDUNDANCY (low impact — ~50 tokens)

SYSTEM_32B ("You are a CODE PREPROCESSOR...") is sent before every stage. It's 50 chars (~17 tok). Once would suffice via conversation history.

**Test**: Send SYSTEM_32B only before Stage 1.

**Estimated savings**: 3 × 17 = 51 tokens (negligible).

### C. STAGE-SPECIFIC VERBOSITY (medium impact — ~200-400 tokens)

Stage 2 prompt includes "Based on the Stage 1 analysis, design the minimal change approach" — redundant given the ANALYSIS field. Stage 3's "Be minimal and surgical" echoes SYSTEM_32B.

**Test**: Remove redundant framing sentences, keep only format specs.

**Estimated savings**: ~200-400 tokens.

### D. REQUEST REPETITION (medium impact — ~150 tokens)

`task_desc` (~74 tokens for T02) is included in Stages 1, 2, and 4. Stage 4 could omit it since the diff already captures the change.

**Test**: Drop REQUEST from Stage 4.

**Estimated savings**: ~74 tokens.

## 4. Recommended Implementation Priority

| # | Change | Token Savings | Risk | Implement |
|---|--------|--------------|------|------------|
| 1 | **A. Code → slices in Stage 2/3** | ~9,000 (55%) | Medium | First |
| 2 | **D. Drop REQUEST from Stage 4** | ~74 | Low | Easy win |
| 3 | **C. Trim redundant framing** | ~200-400 | Low | Quick |
| 4 | **B. SYSTEM_32B once** | ~51 | Very Low | Negligible |

## 5. Automated Ablation Framework (Recommended Approach)

Rather than manual one-off pruning, implement a **section-level ablation harness**:

```python
# Ablation config per stage
STAGE1_SECTIONS = {
    "system": SYSTEM_32B,          # 17 tok
    "instructions": STAGE1_ANALYZE.split("{code}")[0],  # 180 tok
    "format_spec": "{...JSON schema...}",                 # 100 tok
    "code": "{code}",                                     # 4500 tok
    "task": "{task}",                                     # 74 tok
}

# Run: drop one section → score output quality → keep if no degradation
```

**Quality scoring**: Use Stage 4 `confidence` field (0.95 baseline) + field population rate. If both stay at baseline, the removed section was unnecessary.

**Integration**: Token Budget Negotiator MCP server could be wired directly into Claude Code for interactive ablation during development.

## 6. References

Full references with descriptions: [references.md](references.md#prompt-optimization--ablation)

- [DSPy](https://github.com/stanfordnlp/dspy) — MIPROv2, GEPA, COPRO
- [LLMLingua](https://github.com/microsoft/LLMLingua) — Token compression (20x)
- [PromptBench](https://github.com/microsoft/promptbench) — Sensitivity analysis
- [GEPA](https://github.com/gepa-ai/gepa) — Evolutionary prompt optimization
- [Token Budget Negotiator](https://github.com/dakshjain-1616/token-budget-negotiator) — Section ablation + rubric
- [Pruner](https://github.com/heikki-laitala/pruner) — tree-sitter code indexing
- [Promptomatix](https://github.com/SalesforceAIResearch/promptomatix) — Cost-aware optimization
- [hone](https://github.com/twaldin/hone) — CLI mutation engine

## 7. Conclusion

The 32B 4-stage pipeline has **well-designed prompts** (100% field utilization across all stages). The optimization opportunity is not in removing instructions — it's in **reducing code repetition across stages**. The single highest-impact change: send only affected function slices (from Stage 1 analysis) to Stages 2 and 3 instead of the full file. This alone would cut total prompt tokens by ~55% (~16,800 → ~7,800), roughly halving pipeline wall-clock time.
