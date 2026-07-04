#!/usr/bin/env python3
# Status: experimental
# Path: none — V6 quality filter test for arbiter output
"""V6 Quality Filters: Post-processing improvements for arbiter output.

Loads V5 arbiter result, applies 3 post-processing quality filters:
  1. Metadata Filter — removes MCP JSON leakage, operational metadata
  2. Cross-chunk Dedup — SequenceMatcher dedup across chunk boundaries
  3. Predicate Rewrite — evidence-based generic predicate replacement

Reports before/after quality metrics.
"""

import json
import os
import re
import sys
from difflib import SequenceMatcher

SCRIPTS_DIR = "/opt/projects/server/scripts"
sys.path.insert(0, SCRIPTS_DIR)

from lib.test_common import log, test_complete, test_heartbeat, test_setup

RESULTS_DIR = "/tmp/extraction_model_test_v5"
V5_PATH = os.path.join(RESULTS_DIR, "v5_arbiter_result.json")
V6_PATH = os.path.join(RESULTS_DIR, "v6_filtered_result.json")

# ── Constants ──────────────────────────────────────────────────────

_GENERIC_PREDICATES = frozenset(
    {
        "is",
        "has",
        "have",
        "use",
        "uses",
        "used",
        "was",
        "were",
        "does",
        "do",
        "be",
        "are",
        "called",
        "known",
    }
)

_METADATA_SUBJECT_PATTERNS = re.compile(r"^(claude pro(\s|$)|test \d+$|mcp\b)", re.IGNORECASE)

_OPERATIONAL_EVIDENCE = re.compile(
    r"(available_functions|tool_call|system_message|user_role|"
    r"i am a|let me |i will |i.ll )",
    re.IGNORECASE,
)

DEDUP_THRESHOLD = 0.85


# ── Filter 1: Metadata Noise Filter ───────────────────────────────


def filter_metadata(chunks):
    """Remove facts that are MCP/operational metadata noise.

    Removal conditions:
    - Subject matches metadata pattern AND evidence is short/absent
    - Evidence contains operational patterns (MCP JSON artifacts)
    """
    kept_total = 0
    removed_total = 0
    removed_samples = []

    for chunk in chunks:
        filtered = []
        for f in chunk.get("facts", []):
            subj = str(f.get("subject", ""))
            ev = str(f.get("evidence", ""))
            obj = str(f.get("object", ""))

            # Condition 1: metadata subject + weak evidence
            if _METADATA_SUBJECT_PATTERNS.match(subj) and (not ev or len(ev) < 20):
                removed_total += 1
                removed_samples.append(
                    f"[meta subj] {subj} -> {f.get('predicate', '?')} = {obj[:60]}"
                )
                continue

            # Condition 2: evidence contains operational/MCP artifacts
            if _OPERATIONAL_EVIDENCE.search(ev):
                removed_total += 1
                removed_samples.append(f"[op ev] {subj} -> {f.get('predicate', '?')} = {obj[:60]}")
                continue

            filtered.append(f)
            kept_total += 1

        chunk["facts"] = filtered

    return chunks, kept_total, removed_total, removed_samples


# ── Filter 2: Cross-chunk Dedup ───────────────────────────────────


def _fact_text(fact):
    subj = str(fact.get("subject", ""))
    pred = str(fact.get("predicate", ""))
    obj = str(fact.get("object", ""))
    return (subj + " " + pred + " " + obj).lower()


def _entry_key(entry):
    """Sort key: UNIQUE_B (has evidence) > CONSENSUS > UNIQUE_A > CONFLICT."""
    _, fact = entry
    status = fact.get("status", "")
    ev = fact.get("evidence", "")
    order = {"UNIQUE_B": 3, "CONSENSUS": 2, "UNIQUE_A": 1, "CONFLICT": 0}
    base = order.get(status, 0)
    ev_bonus = min(len(ev) / 200, 1.0) if ev else 0
    return base + ev_bonus


