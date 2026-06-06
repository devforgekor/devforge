# Language: English only — machine-readable per llm-common-rule.md §Communication
# Naming Audit — Abbreviations & Model Names Hardcoded in Identifiers
# 2026-06-06 | Status: audit complete, fix pending

## Principle

> Function names, variable names, and file names must clearly communicate their role.
> Abbreviated names that neither human nor machine can understand are unacceptable.
> After 6 months, even the original author forgets what cryptic abbreviations meant.
> Model names must never appear in code identifiers — when the model changes, all those names become wrong.

---

## 1. Model Names in File Names (13 files)

All violate "no model name in identifiers" rule. When model changes, file name becomes misleading.

| File | Problem | Suggested |
|------|---------|-----------|
| `test_27b_optimization.py` | "27b" hardcoded | `test_verify_optimization.py` |
| `test_27b_verify.py` | "27b" hardcoded | `test_verify_pipeline.py` |
| `test_27b_verify_opt.py` | "27b" hardcoded | `test_verify_optimized_params.py` |
| `test_30b_q4ks_p.py` | "30b", "q4ks" hardcoded | `test_proposer_quantization.py` |
| `test_30b_q4.py` | "30b" hardcoded | `test_q4_quantization.py` |
| `test_3b_verify.py` | "3b" hardcoded | `test_small_verify.py` |
| `test_verify_14b_q8.py` | "14b" hardcoded | `test_verify_reflector.py` |
| `test_verify_7b_q8.py` | "7b" hardcoded | `test_verify_proposer.py` |
| `test_qwen3_4b_comprehensive.py` | "qwen3", "4b" hardcoded | `test_operator_comprehensive.py` |
| `test_extract_3b_q8.py` | "3b" hardcoded | `test_extract_quantized.py` |
| `test_dsv2_verify_mcp.py` | "dsv2" hardcoded | `test_verify_mcp.py` |
| `test_r_q8_comparison.py` | "r", "q8" hardcoded | `test_reflector_comparison.py` |
| `test_30b_q4ks_p.py` | duplicate of above | |

---

## 2. Model Names in Function Names (8 functions)

| File | Current | Suggested |
|------|---------|-----------|
| `night_pipeline.py` | `phase_4_verify_27b()` | `phase_4_verify()` |
| `night_pipeline.py` | `phase_5_verify_32b()` | `phase_5_test_verify()` |
| `rubric_experiment.py` | `run_verify_27b()` | `run_verify_primary()` |
| `rubric_experiment.py` | `run_verify_32b()` | `run_verify_test()` |
| `code_mod_pipeline.py` | `run_32b_4stage()` | `run_code_mod_4stage()` |
| `code_mod_pipeline.py` | `warmup_32b()` | `warmup_code_model()` |

---

## 3. Model Names in Variable Names (10+ variables)

| File | Current | Suggested |
|------|---------|-----------|
| `night_pipeline.py` | `v27b` | `verify_result` |
| `rubric_experiment.py` | `v27b_result`, `r1_v27b`, `r2_v27b` | `verify_result`, `r1_verify`, `r2_verify` |
| `rubric_experiment.py` | `v32b`, `r1_v32b`, `r2_v32b` | `test_verify`, `r1_test_verify`, `r2_test_verify` |
| `test_3b_verify.py` | `exp_30b` | `experiment_data` |

---

## 4. Single-Letter Abbreviations (P, R, J)

Used as module constants AND dictionary keys AND log prefixes. Too overloaded — machine cannot trace without context.

