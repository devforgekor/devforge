#!/usr/bin/env python3
# Status: production
# Path: extract.py — re-export from lib/extract_llm/
"""LLM extraction submodule — re-exports from lib/extract_llm package.

See lib/extract_llm/ for implementation in submodules:
  chunking.py — text splitting, paragraph grouping, compound expansion
  edc.py — EDC predicate/entity normalization, QC pipeline
  parser.py — JSON error recovery and parsing
  _core.py — constants, prompts, token calc, embed mgmt, _extract_edcr_freeform
"""

import os
import sys

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.extract_llm import (  # noqa: E402, F401
    _SIGTERM_RECEIVED,
    _SYSTEM_TEXT_EXTRACT_FREE,
    _SYSTEM_TEXT_EXTRACT_FREE_8B,
    _SYSTEM_USER_EXTRACT_FREE,
    _SYSTEM_USER_EXTRACT_FREE_8B,
    SYSTEM_DAY_EXTRACT,
    SYSTEM_DESCRIBE_FILE,
    SYSTEM_FALLBACK,
    _build_section_prefix,
    _calc_max_tokens,
    _calc_timeout,
    _call_with_8082_retry,
    _checkpoint_sections,
    _clean_extraction_json,
    _cleanup_all_llms,
    _extract_edcr_freeform,
    _fix_status_hallucination,
    _merge_usage,
    _parse_heading_level,
    _parse_json,
    _quality_check_facts,
    _sigterm_handler,
    _split_atomic,
)