def dedup_cross_chunk(chunks, threshold=DEDUP_THRESHOLD):
    """Greedy cross-chunk dedup, keeping the higher-quality fact."""
    entries = []  # [(chunk_idx, fact_dict)]
    for chunk in chunks:
        ci = chunk["chunk_idx"]
        for f in chunk.get("facts", []):
            entries.append((ci, f))

    texts = [_fact_text(f) for _, f in entries]
    keep = [True] * len(entries)

    removed_pairs = []
    for i in range(len(entries)):
        if not keep[i]:
            continue
        for j in range(i + 1, len(entries)):
            if not keep[j]:
                continue
            # Same chunk — skip (already handled by arbiter)
            if entries[i][0] == entries[j][0]:
                continue
            # Short texts (both <20 chars) — skip
            if len(texts[i]) < 20 and len(texts[j]) < 20:
                continue
            ratio = SequenceMatcher(None, texts[i], texts[j]).ratio()
            if ratio >= threshold:
                key_i = _entry_key(entries[i])
                key_j = _entry_key(entries[j])
                if key_i >= key_j:
                    keep[j] = False
                    removed_pairs.append((j, i, ratio, entries[j][1]))
                else:
                    keep[i] = False
                    removed_pairs.append((i, j, ratio, entries[i][1]))
                    break  # i is gone

    # Rebuild chunks
    new_chunks = {
        c["chunk_idx"]: {
            "chunk_idx": c["chunk_idx"],
            "facts": [],
            "a_in": c.get("a_in", 0),
            "b_in": c.get("b_in", 0),
        }
        for c in chunks
    }

    for idx, flag in enumerate(keep):
        if flag:
            ci, f = entries[idx]
            new_chunks[ci]["facts"].append(f)

    result = list(new_chunks.values())
    for c in result:
        c["n_out"] = len(c["facts"])

    kept = sum(1 for k in keep if k)
    removed = sum(1 for k in keep if not k)

    return result, kept, removed, removed_pairs[:10]


# ── Filter 3: Predicate Rewrite ───────────────────────────────────


def _find_verb_in_evidence(subject, evidence):
    """Extract a concrete verb from evidence near the subject mention."""
    if not evidence:
        return ""
    subj_lower = subject.lower().strip()
    ev_lower = evidence.lower().strip()

    words = ev_lower.split()
    subj_parts = subj_lower.split()
    if not subj_parts:
        return ""

    stop = {
        "the",
        "a",
        "an",
        "to",
        "in",
        "on",
        "at",
        "for",
        "of",
        "and",
        "or",
        "with",
        "by",
        "from",
        "as",
        "is",
        "was",
        "be",
    }

    # Pattern: find subject in evidence, grab next verb-like word
    for i in range(len(words) - len(subj_parts)):
        matches = True
        for k, part in enumerate(subj_parts):
            if words[i + k].strip(".,;:!?()[]{}'\"") != part:
                matches = False
                break
        if not matches:
            continue
        look = words[i + len(subj_parts) : i + len(subj_parts) + 8]
        for w in look:
            wc = w.strip(".,;:!?()[]{}'\"")
            if len(wc) > 2 and wc not in stop:
                if wc.endswith(("s", "ed", "en", "ing")) or wc.endswith("다"):
                    return wc
        break

    # Fallback: first verb-like word in evidence
    for w in words[:15]:
        wc = w.strip(".,;:!?()[]{}'\"")
        if len(wc) > 2 and wc not in stop:
            if wc.endswith(("s", "ed", "en", "ing")) or wc.endswith("다"):
                return wc

    return ""


def rewrite_predicates(chunks):
    """Replace generic predicates with evidence-derived action verbs."""
    rewritten = 0
    unchanged = 0
    rewrites = []

    for chunk in chunks:
        for f in chunk.get("facts", []):
            pred = str(f.get("predicate", ""))
            pred_lower = pred.lower().strip()

            if pred_lower not in _GENERIC_PREDICATES:
                unchanged += 1
                continue

            evidence = str(f.get("evidence", ""))
            if not evidence:
                unchanged += 1
                continue

            subject = str(f.get("subject", ""))
            verb = _find_verb_in_evidence(subject, evidence)

            if verb:
                new_pred = re.sub(r"[^a-z0-9_]", "", re.sub(r"[\s\-]+", "_", verb.lower()))
                if new_pred and len(new_pred) >= 3 and new_pred != pred_lower:
                    rewrites.append(f"  {pred} -> {new_pred} ({subject})")
                    f["predicate"] = new_pred
                    rewritten += 1
                    continue

            unchanged += 1

    return chunks, rewritten, unchanged, rewrites