| File | Current | Meaning | Suggested |
|------|---------|---------|-----------|
| `prj_cycle.py` | `P_MODEL` | role key for proposer | `PROPOSER_ROLE` |
| `prj_cycle.py` | `R_MODEL` | role key for reviewer/reflector | `REFLECTOR_ROLE` |
| `prj_cycle.py` | `J_MODEL` | role key for judge | `JUDGE_ROLE` |
| `prj_cycle.py` | `p_findings` | proposer findings | `proposer_findings` |
| `prj_cycle.py` | `r_verdicts` | reflector verdicts | `reflector_verdicts` |
| `prj_cycle.py` | `p_r`, `r_r`, `j_r` | results from P/R/J | `proposer_result`, `reflector_result`, `judge_result` |
| `prj_cycle.py` | `SYS_P`, `SYS_R`, `SYS_J` | system prompts | `SYS_PROPOSER`, `SYS_REFLECTOR`, `SYS_JUDGE` |
| `prj_cycle.py` | `_batch_r`, `_batch_p`, `_batch_j` | batch functions | `batch_reflector`, `batch_proposer`, `batch_judge` |
| `prj_cycle.py` | `_phase_r`, `_phase_p`, `_phase_j` | phase functions | `phase_reflector`, `phase_proposer`, `phase_judge` |
| `prj_cycle.py` | `_run_prj()` | run propose-review-judge cycle | `run_propose_review_judge()` |
| `prj_cycle.py` | `day_r` | day reviewer role key | `day_reviewer` |
| `prj_cycle.py` | `day_p` | day proposer role key | `day_proposer` |
| `prj_cycle.py` | `day_j` | day judge role key | `day_judge` |
| `prj_cycle.py` | `day_mcp` | day MCP role key | `day_mcp` (OK — MCP is domain term) |
| `prj_cycle.py` | `prj_p`, `prj_j` | phase names | `phase_proposer`, `phase_judge` |
| `classify_pipeline.py` | `j_result`, `_phase_j` | judge result | `judge_result`, `phase_judge` |
| `classify_pipeline.py` | `j_result.get("P_score")` | P/R/consensus score keys | `proposer_score`, `reflector_score` |
| `finalize_pipeline.py` | `prj_result`, `prj_results` | PRJ results | `cycle_result`, `cycle_results` |

**Affected files**: `prj_cycle.py` (37 uses), `classify_pipeline.py` (31 uses), `experiment_runner.py` (7 uses), `rubric_experiment.py` (14 uses), `test_handoff_quality.py` (14 uses), `review_pipeline_3model.py` (5 uses), `review_pipeline_steps.py` (7 uses), `night_pipeline.py` (2 uses), `resume_pipeline.py` (4 uses), `finalize_pipeline.py` (4 uses)

---

## 5. Ambiguous Abbreviations (15 categories)

### 5.1 Function names — too short, unclear

| Current | File | Meaning | Suggested |
|---------|------|---------|-----------|
| `ts()` | `prj_cycle.py` | UTC timestamp | `utc_timestamp()` |
| `_est_tok()` | `prj_cycle.py` | estimate token count | `estimate_token_count()` |
| `_sql()` | `lib/db.py` | execute SQL, return pipe-delimited text | `query_sql()` or `run_query()` |
| `esc_sql()` | `lib/db.py` | escape SQL string literal | `escape_sql_string()` |

### 5.2 Variable names — abbreviations that obscure meaning

| Current | File | Meaning | Suggested |
|---------|------|---------|-----------|
| `j_res` | `prj_cycle.py` (15 uses) | judge result | `judge_result` |
| `llm_r` | `prj_cycle.py` (7 uses) | LLM response | `llm_response` |
| `py_res` | `prj_cycle.py` (7 uses) | Python verified result | `python_verified` |
| `ok_a`, `ok_b` | `prj_cycle.py` (12 uses) | Pod A/B health OK | `pod_a_ready`, `pod_b_ready` |
| `max_tok` | multiple (42 uses) | max tokens | `max_tokens` |
| `mcp_pt` | `extract_pipeline.py` | MCP prompt tokens | `mcp_prompt_tokens` |
| `mcp_gt` | `extract_pipeline.py` | MCP generation tokens | `mcp_gen_tokens` |
| `mcp_em` | `extract_pipeline.py` | MCP elapsed ms | `mcp_elapsed_ms` |
| `mcp_acc` | `test_dsv2_verify_mcp.py` | MCP accuracy | `mcp_accuracy` |
| `inv_sev` | `extract_pipeline.py` | investigation severity? | `severity` |
| `sev_dist` | multiple | severity distribution | `severity_distribution` |
| `sev_name` | multiple | severity name | `severity_name` |
| `f_list` | `prj_cycle.py` | finding list? file list? | `finding_list` |
| `no_src` | `extract_pipeline.py` | no source | `without_source` |
| `t_all` | multiple | total all | `total_count` |
| `fb_tag` | multiple | feedback tag | `feedback_tag` |
| `fb_prj` | multiple | feedback project? PRJ? | `feedback_cycle` |
| `fb_nv` | multiple | feedback n...? | unknown — needs inspection |
| `fb_rv` | multiple | feedback review? | `feedback_review` |
| `fb_pf` | multiple | feedback p...? | unknown — needs inspection |
| `role_fb` | `prj_cycle.py` | role feedback? | `role_feedback` |

