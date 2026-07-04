#!/usr/bin/env python3
# Status: experimental
# Path: none — Fact 5 vs V6 dual comparison test
"""Fact 5 vs V6 Comparison: Increase "Max 4 facts" to "Max 5 facts" in detective prompt.

Dual extraction: inference (detective, max 5) + inference (exploratory) concurrently via WorkStealer.
Then FactArbiter + V6 quality filters. Compare with V6 baseline (max 4, same pipeline).

Hypothesis: +1 fact/chunk should not increase hallucination rate on 4B.
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

from lib.arbiter import FactArbiter, FactStatus
from lib.test_common import log, test_complete, test_heartbeat, test_setup
from lib.work_steal import WorkStealer

RESULTS_DIR = "/tmp/extraction_model_test_v5"
V6_PATH = os.path.join(RESULTS_DIR, "v6_filtered_result.json")
LONG_TEXT_PATH = "/tmp/long_turn_text.txt"

INFERENCE_PORT = 8082
CHUNK_TIMEOUT = 600
FACT5_DIR = "/tmp/extraction_model_test_fact5"
os.makedirs(FACT5_DIR, exist_ok=True)

# ── Prompts ──

YAML_PATH = os.path.join(SCRIPTS_DIR, "docs", "extraction-prompts.yaml")
with open(YAML_PATH) as f:
    PROMPTS_YAML = yaml.safe_load(f)


def get_prompt(prompt_id):
    for p in PROMPTS_YAML["prompts"]:
        if p["id"] == prompt_id:
            return p["prompt"]
    raise ValueError(f"Prompt {prompt_id} not found")


# Detective Max 5 (from extraction-prompts.yaml)
DETECTIVE_PROMPT = get_prompt("detective_5b")
# Exploratory Max 5 (from extraction-prompts.yaml)
EXPLORATORY_PROMPT = get_prompt("exploratory_5b")

DETECTIVE_JSON_FMT = '\n\nOutput ONLY valid JSON. No other text:\n{"facts": [{"subject": "...", "predicate": "...", "object": "..."}]}'
EXPLORATORY_JSON_FMT = '\n\nOutput ONLY valid JSON. No other text:\n{"facts": [{"subject": "...", "predicate": "...", "object": "...", "evidence": "..."}]}'

# ── Model management ──

INFERENCE_ENV = "/opt/ai_data/scripts/current-mode-inference.env"
INFERENCE_SVC = "devforge-inference"


def _read_env(path):
    with open(path) as f:
        return f.read()


def _write_env(path, content):
    with open(path, "w") as f:
        f.write(content)


def restart_container(port, service, timeout=300):
    os.system(f"systemctl --user restart {service} 2>&1")
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=5) as r:
                if r.status == 200:
                    log(f"  :{port} ready ({time.time() - t0:.0f}s)")
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


def load_4b_both():
    """Switch inference to 4B model for side-by-side comparison."""
    inference_content = (
        "MODE=day\nMODEL_NAME=day-extractor\n"
        "MODEL_FILE=Qwen3-4B-Instruct-Q8_0.gguf\n"
        "THREADS=2\nTHREADS_BATCH=2\n"
        f"CTX_SIZE=4096\nPORT={INFERENCE_PORT}\n"
        "CPUS=\n"
    )

    _write_env(INFERENCE_ENV, inference_content)

    log("Starting inference (4B, exploratory)...")
    ok = restart_container(INFERENCE_PORT, INFERENCE_SVC)

    if not ok:
        log("WARN: inference failed")
    return ok


# ── Chunking ──


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


# ── Phase 1: Dual extraction ──


def call_model(port, system, user_msg, max_tokens=768, timeout=300):
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


def phase1_dual_extraction():
    """Dual extraction: inference (detective max 5) + inference (exploratory) via WorkStealer."""
    log(f"\n{'=' * 60}")
    log("Phase 1 — Dual Extraction (detective Max 5 + exploratory)")
    log(f"{'=' * 60}")

    with open(LONG_TEXT_PATH) as f:
        text = f.read()
    chunks = split_atomic(text)
    log(f"  Atomic chunks: {len(chunks)}")

    items = []
    for ci, ct in enumerate(chunks):
        items.append({"chunk_idx": ci, "chunk_text": ct, "role": "detective"})
        items.append({"chunk_idx": ci, "chunk_text": ct, "role": "exploratory"})

    def process_chunk(port, item, timeout):
        ci = item["chunk_idx"]
        role = item["role"]
        prompt = (
            DETECTIVE_PROMPT + DETECTIVE_JSON_FMT
            if role == "detective"
            else EXPLORATORY_PROMPT + EXPLORATORY_JSON_FMT
        )
        msg = f"[Text]\n{item['chunk_text']}\n\n[Output]"
        t1 = time.time()
        try:
            raw, usage = call_model(port, prompt, msg, timeout=timeout)
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
        tok = usage.get("completion_tokens", "?")
        elapsed = time.time() - t1
        log(f"    ch{ci} {role}: {len(facts)} facts ({tok} tok, {elapsed:.0f}s)")
        return {
            "ok": error is None,
            "chunk_idx": ci,
            "role": role,
            "port": port,
            "facts": facts,
            "error": error,
            "token_count": tok,
            "elapsed": elapsed,
        }

    ports = [INFERENCE_PORT]
    # Detect which ports are alive
    alive_ports = []
    for p in ports:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{p}/health")
            with urllib.request.urlopen(req, timeout=5) as r:
                if r.status == 200:
                    alive_ports.append(p)
        except Exception:
            pass

    if not alive_ports:
        log("ERROR: No model ports available")
        return [], chunks

    log(f"  Active ports: {alive_ports}")
    stealer = WorkStealer(ports=alive_ports, item_timeout=CHUNK_TIMEOUT)
    stealer.run(items=items, process_fn=process_chunk, progress_cb=lambda d, t: None)
    return stealer.results, chunks


def pair_results(results):
    detective = {r["chunk_idx"]: r for r in results if r.get("role") == "detective"}
    exploratory = {r["chunk_idx"]: r for r in results if r.get("role") == "exploratory"}
    all_idxs = sorted(set(list(detective.keys()) + list(exploratory.keys())))
    pairs = []
    for ci in all_idxs:
        d = detective.get(ci, {"ok": False, "facts": [], "error": "missing"})
        e = exploratory.get(ci, {"ok": False, "facts": [], "error": "missing"})
        pairs.append(
            {
                "chunk_idx": ci,
                "a_facts": d["facts"],
                "a_ok": d["ok"],
                "b_facts": e["facts"],
                "b_ok": e["ok"],
                "a_error": d.get("error"),
                "b_error": e.get("error"),
            }
        )
    return pairs


# ── Phase 2: Arbiter ──


def phase2_consolidate(pairs):
    """Python FactArbiter + CONFLICT-only LLM fallback."""
    log(f"\n{'=' * 60}")
    log("Phase 2 — Python FactArbiter + LLM CONFLICT Fallback")
    log(f"{'=' * 60}")

    arbiter = FactArbiter(threshold=0.85, conflict_threshold=0.90)
    conflict_count = 0
    conflict_resolved = {"CONSENSUS": 0, "CONTRADICT": 0, "DIFFERENT_ASPECT": 0, "ERROR": 0}
    out_chunks = []
    total_facts = 0
    t0 = time.time()

    for pi, p in enumerate(pairs):
        ci = p["chunk_idx"]
        a_facts = p.get("a_facts") or []
        b_facts = p.get("b_facts") or []

        if not a_facts and not b_facts:
            out_chunks.append({"chunk_idx": ci, "a_in": 0, "b_in": 0, "n_out": 0, "facts": []})
            continue

        refs = arbiter.consolidate_with_fallback(
            a_facts,
            b_facts,
            endpoint=f"http://127.0.0.1:{INFERENCE_PORT}/v1/chat/completions",
        )

        for ref in refs:
            if ref.similarity == 0.0 and ref.a_idx is not None:
                conflict_count += 1
                if ref.status == FactStatus.CONSENSUS:
                    conflict_resolved["CONSENSUS"] += 1
                elif ref.status == FactStatus.UNIQUE_A:
                    conflict_resolved["DIFFERENT_ASPECT"] += 1
                else:
                    conflict_resolved["ERROR"] += 1

        facts_out = [r.to_dict() for r in refs]
        total_facts += len(facts_out)
        out_chunks.append(
            {
                "chunk_idx": ci,
                "a_in": len(a_facts),
                "b_in": len(b_facts),
                "n_out": len(facts_out),
                "facts": facts_out,
            }
        )
        log(f"  ch{ci}: A={len(a_facts)} B={len(b_facts)} → {len(facts_out)} facts")

    elapsed = time.time() - t0

    status_counts = {"CONSENSUS": 0, "UNIQUE_A": 0, "UNIQUE_B": 0, "CONFLICT": 0}
    for c in out_chunks:
        for f in c["facts"]:
            s = f.get("status", "")
            if s in status_counts:
                status_counts[s] += 1

    result = {
        "ok": True,
        "elapsed_s": round(elapsed, 3),
        "total_chunks": len(pairs),
        "total_facts": total_facts,
        "status_counts": status_counts,
        "conflict_count": conflict_count,
        "conflict_resolved": conflict_resolved,
        "chunks": out_chunks,
    }
    with open(os.path.join(FACT5_DIR, "fact5_arbiter_result.json"), "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    log(f"\n  Total facts: {total_facts}")
    log(f"  Status: {status_counts}")
    log(f"  CONFLICT: {conflict_count}, resolved: {conflict_resolved}")
    return result


# ── Phase 3: V6 quality filters ──


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
    """Apply V6 quality filters to arbiter result."""
    log(f"\n{'=' * 60}")
    log("Phase 3 — V6 Quality Filters on Fact 5 Output")
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

    v6_result = dict(result)
    v6_result["chunks"] = chunks
    v6_result["total_facts"] = final_facts
    v6_result["quality_filters"] = {
        "metadata_removed": metadata_removed,
        "cross_chunk_dedup_removed": dedup_removed,
        "predicates_rewritten": rewritten,
        "baseline_facts": total_before,
    }
    v6_result["prompt"] = "detective_5b (max 5)"
    with open(os.path.join(FACT5_DIR, "fact5_filtered_result.json"), "w") as f:
        json.dump(v6_result, f, indent=2, ensure_ascii=False)
    return v6_result


# ── Comparison report ──


def compare_with_v6(fact5_result, v6_result):
    log(f"\n{'=' * 60}")
    log("COMPARISON: Fact 5 (dual, max 5) vs V6 (dual, max 4)")
    log(f"{'=' * 60}")

    # Count detective raw facts from fact 5
    f5_a_facts = 0
    for c in fact5_result["chunks"]:
        f5_a_facts += c["a_in"]

    v6_facts = v6_result["total_facts"]
    v6_status = v6_result["status_counts"]
    v6_filters_data = v6_result.get("quality_filters", {})
    v6_a_facts = v6_result.get("detective_raw_facts", sum(c["a_in"] for c in v6_result["chunks"]))

    f5_total = fact5_result["total_facts"]
    f5_status = fact5_result["status_counts"]
    f5_filters_data = fact5_result.get("quality_filters", {})

    log("\n  ┌──────────────────────┬──────────┬──────────┬──────────┐")
    log("  │ Metric               │ V6 (max4)│ F5 (max5)│ Change   │")
    log("  ├──────────────────────┼──────────┼──────────┼──────────┤")
    log(
        f"  │ Detective A (raw)    │ {v6_a_facts:<8} │ {f5_a_facts:<8} │ {_delta(f5_a_facts - v6_a_facts):<8} │"
    )

    combined_metrics = {
        "total_facts": (v6_facts, f5_total),
        "CONSENSUS": (v6_status.get("CONSENSUS", 0), f5_status.get("CONSENSUS", 0)),
        "UNIQUE_A": (v6_status.get("UNIQUE_A", 0), f5_status.get("UNIQUE_A", 0)),
        "UNIQUE_B": (v6_status.get("UNIQUE_B", 0), f5_status.get("UNIQUE_B", 0)),
        "CONFLICT": (v6_status.get("CONFLICT", 0), f5_status.get("CONFLICT", 0)),
    }
    for metric, (v, f) in combined_metrics.items():
        log(f"  │ {metric:<20} │ {v:<8} │ {f:<8} │ {_delta(f - v):<8} │")

    for metric in ["metadata_removed", "cross_chunk_dedup_removed", "predicates_rewritten"]:
        v = v6_filters_data.get(metric, 0)
        f = f5_filters_data.get(metric, 0)
        log(f"  │ {metric:<20} │ {v:<8} │ {f:<8} │ {_delta(f - v):<8} │")

    log("  └──────────────────────┴──────────┴──────────┴──────────┘")

    # Quality analysis
    log("\n  Analysis:")
    a_delta = f5_a_facts - v6_a_facts
    if a_delta > 0:
        log(f"  + {a_delta} more raw detective facts (max 4→5 effect)")
    elif a_delta == 0:
        log("  No change in raw detective facts (constraint not binding)")

    f_delta = f5_total - v6_facts
    if f_delta > 0:
        log(f"  + {f_delta} more facts after arbiter + filters")

    if f5_filters_data.get("metadata_removed", 0) > v6_filters_data.get("metadata_removed", 0):
        meta_diff = f5_filters_data["metadata_removed"] - v6_filters_data["metadata_removed"]
        log(f"  ⚠ +{meta_diff} more metadata noise removed — more MCP artifacts being extracted")

    f5_ub = f5_status.get("UNIQUE_B", 0)
    f5_ua = max(f5_status.get("UNIQUE_A", 0), 1)
    v6_ub = v6_status.get("UNIQUE_B", 0)
    v6_ua = max(v6_status.get("UNIQUE_A", 0), 1)
    log(f"  UNIQUE_B/A ratio: V6={v6_ub / v6_ua:.2f} F5={f5_ub / f5_ua:.2f}")

    comparison = {
        "v6": {
            "total_facts": v6_facts,
            "status_counts": v6_status,
            "quality_filters": v6_filters_data,
            "detective_raw": v6_a_facts,
        },
        "fact5": {
            "total_facts": f5_total,
            "status_counts": f5_status,
            "quality_filters": f5_filters_data,
            "detective_raw": f5_a_facts,
        },
        "delta": {
            "total_facts": f_delta,
            "detective_raw": a_delta,
            "prompt_change": "max 4 → max 5",
        },
    }
    with open(os.path.join(FACT5_DIR, "comparison.json"), "w") as f:
        json.dump(comparison, f, indent=2, ensure_ascii=False)
    log(f"\n  Full comparison: {FACT5_DIR}/comparison.json")


def _delta(val):
    if val > 0:
        return f"+{val}"
    return str(val)


# ── Main ──


def main():
    log("=" * 60)
    log("FACT 5 vs V6 COMPARISON")
    log("Single inference extraction")
    log(f"Model: Qwen3-4B-Instruct-Q8_0 on :{INFERENCE_PORT}")
    log(f"Chunks: from {LONG_TEXT_PATH}")
    log(f"Baseline: {V6_PATH}")
    log("=" * 60)

    TEST = test_setup(
        "fact5_comparison",
        "Fact 5: dual, Max 5 detective vs V6 (max 4) comparison",
    )

    # Load V6 baseline
    with open(V6_PATH) as f:
        v6_result = json.load(f)
    log(f"\nV6 baseline: {v6_result['total_facts']} facts after filters")
    with open(os.path.join(RESULTS_DIR, "pairs_v5.json")) as f:
        v6_pairs = json.load(f)
    v6_a_facts = sum(len(p.get("a_facts", [])) for p in v6_pairs)
    v6_result["detective_raw_facts"] = v6_a_facts
    log(f"V6 detective raw: {v6_a_facts} facts")

    # Save current envs
    saved_envs = {}
    for env_path in [INFERENCE_ENV]:
        if os.path.exists(env_path):
            saved_envs[env_path] = _read_env(env_path)

    # Stop day cycle
    os.system(
        "systemctl --user stop devforge-day-cycle.service devforge-day-cycle.timer 2>/dev/null"
    )

    try:
        # Load 4B on both pods
        test_heartbeat("Loading 4B on inference")
        if not load_4b_both():
            test_complete("FAILED: could not start both pods with 4B model")
            return

        # Phase 1: dual extraction
        test_heartbeat("Phase 1: dual extraction (max 5 + exploratory)")
        results_ab, chunks = phase1_dual_extraction()
        if not results_ab:
            test_complete("Phase 1 failed: no extraction results")
            return

        pairs = pair_results(results_ab)
        a_ok = sum(1 for p in pairs if p["a_ok"])
        b_ok = sum(1 for p in pairs if p["b_ok"])
        a_facts = sum(len(p["a_facts"]) for p in pairs)
        b_facts = sum(len(p["b_facts"]) for p in pairs)
        log(f"\n  Detective (max 5): {a_ok}/{len(pairs)} ok, {a_facts} facts")
        log(f"  Exploratory: {b_ok}/{len(pairs)} ok, {b_facts} facts")

        # Save pairs
        pairs_path = os.path.join(FACT5_DIR, "pairs_fact5.json")
        with open(pairs_path, "w") as f:
            json.dump(pairs, f, indent=2, ensure_ascii=False)
        log(f"  Pairs saved: {pairs_path}")

        # Phase 2: arbiter
        test_heartbeat("Phase 2: arbiter consolidation")
        arbiter_result = phase2_consolidate(pairs)

        # Phase 3: V6 filters on fact 5
        test_heartbeat("Phase 3: V6 quality filters")
        fact5_result = v6_filters(arbiter_result)

        # Comparison
        compare_with_v6(fact5_result, v6_result)

    finally:
        # Restore inference
        if INFERENCE_ENV in saved_envs:
            log("\nRestoring inference...")
            _write_env(INFERENCE_ENV, saved_envs[INFERENCE_ENV])
            restart_container(INFERENCE_PORT, INFERENCE_SVC)

        # Restart day cycle
        os.system(
            "systemctl --user start devforge-day-cycle.service devforge-day-cycle.timer 2>/dev/null"
        )
        log("Day cycle restarted")

    test_complete(
        f"Fact 5 done — {fact5_result['total_facts']} facts after V6 filters "
        f"(V6: {v6_result['total_facts']}, delta: {fact5_result['total_facts'] - v6_result['total_facts']})"
    )


if __name__ == "__main__":
    main()
