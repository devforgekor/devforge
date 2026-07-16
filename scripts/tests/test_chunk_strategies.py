#!/usr/bin/env python3
"""Test chunking strategies — _split_atomic and its helper functions.

Usage:  python3 -m pytest scripts/tests/test_chunk_strategies.py -v
"""

import os, sys, re
from typing import Optional

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

import pytest
from pipelines.extract_llm import (
    _split_atomic,
    _parse_heading_level,
    _build_section_prefix,
    _group_sentences,
    _emit_chunks_with_overlap,
    _merge_section_paragraphs,
    CHUNK_OVERLAP_CHARS,
)

SRC_FILE = os.path.join(SCRIPTS_DIR, "..", "docs", "architecture", "infrastructure.md")
GT_FACTS = [
    ("22Gi", "Total RAM"),
    ("ARM Neoverse-N1", "CPU"),
    ("zram", "Swap"),
    ("9.7", "OS version"),
    ("rootless", "Container runtime"),
    ("JSONB", "DB version"),
    ("auto-HTTPS", "Proxy"),
    ("100G", "AI data mount"),
    ("30G", "DB mount"),
    ("10G", "Projects mount"),
    ("4G", "Swap volume"),
    ("5432", "Data pod port"),
    ("inactive", "Pod A status"),
    ("active", "Swap service"),
]


# ── Helpers ───────────────────────────────────────────────────────


def _run_strategy(strategy: str, text: Optional[str] = None, max_chars: int = 1600) -> list[str]:
    os.environ["CHUNK_STRATEGY"] = strategy
    import importlib, pipelines.extract_llm as m
    importlib.reload(m)
    if text is None:
        with open(SRC_FILE) as f:
            text = f.read()
    return m._split_atomic(text, max_chars=max_chars)


# ── Unit: _parse_heading_level ────────────────────────────────────


class TestParseHeadingLevel:
    def test_h1(self):
        assert _parse_heading_level("# Title") == (1, "Title", "")

    def test_h2(self):
        assert _parse_heading_level("## Section") == (2, "Section", "")

    def test_h3(self):
        assert _parse_heading_level("### Sub") == (3, "Sub", "")

    def test_h4(self):
        assert _parse_heading_level("#### Deep") == (4, "Deep", "")

    def test_h5(self):
        assert _parse_heading_level("##### VDeep") == (5, "VDeep", "")

    def test_h6(self):
        assert _parse_heading_level("###### VVDeep") == (6, "VVDeep", "")

    def test_heading_with_inline_content(self):
        level, heading, rest = _parse_heading_level("## H2\nSome content here.")
        assert level == 2
        assert heading == "H2"
        assert "Some content here." in rest

    def test_not_a_heading(self):
        assert _parse_heading_level("Just a paragraph.") == (0, "", "Just a paragraph.")

    def test_empty(self):
        assert _parse_heading_level("") == (0, "", "")

    def test_heading_trailing_spaces(self):
        assert _parse_heading_level("##  Section  ") == (2, "Section", "")


# ── Unit: _build_section_prefix ────────────────────────────────────


class TestBuildSectionPrefix:
    def test_empty_stack(self):
        assert _build_section_prefix([]) == ""

    def test_h1_only(self):
        assert _build_section_prefix([(1, "Title")]) == ""

    def test_h2_only(self):
        assert _build_section_prefix([(1, "Title"), (2, "Overview")]) == "[Section: Overview] "

    def test_h2_h3(self):
        stack = [(1, "Title"), (2, "Entry Points"), (3, "Auto-Generated Docs")]
        assert _build_section_prefix(stack) == "[Section: Entry Points > Auto-Generated Docs] "

    def test_h3_without_h2(self):
        assert _build_section_prefix([(3, "Orphan")]) == "[Section: Orphan] "

    def test_mixed_levels(self):
        stack = [(1, "Doc"), (2, "A"), (3, "B"), (4, "C"), (5, "D"), (6, "E")]
        assert _build_section_prefix(stack) == "[Section: A > B > C > D > E] "


# ── Unit: _group_sentences ────────────────────────────────────────