### 5.3 File names — don't reveal responsibility

| Current | Problem | Suggested |
|---------|---------|-----------|
| `prj_cycle.py` | "prj" = propose-review-judge (invisible) | `pipeline_cycle.py` or `review_cycle.py` |
| `prj_watchdog.py` | Same | `cycle_watchdog.py` |
| `prj_complete_monitor.py` | Same | `cycle_completion_monitor.py` |
| `refs.py` | "refs" = references? | `reference_tracker.py` |
| `db.py` | Too generic | `database.py` (but impact is high — many imports) |

---

## 6. File Name Ambiguity (doesn't reveal what it does)

| File | Problem |
|------|---------|
| `observer.py` | Observes what? System? Pipeline? LLM? |
| `shared.py` (lib/code_mod/) | Too generic — what is shared between what? |
| `estimator.py` | Estimates what? Rate? Cost? Time? |
| `scoring.py` | Scores what? |
| `finalize_pipeline.py` | Which pipeline? What finalization? |
| `resume_pipeline.py` | Resumes what pipeline? |
| `hybrid_pipeline.py` | Hybrid of what + what? |

---

## Execution Plan

### Phase 1: Role Abbreviations → Full Words (highest impact, single biggest clarity boost)

Files: `prj_cycle.py`, `classify_pipeline.py`, `experiment_runner.py`, `night_pipeline.py`, `resume_pipeline.py`, `rubric_experiment.py`, `test_handoff_quality.py`, `review_pipeline_3model.py`, `review_pipeline_steps.py`, `finalize_pipeline.py`

- `P_MODEL` → `PROPOSER_ROLE`
- `R_MODEL` → `REFLECTOR_ROLE`
- `J_MODEL` → `JUDGE_ROLE`
- `day_r` → `day_reviewer`
- `day_p` → `day_proposer`
- `day_j` → `day_judge`
- `p_findings` → `proposer_findings`
- `r_verdicts` → `reflector_verdicts`
- `_run_prj()` → `run_propose_review_judge()`
- All derived variables (`p_r`, `r_r`, `j_r`, `j_res`, `prj_*`)

### Phase 2: Model Names → Role Names (prevents future model-swap breakage)

- Rename 13 test files
- Rename 6 functions with model sizes
- Rename 10+ variables with model sizes

### Phase 3: Ambiguous Abbreviations → Full Words

- `ts()` → `utc_timestamp()`
- `_est_tok()` → `estimate_token_count()`
- `esc_sql()` → `escape_sql_string()`
- `_sql()` → `query_sql()`
- All `fb_*` → `feedback_*`
- All `mcp_pt/gt/em` → `mcp_prompt_tokens/gen_tokens/elapsed_ms`
- All `sev_*` → `severity_*`

### Phase 4: File Name Clarity

- `prj_cycle.py` → `pipeline_cycle.py`
- Other `prj_*` files
- `refs.py` → `reference_tracker.py`
- Generic names: `observer.py`, `shared.py`, `estimator.py`, `scoring.py`

---

*Generated: 2026-06-06 | Language: English (machine-readable) | Status: audit complete, execution pending*