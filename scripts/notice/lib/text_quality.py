#!/usr/bin/env python3
# Status: production
# Path: imported by — notice/telegram_bot.py
"""Phase 2 Tier 1 — Outbound Korean text quality guardrails.

Validates Korean user-facing output from the language pipeline:
  1. Script purity — Hangul ratio >= 15% (technical Korean includes ASCII terms)
  2. Token budget  — 10-500 chars (avoids empty/truncated output and runaway verbosity)
  3. Think-tag     — detect LLM internals (thinking, function_call, invoke) leaks

Integration: notice/telegram_bot._send() — single choke-point for all user-facing output.
Soft checks only (warn + log), no automatic blocking.
"""

import re
from typing import Dict, List, Optional, Tuple

_HANGUL_RE = re.compile(r'[가-힣ㄱ-ㅎㅏ-ㅣ]')
_NON_ALPHANUM_HANGUL_RE = re.compile(r'[^\w\s가-힣ㄱ-ㅎㅏ-ㅣ.,!?~\-:;()\[\]{}<>/|@#$%^&*+=`"\'·…]')

_ARTIFACT_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r'</?thinking\s*>', re.IGNORECASE), 'thinking tag'),
    (re.compile(r'<thinking>.*?</thinking>', re.DOTALL | re.IGNORECASE), 'thinking block'),
    (re.compile(r'<function_call>|<invoke>|</invoke>'), 'tool-call tag'),
    (re.compile(r'</?think>'), 'think-short tag'),
]

# Korean technical text: ~20-60% Hangul. Lower threshold catches
# edge cases like ASCII-heavy status messages that are still valid Korean.
MIN_HANGUL_RATIO = 0.10

# below 10 = likely empty/truncated. above 500 = likely runaway verbosity.
# These bounds target the *Korean text body*, not the Telegram 4000-char API limit.
MIN_CHARS = 10
MAX_CHARS = 500


def check_korean_ratio(text: str, min_ratio: float = MIN_HANGUL_RATIO) -> Tuple[bool, float]:
    """Return (ok, hangul_ratio). 0.15 threshold catches ASCII-heavy Korean."""
    if not text:
        return False, 0.0
    hangul_chars = len(_HANGUL_RE.findall(text))
    ratio = hangul_chars / max(len(text), 1)
    return ratio >= min_ratio, ratio


def check_token_budget(text: str) -> Tuple[bool, int]:
    """Return (ok, length). 10-500 char band for well-formed Korean output."""
    length = len(text)
    return MIN_CHARS <= length <= MAX_CHARS, length


def check_think_tags(text: str) -> Tuple[bool, List[str]]:
    """Return (ok, list_of_found_artifact_labels)."""
    found = [label for pat, label in _ARTIFACT_PATTERNS if pat.search(text)]
    return len(found) == 0, found


def validate(text: str) -> Dict:
    """Run all 3 guardrails. Returns dict suitable for logging.

    Always returns ok=True when text is empty/None — caller decides
    whether missing text is an error in its own context.
    """
    if not text:
        return {"ok": True, "text_length": 0, "checks": {}}

    ratio_ok, ratio = check_korean_ratio(text)
    budget_ok, length = check_token_budget(text)
    artifact_ok, artifacts = check_think_tags(text)

    all_ok = ratio_ok and budget_ok and artifact_ok

    return {
        "ok": all_ok,
        "text_length": len(text),
        "checks": {
            "korean_ratio": {"ok": ratio_ok, "ratio": round(ratio, 3)},
            "token_budget": {"ok": budget_ok, "length": length},
            "artifacts":  {"ok": artifact_ok, "found": artifacts},
        },
    }


def clean(text: str) -> str:
    """Best-effort strip of known LLM artifacts from text."""
    for pat, _ in _ARTIFACT_PATTERNS:
        text = pat.sub('', text)
    # remove stray control characters that survive LLM output
    text = text.replace('\x00', '').replace('\r', '')
    return text.strip()


def truncate_at_boundary(text: Optional[str], max_chars: int) -> str:
    """Truncate text at the last sentence boundary (. ? ! ...) within max_chars.

    Falls back to last word boundary (space), then hard truncation.
    Returns "" for None/empty/whitespace-only/zero-max_chars input.
    """
    if not text:
        return ""
    if max_chars <= 0:
        return ""

    stripped = text.strip()
    if not stripped:
        return ""
    if len(stripped) <= max_chars:
        return stripped

    best = -1
    for i, ch in enumerate(stripped):
        if ch not in '.?!...' or i >= max_chars:
            continue
        if ch == '...':
            best = i + 1
        elif i + 1 == len(stripped) or stripped[i + 1] == ' ':
            best = i + 1

    if best > 0:
        return stripped[:best].rstrip()

    last_space = stripped.rfind(' ', 0, max_chars)
    if last_space > 0:
        return stripped[:last_space]

    return stripped[:max_chars]
