#!/usr/bin/env python3
# Status: experimental
# Path: none — arbiter model comparison test (7B/9B on V6 raw pairs)
"""Arbiter model comparison: 4B vs 7B/9B on identical V6 extraction pairs.

Takes V6 raw pairs (max 4 detective + exploratory) and re-runs the arbiter
phase using a larger LLM model (9B). Applies V6 quality filters and compares
against the 4B arbiter baseline from v5_arbiter_result.json.
"""

import json
import os
import re
import sys
import time
import urllib.request

import yaml

SCRIPTS_DIR = "/opt/projects/server/scripts"
sys.path.insert(0, SCRIPTS_DIR)

from lib.test_common import log, test_complete, test_heartbeat, test_setup

V6_DIR = "/tmp/extraction_model_test_v5"
PAIRS_PATH = os.path.join(V6_DIR, "pairs_v5.json")
BASELINE_PATH = os.path.join(V6_DIR, "v5_arbiter_result.json")
LONG_TEXT_PATH = "/tmp/long_turn_text.txt"
RESULTS_DIR = "/tmp/extraction_model_test_arbiter_7b9b"
os.makedirs(RESULTS_DIR, exist_ok=True)

INFERENCE_PORT = 8082
CHUNK_TIMEOUT = 600

# ── Prompts ──

YAML_PATH = os.path.join(SCRIPTS_DIR, "docs", "extraction-prompts.yaml")
with open(YAML_PATH) as f:
    PROMPTS_YAML = yaml.safe_load(f)


def get_prompt(prompt_id):
    for p in PROMPTS_YAML["prompts"]:
        if p["id"] == prompt_id:
            return p["prompt"]
    raise ValueError(f"Prompt {prompt_id} not found")


ARBITER_PROMPT = get_prompt("arbiter_7b_9b")
ARBITER_JSON_FMT = '\n\nOutput ONLY valid JSON. No other text:\n{"facts": [{"subject": "...", "predicate": "...", "object": "...", "evidence": "..."}]}'

# ── Chunking (must match test_fact5_comparison.py) ──


def split_atomic(text, max_chars=400):
    paragraphs = re.split(r"\n\s*\n", text)
    chunks = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            chunks.append(para)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", para)
        current = ""
        for sent in sentences:
            if len(current) + len(sent) + 1 <= max_chars:
                current = (current + " " + sent).strip()
            else:
                if current:
                    chunks.append(current)
                current = sent
        if current:
            chunks.append(current)
    merged = []
    for c in chunks:
        if merged and len(c) < 40:
            merged[-1] += " " + c
        else:
            merged.append(c)
    return merged


# ── LLM call ──