class TestGroupSentences:
    def test_single_sentence(self):
        assert _group_sentences(["Hello world."], 100) == ["Hello world."]

    def test_multiple_sentences_one_chunk(self):
        sents = ["A. ", "B. ", "C. "]
        result = _group_sentences(sents, 100)
        assert len(result) == 1
        assert result[0] == "A.  B.  C. "

    def test_multiple_chunks(self):
        sents = ["A" * 60, "B" * 60, "C" * 60]
        result = _group_sentences(sents, 100)
        assert len(result) == 3

    def test_empty(self):
        assert _group_sentences([], 100) == []

    def test_two_sentences_fit(self):
        sents = ["Hello world.", "Foo bar baz."]
        result = _group_sentences(sents, 200)
        assert len(result) == 1
        assert "Hello world. Foo bar baz." in result[0]

    def test_split_when_exceeds(self):
        sents = ["A" * 90, "B" * 90]
        result = _group_sentences(sents, 100)
        assert len(result) == 2


# ── Unit: _emit_chunks_with_overlap ────────────────────────────────


class TestEmitChunksWithOverlap:
    def _call(self, base_chunks, prefix="[Section: Test] ", max_chars=200):
        result = []
        _emit_chunks_with_overlap(result, base_chunks, prefix, max_chars)
        return result

    def test_single_chunk_no_overlap(self):
        result = self._call(["Content A."])
        assert len(result) == 1
        assert result[0] == "[Section: Test] Content A."

    def test_overlap_present(self):
        chunks = ["x" * 100, "y" * 100]
        result = self._call(chunks, max_chars=300)
        assert len(result) == 2
        assert "[Section: Test]" in result[1]
        # overlap should be in chunk 1
        tail = chunks[0][-CHUNK_OVERLAP_CHARS:]
        assert tail.strip() in result[1]

    def test_overlap_truncated_when_too_long(self):
        long = "A" * 200
        chunks = [long, "B" * 200]
        result = self._call(chunks, prefix="[X] ", max_chars=250)
        assert len(result) == 2
        # overlap doesn't fit, falls back to prefix+child truncated
        assert result[1].startswith("[X] ")
        assert len(result[1]) <= 250

    def test_no_prefix(self):
        result = self._call(["Content."], prefix="", max_chars=200)
        assert result[0] == "Content."

    def test_empty_base_chunks(self):
        result = self._call([], prefix="[X] ")
        assert result == []


# ── Unit: _merge_section_paragraphs ────────────────────────────────


class TestMergeSectionParagraphs:
    def test_no_h2_no_merge(self):
        paras = ["Small", "paras"]
        result = _merge_section_paragraphs(paras, 1600)
        assert result == ["Small\nparas"]  # both <200 → merged

    def test_h2_boundary_preserved(self):
        paras = ["## H2 A", "small1", "small2", "## H2 B", "small3"]
        result = _merge_section_paragraphs(paras, 1600)
        assert result[0] == "## H2 A"
        assert result[1] == "small1\nsmall2"
        assert result[2] == "## H2 B"
        assert result[3] == "small3"

    def test_large_paragraph_not_merged(self):
        large = "X" * 300
        paras = ["## H2", "small", large]
        result = _merge_section_paragraphs(paras, 1600)
        assert result == ["## H2", "small", large]

    def test_empty(self):
        assert _merge_section_paragraphs([], 1600) == []


# ── Integration: GT fact coverage (all strategies) ─────────────────