# ── Main ──────────────────────────────────────────────────────────


def main():
    log("=" * 60)
    log("V6 Quality Filters — Post-processing for Arbiter Output")
    log("=" * 60)

    TEST = test_setup(
        "quality_filters_v6",
        "V6: post-processing quality filters on arbiter output",
    )

    with open(V5_PATH) as f:
        v5 = json.load(f)

    total_before = v5["total_facts"]
    chunks = v5["chunks"]
    log(f"Baseline (V5): {total_before} facts, {len(chunks)} chunks")

    # ── Phase 3a: Metadata Filter ──
    test_heartbeat("Phase 3a: metadata filter")
    log(f"\n{'─' * 50}")
    log("Phase 3a — Metadata Noise Filter")
    log(f"{'─' * 50}")
    chunks, kept_m, removed_m, samples_m = filter_metadata(chunks)
    log(f"  Kept: {kept_m}, Removed: {removed_m}")
    if removed_m and samples_m:
        log("  Removed:")
        for s in samples_m[:8]:
            log(f"    {s}")

    # ── Phase 3b: Cross-chunk Dedup ──
    test_heartbeat("Phase 3b: cross-chunk dedup")
    log(f"\n{'─' * 50}")
    log(f"Phase 3b — Cross-chunk Dedup (threshold={DEDUP_THRESHOLD})")
    log(f"{'─' * 50}")
    chunks, kept_d, removed_d, pairs_d = dedup_cross_chunk(chunks)
    log(f"  Kept: {kept_d}, Removed: {removed_d}")
    if removed_d and pairs_d:
        log("  Sample (removed -> kept):")
        for ridx, kidx, ratio, f_removed in pairs_d[:6]:
            subj = f_removed.get("subject", "?")
            pred = f_removed.get("predicate", "?")
            obj = f_removed.get("object", "?")
            log(f"    [{subj}] {pred} = {str(obj)[:50]}  (sim={ratio:.2f})")

    # ── Phase 3c: Predicate Rewrite ──
    test_heartbeat("Phase 3c: predicate rewrite")
    log(f"\n{'─' * 50}")
    log("Phase 3c — Predicate Rewrite (evidence-based)")
    log(f"{'─' * 50}")
    chunks, rewrote, unchanged, rewrites = rewrite_predicates(chunks)
    log(f"  Rewritten: {rewrote}, Unchanged: {unchanged}")
    if rewrote and rewrites:
        log("  Changes:")
        for r in rewrites[:12]:
            log(f"    {r}")

    # ── Summary ──
    final_facts = sum(len(c["facts"]) for c in chunks)
    pct = ((total_before - final_facts) / total_before * 100) if total_before else 0

    log(f"\n{'=' * 60}")
    log("V6 QUALITY IMPROVEMENT SUMMARY")
    log(f"{'=' * 60}")
    log(f"  Before (V5): {total_before} facts")
    log(f"  After  (V6): {final_facts} facts")
    log(f"  Metadata removed:   {removed_m}")
    log(f"  Cross-chunk dedup:  {removed_d}")
    log(f"  Predicates rewritten: {rewrote}")
    log(f"  Net reduction: {total_before - final_facts} ({pct:.1f}%)")

    # Save V6 result
    v6_result = dict(v5)
    v6_result["chunks"] = chunks
    v6_result["total_facts"] = final_facts
    v6_result["quality_filters"] = {
        "metadata_removed": removed_m,
        "cross_chunk_dedup_removed": removed_d,
        "predicates_rewritten": rewrote,
        "baseline_facts": total_before,
    }
    with open(V6_PATH, "w") as f:
        json.dump(v6_result, f, indent=2, ensure_ascii=False)
    log(f"\n  Saved to: {V6_PATH}")

    test_complete(
        f"V6 done — {final_facts} facts ({pct:.1f}% reduction), "
        f"{removed_m} metadata, {removed_d} dedup, {rewrote} rewrites"
    )


if __name__ == "__main__":
    main()
