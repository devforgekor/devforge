#!/usr/bin/env python3
# Status: experimental
# Path: preprocessing pipeline — turn_watcher.py, backfill scripts
"""Korean text cleaner using Kiwi morphological analyzer.

Two phases:
  1. clean(text) → normalized text (NFKC, whitespace, emoticons, repeat chars)
  2. tokenize(text) → Kiwi POS tags for BM25 indexing

Code blocks (```...```) and inline code (`...`) are preserved verbatim.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

from kiwipiepy import Kiwi

# Emoji removal — only well-known emoji blocks, no Hangul overlap
RE_EMOJI = re.compile(
    "[\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001FA00-\U0001FA6F"  # chess symbols
    "\U0001FA70-\U0001FAFF"  # symbols extended-A
    "\U00002702-\U000027B0"  # dingbats
    "]+"
)
RE_KOREAN_EMOTICON = re.compile(r"[ㅋㅠㅜㅎㅡ]{3,}")
RE_REPEAT_HANGUL = re.compile(r"([가-힣])\1{3,}")
RE_MULTI_SPACE = re.compile(r"\s+")
RE_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
RE_INLINE_CODE = re.compile(r"`[^`]+`")

# Tags that carry lexical meaning for BM25 indexing
LEXICAL_TAGS = frozenset({
    "NNG", "NNP", "NNB", "NR", "NP",  # nouns
    "VV", "VA", "VX",  # verbs/adjectives
    "MAG", "MAJ",  # adverbs
    "SL", "SH", "SN",  # foreign/chinese/numbers
    "XR",  # roots
})

# Tags used for topic/keyword extraction (topic-bearing nouns)
TOPIC_TAGS = frozenset({"NNP"})


class TextCleaner:
    """Korean text cleaner with Kiwi-based tokenization.

    Usage:
        cleaner = TextCleaner()
        clean_text = cleaner.clean(raw_text)
        tokens = cleaner.tokenize(clean_text)
    """

    def __init__(self) -> None:
        self._kiwi = Kiwi()

    # ------------------------------------------------------------------
    # Phase 1: Text Cleaning
    # ------------------------------------------------------------------

    def _clean_non_kiwi(self, text: str) -> Tuple[str, List[str], List[str]]:
        """Apply non-Kiwi cleaning steps (1-6). Returns (cleaned, code_blocks, inline_codes)."""
        if not text:
            return "", [], []

        code_blocks: List[str] = []
        inline_codes: List[str] = []

        def _save_code(m: re.Match) -> str:
            code_blocks.append(m.group())
            return f"\x00BLOCK{len(code_blocks) - 1}\x00"

        def _save_inline(m: re.Match) -> str:
            inline_codes.append(m.group())
            return f"\x00INLINE{len(inline_codes) - 1}\x00"

        t = RE_CODE_BLOCK.sub(_save_code, text)
        t = RE_INLINE_CODE.sub(_save_inline, t)

        t = RE_KOREAN_EMOTICON.sub(lambda m: m.group()[0] * 2, t)
        t = unicodedata.normalize("NFKC", t)
        t = RE_EMOJI.sub(" ", t)
        t = RE_REPEAT_HANGUL.sub(lambda m: m.group(1) * 2, t)
        t = RE_MULTI_SPACE.sub(" ", t)
        t = t.strip()
        return t, code_blocks, inline_codes

    def _apply_kiwi(self, text: str) -> str:
        """Apply Kiwi typo correction only. Placeholders pass through unchanged."""
        if not text.strip():
            return text
        tokens = self._kiwi.tokenize(text, typos="basic_with_continual_and_lengthening")
        return self._kiwi.join(tokens)

    def _restore_placeholders(self, text: str, code_blocks: List[str], inline_codes: List[str]) -> str:
        for i, cb in enumerate(code_blocks):
            text = text.replace(f"\x00BLOCK{i}\x00", cb)
        for i, ic in enumerate(inline_codes):
            text = text.replace(f"\x00INLINE{i}\x00", ic)
        return text

    def clean(self, text: str) -> str:
        """Normalize Korean text for BM25 + Embedding.

        Preserves code blocks (``````) and inline code (``) verbatim.
        """
        t, code_blocks, inline_codes = self._clean_non_kiwi(text)
        if t:
            t = self._apply_kiwi(t)
        return self._restore_placeholders(t, code_blocks, inline_codes)

    def detect_kiwi_changes(self, text: str) -> bool:
        """True if Kiwi typo correction modified the text (vs NFKC/whitespace-only changes).

        Runs non-Kiwi cleaning, then Kiwi, then compares. Returns True only when
        Kiwi actually corrected spelling/grammar — ignores NFKC/emoji/whitespace changes.
        ~5ms per call.
        """
        if not text.strip():
            return False
        t, _, _ = self._clean_non_kiwi(text)
        after = self._apply_kiwi(t)
        return t != after

    # ------------------------------------------------------------------
    # Phase 2: Kiwi Tokenization (for BM25)
    # ------------------------------------------------------------------

    def tokenize(self, text: str) -> List[Dict]:
        """Kiwi POS tagging. Returns list of {form, tag, start, len} dicts."""
        tokens = self._kiwi.tokenize(
            text,
            normalize_coda=True,
            typos="basic_with_continual_and_lengthening",
            oov_handling="chr_freq",
        )
        return [
            {"form": t.form, "tag": t.tag, "start": t.start, "len": len(t.form)}
            for t in tokens
        ]

    def extract_terms(self, text: str) -> List[str]:
        """Extract lexical terms (nouns, verbs, foreign) for BM25 indexing."""
        tokens = self._kiwi.tokenize(
            text,
            normalize_coda=True,
            typos="basic_with_continual_and_lengthening",
            oov_handling="chr_freq",
        )
        return [t.form for t in tokens if t.tag in LEXICAL_TAGS]

    def extract_nnp(self, text: str) -> List[str]:
        """Extract proper nouns (NNP) for topic/keyword tagging."""
        if not text.strip():
            return []
        tokens = self._kiwi.tokenize(
            text,
            normalize_coda=True,
            typos="basic_with_continual_and_lengthening",
            oov_handling="chr_freq",
        )
        seen = set()
        result = []
        for t in tokens:
            if t.tag == "NNP" and t.form not in seen:
                seen.add(t.form)
                result.append(t.form)
        return result

    def estimate_tokens(self, text: str) -> int:
        """Estimate LLM token count for Korean text using Kiwi.

        Kiwi POS tokens approximate LLM subword tokens. Korean averages ~1.2x
        Kiwi→LLM-token ratio, so we multiply by 1.2 for a safe estimate.
        Returns 0 for empty text.
        """
        if not text.strip():
            return 0
        tokens = self._kiwi.tokenize(
            text,
            normalize_coda=True,
            typos="basic_with_continual_and_lengthening",
            oov_handling="chr_freq",
        )
        return max(4, int(len(tokens) * 1.2))

    # ------------------------------------------------------------------
    # Batch processing
    # ------------------------------------------------------------------

    def process_document(self, text: str) -> Dict:
        """Full document processing: clean → tokenize → extract.

        Returns dict suitable for storing in turns.tokens jsonb column.
        """
        if not text:
            return {"clean": "", "terms": [], "tokens": [], "kiwi_changed": False, "nnp": []}

        clean_text = self.clean(text)
        tokens = self.tokenize(clean_text)
        terms = [t["form"] for t in tokens if t["tag"] in LEXICAL_TAGS]
        nnp = self.extract_nnp(clean_text)
        kiwi_changed = self.detect_kiwi_changes(text)

        return {
            "clean": clean_text,
            "terms": terms,
            "tokens": tokens,
            "kiwi_changed": kiwi_changed,
            "nnp": nnp,
        }


# Module-level singleton
_cleaner: Optional[TextCleaner] = None


def get_cleaner() -> TextCleaner:
    global _cleaner
    if _cleaner is None:
        _cleaner = TextCleaner()
    return _cleaner


def clean(text: str) -> str:
    return get_cleaner().clean(text)


def tokenize(text: str) -> List[Dict]:
    return get_cleaner().tokenize(text)


def extract_terms(text: str) -> List[str]:
    return get_cleaner().extract_terms(text)


def extract_nnp(text: str) -> List[str]:
    return get_cleaner().extract_nnp(text)


def estimate_tokens(text: str) -> int:
    return get_cleaner().estimate_tokens(text)


def detect_kiwi_changes(text: str) -> bool:
    return get_cleaner().detect_kiwi_changes(text)