class TestGtFactCoverage:
    STRATEGIES = ["plain", "contextual", "hierarchical"]

    @pytest.mark.parametrize("strategy", STRATEGIES)
    @pytest.mark.parametrize("val,note", GT_FACTS)
    def test_gt_value_present(self, strategy, val, note):
        chunks = _run_strategy(strategy)
        assert any(val.lower() in c.lower() for c in chunks), (
            f"[{strategy}] GT value {val!r} ({note}) not found in any chunk"
        )

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_no_placeholder_leak(self, strategy):
        chunks = _run_strategy(strategy)
        for c in chunks:
            assert "@@@DOT@@@" not in c, f"[{strategy}] Dangling @@@DOT@@@ in chunk"
            assert "@@@CBNL@@@" not in c, f"[{strategy}] Dangling @@@CBNL@@@ in chunk"

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_no_marker_noise(self, strategy):
        chunks = _run_strategy(strategy)
        for c in chunks:
            assert ">>>" not in c, f"[{strategy}] Stray >>> in chunk"
            assert "(prev:" not in c, f"[{strategy}] Stray (prev: in chunk"
            assert "(next:" not in c, f"[{strategy}] Stray (next: in chunk"


# ── Integration: strategy-specific behavior ────────────────────────


class TestStrategySpecific:
    def test_plain_no_section_prefix(self):
        chunks = _run_strategy("plain")
        assert not any(c.startswith("[Section:") for c in chunks)

    def test_contextual_all_chunks_prefixed(self):
        chunks = _run_strategy("contextual")
        for i, c in enumerate(chunks):
            if i < 2:  # preamble (H1 + comment)
                continue
            assert c.startswith("[Section:"), f"contextual chunk {i} missing prefix: {c[:50]}"

    def test_hierarchical_all_chunks_prefixed(self):
        chunks = _run_strategy("hierarchical")
        for i, c in enumerate(chunks):
            if i < 2:
                continue
            assert c.startswith("[Section:"), f"hierarchical chunk {i} missing prefix: {c[:50]}"

    def test_h3_hierarchy_preserved(self):
        for strategy in ("contextual", "hierarchical"):
            chunks = _run_strategy(strategy)
            h3 = [c for c in chunks if "Section: Entry Points > Auto-Generated" in c]
            assert h3, f"[{strategy}] H3 hierarchy missing in all chunks"

    def test_plain_still_works_no_crash(self):
        chunks = _run_strategy("plain")
        assert len(chunks) > 0

    def test_all_strategies_nonempty(self):
        for s in ("plain", "contextual", "hierarchical"):
            chunks = _run_strategy(s)
            assert len(chunks) > 0, f"[{s}] returned no chunks"


# ── Edge cases ─────────────────────────────────────────────────────


class TestEdgeCases:
    @pytest.mark.parametrize("strategy", ("plain", "contextual", "hierarchical"))
    def test_empty_text(self, strategy):
        assert _run_strategy(strategy, "") == []

    @pytest.mark.parametrize("strategy", ("plain", "contextual", "hierarchical"))
    def test_single_sentence(self, strategy):
        chunks = _run_strategy(strategy, "Just one sentence here.")
        assert len(chunks) == 1

    @pytest.mark.parametrize("strategy", ("plain", "contextual", "hierarchical"))
    def test_preamble_only(self, strategy):
        chunks = _run_strategy(strategy, "Just some text.\nNo headings at all.\nSecond paragraph.")
        assert len(chunks) >= 1

    def test_code_block_blank_lines(self):
        text = "## Section\n\n```\nline1\n\nline3\n```\n\nMore text."
        chunks = _run_strategy("contextual", text)
        combined = " ".join(chunks)
        assert "@@@CBNL@@@" not in combined
        assert "line1" in combined and "line3" in combined

    @pytest.mark.parametrize("strategy", ("plain", "contextual", "hierarchical"))
    def test_dotted_numbers_preserved(self, strategy):
        text = "## Test\nVersion 2.5.3 is out. IP 10.0.0.1 is up."
        chunks = _run_strategy(strategy, text)
        combined = " ".join(chunks)
        assert "2.5.3" in combined
        assert "10.0.0.1" in combined

    def test_small_max_chars_splits(self):
        text = "A" * 40 + ". " + "B" * 40 + ". " + "C" * 40 + "."
        chunks = _run_strategy("plain", text, max_chars=50)
        assert len(chunks) >= 2

    def test_h1_only_document(self):
        text = "# Doc Title\n\nContent paragraph.\n\nAnother paragraph."
        chunks = _run_strategy("contextual", text)
        assert any(c == "# Doc Title" for c in chunks)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