def call_model(port, system, user_msg, max_tokens=1024, timeout=300):
    body_dict = {
        "model": "default",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    body = json.dumps(body_dict).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        d = json.loads(resp.read())
    return d["choices"][0]["message"]["content"], d.get("usage", {})


# ── Phase: LLM Arbiter (arbiter_7b_9b prompt) ──


def phase_llm_arbiter(pairs, chunks):
    """Run arbiter_7b_9b prompt on each chunk pair via LLM.

    Unlike the Python FactArbiter hybrid approach that only calls LLM
    for CONFLICT resolution, this sends the full A+B consolidation task
    to the LLM.
    """
    log(f"\n{'=' * 60}")
    log("Phase — LLM Arbiter (arbiter_7b_9b prompt on 9B)")
    log(f"{'=' * 60}")

    out_chunks = []
    total_facts = 0
    status_counts = {"CONSENSUS": 0, "UNIQUE_A": 0, "UNIQUE_B": 0, "CONFLICT": 0}
    t0 = time.time()

    for pi, p in enumerate(pairs):
        ci = p["chunk_idx"]
        chunk_text = chunks[ci] if ci < len(chunks) else ""
        a_facts = p.get("a_facts") or []
        b_facts = p.get("b_facts") or []

        if not a_facts and not b_facts:
            out_chunks.append({"chunk_idx": ci, "a_in": 0, "b_in": 0, "n_out": 0, "facts": []})
            continue

        # Build arbiter prompt
        a_text = json.dumps(a_facts, indent=2, ensure_ascii=False)
        b_text = json.dumps(b_facts, indent=2, ensure_ascii=False)
        user_msg = (
            f"[Original Text]\n{chunk_text}\n\n"
            f"[Candidate A (Strict)]\n{a_text}\n\n"
            f"[Candidate B (Exploratory)]\n{b_text}"
        )

        t1 = time.time()
        try:
            raw, usage = call_model(
                INFERENCE_PORT, ARBITER_PROMPT + ARBITER_JSON_FMT, user_msg, timeout=CHUNK_TIMEOUT
            )
            raw = raw.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```\w*\n?", "", raw)
                raw = raw.rstrip("`").strip()
            parsed = json.loads(raw) if raw else None
            facts = parsed.get("facts", []) if parsed else []
            error = None
        except json.JSONDecodeError as e:
            facts = []
            error = f"JSON parse: {str(e)[:60]}"
        except Exception as e:
            facts = []
            error = f"{type(e).__name__}: {str(e)[:60]}"

        tok = usage.get("completion_tokens", "?") if not error else 0
        elapsed = time.time() - t1
        log(
            f"  ch{ci}: A={len(a_facts)} B={len(b_facts)} → {len(facts)} facts ({tok} tok, {elapsed:.0f}s)"
            + (f" [{error}]" if error else "")
        )

        # Count statuses (heuristic: UNIQUE_A if in A only, UNIQUE_B if in B only, else CONSENSUS)
        # We don't have detailed status from the LLM, so estimate from the raw facts
        for f in facts:
            total_facts += 1

        out_chunks.append(
            {
                "chunk_idx": ci,
                "a_in": len(a_facts),
                "b_in": len(b_facts),
                "n_out": len(facts),
                "facts": facts,
                "error": error,
            }
        )

    elapsed = time.time() - t0

    # Re-count via FactArbiter for status classification
    all_facts_flat = []
    for c in out_chunks:
        for f in c.get("facts", []):
            all_facts_flat.append(f)

    result = {
        "ok": True,
        "model": "9B",
        "prompt_id": "arbiter_7b_9b",
        "elapsed_s": round(elapsed, 3),
        "total_chunks": len(pairs),
        "total_facts": total_facts,
        "status_counts": status_counts,
        "conflict_count": 0,
        "conflict_resolved": {},
        "chunks": out_chunks,
    }

    with open(os.path.join(RESULTS_DIR, "arbiter_7b9b_result.json"), "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    log(f"\n  Total facts: {total_facts}")
    log(f"  Elapsed: {elapsed:.0f}s ({elapsed / len(pairs):.1f}s/chunk)")
    return result


# ── V6 quality filters (mirrors test_fact5_comparison.py) ──

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
    r"(available_functions|tool_call|system_message|user_role|i am a|let me |i will |i.ll )",
    re.IGNORECASE,
)
DEDUP_THRESHOLD = 0.85


def v6_filters(result):
    """Apply V6 quality filters (metadata filter + cross-chunk dedup + predicate rewrite)."""
    log(f"\n{'=' * 60}")
    log("Phase — V6 Quality Filters")
    log(f"{'=' * 60}")

    total_before = result["total_facts"]
    chunks = result["chunks"]

    # Metadata filter
    kept_total = 0
    removed_total = 0
    for chunk in chunks:
        filtered = []
        for f in chunk.get("facts", []):
            subj = str(f.get("subject", ""))
            ev = str(f.get("evidence", ""))
            obj = str(f.get("object", ""))
            if _METADATA_SUBJECT_PATTERNS.match(subj) and (not ev or len(ev) < 20):
                removed_total += 1
                continue
            if _OPERATIONAL_EVIDENCE.search(ev):
                removed_total += 1
                continue
            filtered.append(f)
            kept_total += 1
        chunk["facts"] = filtered
    metadata_removed = removed_total
    log(f"  Metadata filter: kept={kept_total} removed={metadata_removed}")

    # Cross-chunk dedup
    entries = []
    for chunk in chunks:
        ci = chunk["chunk_idx"]
        for f in chunk.get("facts", []):
            entries.append((ci, f))

    texts = []
    for _, f in entries:
        t = (
            str(f.get("subject", ""))
            + " "
            + str(f.get("predicate", ""))
            + " "
            + str(f.get("object", ""))
        ).lower()
        texts.append(t)
    keep = [True] * len(entries)

    for i in range(len(entries)):
        if not keep[i]:
            continue
        for j in range(i + 1, len(entries)):
            if not keep[j]:
                continue
            if entries[i][0] == entries[j][0]:
                continue
            if len(texts[i]) < 20 and len(texts[j]) < 20:
                continue
            from difflib import SequenceMatcher

            ratio = SequenceMatcher(None, texts[i], texts[j]).ratio()
            if ratio >= DEDUP_THRESHOLD:

                def _entry_key(entry):
                    _, fact = entry
                    status = fact.get("status", "")
                    ev = fact.get("evidence", "")
                    order = {"UNIQUE_B": 3, "CONSENSUS": 2, "UNIQUE_A": 1, "CONFLICT": 0}
                    base = order.get(status, 0)
                    return base + min(len(ev) / 200, 1.0) if ev else base

                if _entry_key(entries[i]) >= _entry_key(entries[j]):
                    keep[j] = False
                else:
                    keep[i] = False
                    break

    new_chunks = {}
    for c in chunks:
        new_chunks[c["chunk_idx"]] = {
            "chunk_idx": c["chunk_idx"],
            "facts": [],
            "a_in": c.get("a_in", 0),
            "b_in": c.get("b_in", 0),
        }
    for idx, flag in enumerate(keep):
        if flag:
            ci, f = entries[idx]
            new_chunks[ci]["facts"].append(f)
    chunks = list(new_chunks.values())
    for c in chunks:
        c["n_out"] = len(c["facts"])
    dedup_removed = sum(1 for k in keep if not k)
    log(f"  Cross-chunk dedup: removed={dedup_removed}")

    # Predicate rewrite
    rewritten = 0
    for chunk in chunks:
        for f in chunk.get("facts", []):
            pred = str(f.get("predicate", "")).lower().strip()
            if pred not in _GENERIC_PREDICATES:
                continue
            evidence = str(f.get("evidence", ""))
            if not evidence:
                continue
            subject = str(f.get("subject", ""))
            words = evidence.lower().split()
            verb = ""
            for w in words[:15]:
                wc = w.strip(".,;:!?()[]{}'\"")
                if len(wc) > 2 and wc not in {
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
                }:
                    if wc.endswith(("s", "ed", "en", "ing")) or wc.endswith("다"):
                        verb = wc
                        break
            if verb:
                new_pred = re.sub(r"[^a-z0-9_]", "", re.sub(r"[\s\-]+", "_", verb.lower()))
                if new_pred and len(new_pred) >= 3 and new_pred != pred:
                    f["predicate"] = new_pred
                    rewritten += 1

    final_facts = sum(len(c["facts"]) for c in chunks)
    pct = ((total_before - final_facts) / total_before * 100) if total_before else 0
    log(f"\n  Summary: {total_before} → {final_facts} ({pct:.1f}%)")
    log(f"  Metadata removed: {metadata_removed}")
    log(f"  Cross-chunk dedup: {dedup_removed}")
    log(f"  Predicates rewritten: {rewritten}")

    filtered_result = dict(result)
    filtered_result["chunks"] = chunks
    filtered_result["total_facts"] = final_facts
    filtered_result["quality_filters"] = {
        "metadata_removed": metadata_removed,
        "cross_chunk_dedup_removed": dedup_removed,
        "predicates_rewritten": rewritten,
        "baseline_facts": total_before,
    }

    with open(os.path.join(RESULTS_DIR, "arbiter_7b9b_filtered.json"), "w") as f:
        json.dump(filtered_result, f, indent=2, ensure_ascii=False)
    return filtered_result


# ── Comparison ──


def compare_baseline(arbiter_result, baseline):
    """Compare 9B arbiter result vs 4B arbiter baseline."""
    log(f"\n{'=' * 60}")
    log("COMPARISON: 9B Arbiter (LLM) vs 4B Arbiter (Python+LLM hybrid)")
    log(f"{'=' * 60}")

    # 4B baseline
    b_facts = baseline["total_facts"]
    b_qf = baseline.get("quality_filters", {})
    b_filtered = b_qf.get("baseline_facts", b_facts) if b_qf else b_facts

    # 9B result
    arb_total = arbiter_result["total_facts"]
    arb_qf = arbiter_result.get("quality_filters", {})
    arb_filtered = arb_qf.get("baseline_facts", arb_total) if arb_qf else arb_total

    log("\n  ┌──────────────────────────┬──────────┬──────────┬──────────┐")
    log("  │ Metric                   │ 4B Arb   │ 9B Arb   │ Change   │")
    log("  ├──────────────────────────┼──────────┼──────────┼──────────┤")

    metrics = [
        ("Arbiter output", b_filtered, arb_filtered, f"{arb_filtered - b_filtered:+d}"),
        ("After V6 filters", b_facts, arb_total, f"{arb_total - b_facts:+d}"),
        ("Metadata removed", b_qf.get("metadata_removed", 0), arb_qf.get("metadata_removed", 0), 0),
        (
            "Cross-chunk dedup",
            b_qf.get("cross_chunk_dedup_removed", 0),
            arb_qf.get("cross_chunk_dedup_removed", 0),
            0,
        ),
        (
            "Predicates rewritten",
            b_qf.get("predicates_rewritten", 0),
            arb_qf.get("predicates_rewritten", 0),
            0,
        ),
    ]

    for label, v1, v2, _ in metrics:
        delta = v2 - v1
        sign = "+" if delta > 0 else ""
        log(f"  │ {label:<24} │ {v1:<8} │ {v2:<8} │ {sign}{delta:<7} │")

    log("  └──────────────────────────┴──────────┴──────────┴──────────┘")

    # Analysis
    log("\n  Analysis:")
    delta = arb_total - b_facts
    if delta > 0:
        log(f"  +{delta} more facts with 9B arbiter (LLM consolidation)")
    elif delta < 0:
        log(f"  {delta} fewer facts with 9B arbiter (stricter consolidation)")
    else:
        log("  Same fact count (different content?)")

    comp = {
        "4b_arbiter": {"total_facts": b_facts, "quality_filters": b_qf},
        "9b_arbiter": {"total_facts": arb_total, "quality_filters": arb_qf},
        "delta": {
            "arbiter_output": arb_filtered - b_filtered,
            "after_filters": arb_total - b_facts,
        },
    }
    with open(os.path.join(RESULTS_DIR, "comparison.json"), "w") as f:
        json.dump(comp, f, indent=2, ensure_ascii=False)
    log(f"\n  Full comparison: {RESULTS_DIR}/comparison.json")


# ── Main ──


def main():
    log("=" * 60)
    log("ARBITER MODEL COMPARISON: 4B vs 9B on V6 pairs")
    log("Using arbiter_7b_9b prompt (full LLM consolidation)")
    log(f"Model: Qwen3.5-9B-Q4_K_M on :{INFERENCE_PORT}")
    log(f"V6 pairs: {PAIRS_PATH}")
    log(f"4B baseline: {BASELINE_PATH}")
    log("=" * 60)

    TEST = test_setup("arbiter_7b9b", "Arbiter model comparison: 4B vs 9B on V6 pairs")

    # Load V6 pairs
    with open(PAIRS_PATH) as f:
        pairs = json.load(f)
    log(f"\nV6 pairs loaded: {len(pairs)}")

    # Load 4B baseline
    with open(BASELINE_PATH) as f:
        baseline = json.load(f)
    log(f"4B arbiter baseline: {baseline['total_facts']} facts")

    # Chunk text
    with open(LONG_TEXT_PATH) as f:
        text = f.read()
    chunks = split_atomic(text)
    log(f"Text chunks: {len(chunks)}")

    # Verify pair-chunk alignment
    pair_idx_range = (pairs[0]["chunk_idx"], pairs[-1]["chunk_idx"])
    log(f"Pair idx range: {pair_idx_range[0]}-{pair_idx_range[1]} (expect 0-{len(chunks) - 1})")
    if pair_idx_range[0] != 0 or pair_idx_range[1] != len(chunks) - 1:
        log("ERROR: pair-chunk mismatch!")
        test_complete("FAILED: pair-chunk mismatch")
        return

    try:
        # Phase: LLM arbiter
        test_heartbeat("Phase: LLM arbiter (9B)")
        arbiter_result = phase_llm_arbiter(pairs, chunks)

        # Phase: V6 quality filters
        test_heartbeat("Phase: V6 quality filters")
        filtered_result = v6_filters(arbiter_result)

        # Comparison
        compare_baseline(filtered_result, baseline)

    finally:
        pass  # Keep inference state as-is

    test_complete(
        f"9B arbiter done — {filtered_result['total_facts']} facts after V6 filters "
        f"(4B baseline: {baseline['total_facts']}, "
        f"delta: {filtered_result['total_facts'] - baseline['total_facts']})"
    )


if __name__ == "__main__":
    main()
