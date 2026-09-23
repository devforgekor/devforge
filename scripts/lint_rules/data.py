#!/usr/bin/env python3
# Status: production
"""Lint rules constants."""

import re

# Files where model names in identifiers are legitimate
MODEL_NAME_OK_FILES = {
    "scripts/lint_rules/data.py",  # rule data defines the brand/size patterns
    "scripts/lib/debate/debate_data.py",
    "scripts/lib/infra/azure_spot/config.py",
    "scripts/lib/llm_client.py",
    "scripts/lint_rules.py",
    "scripts/lib/tracking/agent_names.py",
    "scripts/proxies/model_score.py",  # role scoring keyword data (matches model IDs)
    "scripts/observer.py",
    "scripts/lib/debate/cooperative_debate.py",
    "scripts/pipelines/hybrid.py",
    "scripts/pipelines/night.py",
    # 2026-09-20: model-specific by nature (model config, resource names, model regex, model-comparison tests)
    "scripts/golden_image/azure_client.py",  # Azure LLM resource names ("llm-qwen-*")
    "scripts/golden_image/refresh_cycle.py",  # Azure LLM VM prefix ("llm-qwen")
    "scripts/lib/extract_llm/_core.py",  # _ENTITY_RESOLVER_4B prompt constant
    "scripts/pipelines/entity_scan.py",  # model-name regex (data)
    "scripts/tests/nli_compare_14b.py",
    "scripts/tests/pipeline_verify_compare.py",
    "scripts/tests/test_4b_dual_review_7b_vs_9b.py",
    "scripts/tests/test_7b_vs_9b_text_extract.py",
    "scripts/tests/test_arbiter_dual_7b9b.py",
    "scripts/tests/test_dual_4b.py",
    "scripts/tests/test_dual_extract_7b_vs_9b.py",
    "scripts/tests/test_extract_3config_comparison.py",
    "scripts/tests/test_extract_strategies.py",
    "scripts/tests/test_fact5_comparison.py",
    "scripts/tests/test_phase1_14b.py",
    "scripts/tests/test_phase2_3.py",
    "scripts/tests/test_review_7b.py",
    "scripts/tests/test_review_9b.py",
}

MODEL_SIZE_PATTERN = re.compile(r"\b(3b|4b|7b|14b|27b|30b)\b", re.IGNORECASE)
MODEL_BRAND_PATTERN = re.compile(r"\b(qwen|codestral|selene|nemotron|phi[_-]?4|llama[_-]?3)\b", re.IGNORECASE)

ABBREVIATION_OK_FILES = {
    "scripts/lib/db.py",
    "scripts/bench_llm.py",
}
BANNED_SINGLE_LETTER = {"P", "R", "J"}

BANNED_NAMES = {
    "ts": "utc_timestamp",
    "_est_tok": "estimate_token_count",
    "esc_sql": "escape_sql_string",
    "j_res": "judge_result",
    "llm_r": "llm_response",
    "py_res": "python_verified_result",
    "ok_a": "pod_a_ready",
    "ok_b": "pod_b_ready",
    "max_tok": "max_tokens",
    "enrich_pt": "enrich_prompt_tokens",
    "enrich_gt": "enrich_gen_tokens",
    "enrich_em": "enrich_elapsed_ms",
    "fb_tag": "feedback_tag",
    "fb_prj": "feedback_cycle",
    "fb_nv": "feedback_nv",
    "fb_rv": "feedback_review",
    "fb_pf": "feedback_pf",
    "inv_sev": "inverted_severity",
    "sev_dist": "severity_distribution",
    "sev_name": "severity_name",
    "no_src": "without_source",
    "t_all": "total_count",
    "f_list": "finding_list",
    "day_r": "day_reviewer",
    "day_p": "day_proposer",
    "day_j": "day_judge",
    "prj_p": "phase_proposer",
    "prj_r": "phase_reflector",
    "prj_j": "phase_judge",
    "p_r": "proposer_result",
    "r_r": "reflector_result",
    "j_r": "judge_result",
    "SYS_P": "PROPOSER_SYSTEM_PROMPT",
    "SYS_R": "REFLECTOR_SYSTEM_PROMPT",
    "SYS_J": "JUDGE_SYSTEM_PROMPT",
    "SYS_V": "VERIFIER_SYSTEM_PROMPT",
    "SYS_V27": "VERIFIER_SYSTEM_PROMPT",
    "SYS_V32": "SECONDARY_VERIFIER_SYSTEM_PROMPT",
    "SYS_DAY_P": "DAY_PROPOSER_SYSTEM_PROMPT",
    "SYS_DAY_R": "DAY_REFLECTOR_SYSTEM_PROMPT",
    "SYS_DAY_J": "DAY_JUDGE_SYSTEM_PROMPT",
    "SYS_NIGHT_P": "NIGHT_PROPOSER_SYSTEM_PROMPT",
    "SYS_NIGHT_R": "NIGHT_REFLECTOR_SYSTEM_PROMPT",
    "SYS_NIGHT_J": "NIGHT_JUDGE_SYSTEM_PROMPT",
}

_KOREAN_RE = re.compile(r"[가-힯ᄀ-ᇿ㄰-㆏]")
