# Status: archived
# Path: none — archived 2026-07-03 (replaced by EDC+R pipeline)
"""Archived old extraction prompts and functions from extract_llm.py.

These were the original section-based extraction logic (user→thinking→text),
replaced by the EDC+R pipeline (_extract_edcr_freeform) with FREE-form
A-Strict/B-Xplore dual extraction and Stop-and-Swap lazy embed canonicalization.

Full code available in git history at commit 6547a56~1.
"""

# ── Old section-specific prompts (lines 157-289 in pre-archive extract_llm.py) ──
# Replaced by STRICT/XPLORE/FREE variants. The old prompts had snake_case
# predicate constraints that caused Taxonomy Trap issues.

_SYSTEM_USER_EXTRACT = """\
You are a fact extractor for a developer conversation. Extract factual triples
(subject, predicate, object) that are EXPLICITLY stated in the USER MESSAGE.
..."""
# Full text in git history: commit 6547a56~1, lines 157-201

_SYSTEM_THINKING_EXTRACT = """\
You are a fact extractor for a developer conversation. Extract factual triples
(subject, predicate, object) that are EXPLICITLY stated in the ASSISTANT'S
INTERNAL REASONING (thinking).
..."""
# Full text in git history: commit 6547a56~1, lines 203-245

_SYSTEM_TEXT_EXTRACT = """\
You are a fact extractor for a developer conversation. Extract factual triples
(subject, predicate, object) that are EXPLICITLY present in the ASSISTANT'S
RESPONSE (text).
..."""
# Full text in git history: commit 6547a56~1, lines 247-289

# ── A-Strict / B-Xplore prompts (lines 155-288 in pre-archive) ──
# _SYSTEM_USER_EXTRACT_STRICT, _SYSTEM_TEXT_EXTRACT_STRICT (4B Precision)
# _SYSTEM_USER_EXTRACT_XPLORE, _SYSTEM_TEXT_EXTRACT_XPLORE (High Recall)
# Replaced by FREE/XPLORE_FREE variants with natural-language predicates.
# Full code in git history: commit 6547a56~1

# ── Old per-turn extraction functions ──
# _extract_section, _extract_single, _extract_for_turn
# _observe_extract_usage, _is_low_value, _load_entity_context, _LOW_VALUE_PATTERNS
# Full code in git history: commit 6547a56~1

# ── Old section-major dual 4B extraction (predecessor to EDC+R) ──
# _extract_dual_section_major, _backward_compat
# Full code in git history: commit 6547a56~1

# ── Archived test files ──
# compare_extract_ab.py, compare_extract_ab_resume.py
# compare_extract_ab_resume2.py, compare_extract_ab_retry_B.py
# All imported _extract_for_turn (dead code).
# Moved to _archive/tests/ on 2026-07-03.
