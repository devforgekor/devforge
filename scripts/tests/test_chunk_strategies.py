#!/usr/bin/env python3
"""Test chunking strategies against infrastructure.md.
Usage:
  CHUNK_STRATEGY=plain   python3 scripts/tests/test_chunk_strategies.py
  CHUNK_STRATEGY=contextual python3 scripts/tests/test_chunk_strategies.py
  CHUNK_STRATEGY=hierarchical python3 scripts/tests/test_chunk_strategies.py
  # compare all:
  python3 scripts/tests/test_chunk_strategies.py --compare
"""

import os, sys, re

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from pipelines.extract_llm import _split_atomic, CHUNK_STRATEGY

SRC_FILE = os.path.join(SCRIPTS_DIR, "..", "docs", "architecture", "infrastructure.md")


def load_source() -> str:
    with open(SRC_FILE) as f:
        return f.read()


def show_chunks(strategy: str, source: str):
    os.environ["CHUNK_STRATEGY"] = strategy
    import importlib
    import pipelines.extract_llm as m
    importlib.reload(m)
    chunks = m._split_atomic(source)

    print(f"\n{'='*70}")
    print(f"  STRATEGY: {strategy}  ({len(chunks)} chunks)")
    print(f"{'='*70}")
    for i, c in enumerate(chunks):
        print(f"\n  Chunk {i} ({len(c)} chars):")
        print(f"    {c[:200]}{'...' if len(c) > 200 else ''}")

    # Count GT fact mentions
    gt_facts = [
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

    print(f"\n  {'─'*60}")
    print(f"  GT Fact Coverage:")
    for val, note in gt_facts:
        found = [i for i, c in enumerate(chunks) if val.lower() in c.lower()]
        status = f"chunks {found}" if found else "MISSING"
        print(f"    {val:20s} ({note:20s}) → {status}")

    return chunks


def main():
    source = load_source()
    compare = "--compare" in sys.argv

    if compare:
        print(f"\nSource: {SRC_FILE} ({len(source)} chars)")
        print(f"Default CHUNK_STRATEGY: {CHUNK_STRATEGY}")

        for strategy in ["plain", "contextual", "hierarchical"]:
            show_chunks(strategy, source)
    else:
        print(f"Source: {SRC_FILE} ({len(source)} chars)")
        print(f"CHUNK_STRATEGY={CHUNK_STRATEGY}")
        chunks = show_chunks(CHUNK_STRATEGY, source)

        # Summary
        print(f"\n  {'─'*60}")
        print(f"  Total chunks: {len(chunks)}")
        total_chars = sum(len(c) for c in chunks)
        print(f"  Total chars: {total_chars}")
        print(f"  Avg chunk size: {total_chars // max(1, len(chunks))} chars")

        # Chunks that include section context
        contextual_chunks = sum(1 for c in chunks if c.startswith("[Section:"))
        hierarchical_chunks = sum(1 for c in chunks if ">>>" in c or "(prev:" in c or "(next:" in c)
        print(f"  Context-prefixed chunks: {contextual_chunks}")
        print(f"  Hierarchical chunks: {hierarchical_chunks}")


if __name__ == "__main__":
    main()
