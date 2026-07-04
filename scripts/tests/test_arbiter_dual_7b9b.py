#!/usr/bin/env python3
# Status: experimental
# Path: none — arbiter model comparison: dual 9B vs dual 7B on V6 pairs
"""Dual arbiter comparison: 9B (2 instances) vs 7B (2 instances) on V6 raw pairs.

Takes V6 raw pairs (max 4 detective+exploratory) and re-runs the arbiter phase
using dual instances of 9B and 7B models via WorkStealer.
Compares against 4B arbiter baseline from v5_arbiter_result.json.
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
from lib.work_steal import WorkStealer

V6_DIR = "/tmp/extraction_model_test_v5"
PAIRS_PATH = os.path.join(V6_DIR, "pairs_v5.json")
BASELINE_PATH = os.path.join(V6_DIR, "v5_arbiter_result.json")
LONG_TEXT_PATH = "/tmp/long_turn_text.txt"
RESULTS_DIR = "/tmp/extraction_model_test_arbiter_7b9b"
os.makedirs(RESULTS_DIR, exist_ok=True)

PORT_8080 = 8080
INFERENCE_PORT = 8082
CHUNK_TIMEOUT = 600

INFERENCE_ENV = "/opt/ai_data/scripts/current-mode-inference.env"
INFERENCE_SVC = "devforge-inference"

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

# ── Helpers ──


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


def load_model_both(model_file, port_a, port_b, svc_a, svc_b, tag=""):
    """Switch inference to the given model on both port groups, restart twice."""
    tag = f" ({tag})" if tag else ""
    content_a = (
        "MODE=detective\nMODEL_NAME=day-extractor\n"
        f"MODEL_FILE={model_file}\n"
        "THREADS=4\nTHREADS_BATCH=4\nFLASH_ATTN=1\n"
        f"CTX_SIZE=4096\nPORT={port_a}\n"
        "UBATCH_SIZE=256\nPARALLEL=1\n"
        "CACHE_RAM=512\n"
    )
    inference_content = (
        "MODE=day\nMODEL_NAME=day-extractor\n"
        f"MODEL_FILE={model_file}\n"
        "THREADS=4\nTHREADS_BATCH=4\nFLASH_ATTN=1\n"
        f"CTX_SIZE=4096\nPORT={port_b}\n"
        "UBATCH_SIZE=256\nPARALLEL=1\n"
    )
    _write_env(INFERENCE_ENV, content_a)
    _write_env(INFERENCE_ENV, inference_content)

    log(f"Starting inference ({model_file}, :{port_a}){tag}...")
    a_ok = restart_container(port_a, svc_a, timeout=300)
    log(f"Starting inference ({model_file}, :{port_b}){tag}...")
    b_ok = restart_container(port_b, svc_b, timeout=300)
    if not a_ok or not b_ok:
        log(f"WARN: a_ok={a_ok} b_ok={b_ok}")
    return a_ok and b_ok


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


# ── Phase: Dual LLM Arbiter ──


def phase_dual_arbiter(pairs, chunks, ports, tag=""):
    """Run arbiter_7b_9b prompt on each chunk pair via WorkStealer (dual instances)."""
    tag_s = f" ({tag})" if tag else ""
    log(f"\n{'=' * 60}")
    log(f"Phase — Dual LLM Arbiter{tag_s}")
    log(f"{'=' * 60}")

    items = []
    for pi, p in enumerate(pairs):
        items.append({"chunk_idx": p["chunk_idx"]})

    def process_chunk(port, item, timeout):
        ci = item["chunk_idx"]
        p = pairs[ci]
        chunk_text = chunks[ci] if ci < len(chunks) else ""
        a_facts = p.get("a_facts") or []
        b_facts = p.get("b_facts") or []

        if not a_facts and not b_facts:
            return {
                "ok": True,
                "chunk_idx": ci,
                "port": port,
                "facts": [],
                "error": None,
                "token_count": 0,
                "elapsed": 0,
            }

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
                port, ARBITER_PROMPT + ARBITER_JSON_FMT, user_msg, timeout=timeout
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

        tok = usage.get("completion_tokens", 0) if not error else 0
        elapsed = time.time() - t1
        log(
            f"  ch{ci}: A={len(a_facts)} B={len(b_facts)} → {len(facts)} facts ({tok} tok, {elapsed:.0f}s)"
            + (f" [{error}]" if error else "")
        )

        return {
            "ok": error is None,
            "chunk_idx": ci,
            "port": port,
            "facts": facts,
            "error": error,
            "token_count": tok,
            "elapsed": elapsed,
        }

    stealer = WorkStealer(ports=ports, item_timeout=CHUNK_TIMEOUT)
    stealer.run(items=items, process_fn=process_chunk, progress_cb=lambda d, t: None)

    # Build result
    results = stealer.results
    out_chunks = []
    total_facts = 0
    for r in results:
        ci = r["chunk_idx"]
        p = pairs[ci]
        a_facts = p.get("a_facts") or []
        b_facts = p.get("b_facts") or []
        out_chunks.append(
            {
                "chunk_idx": ci,
                "a_in": len(a_facts),
                "b_in": len(b_facts),
                "n_out": len(r["facts"]),
                "facts": r["facts"],
                "error": r.get("error"),
            }
        )
        total_facts += len(r["facts"])

    ok_count = sum(1 for r in results if r["ok"])
    fail_count = sum(1 for r in results if not r["ok"])
    log(f"\n  Total facts: {total_facts} ({ok_count} ok, {fail_count} failed)")

    return {
        "ok": fail_count == 0,
        "elapsed_s": round(sum(r["elapsed"] for r in results if r["ok"]), 3),
        "total_chunks": len(pairs),
        "total_facts": total_facts,
        "status_counts": {},
        "chunks": out_chunks,
        "results": results,
    }


# ── V6 quality filters ──

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


def v6_filters(result, tag=""):
    """Apply V6 quality filters (metadata filter + cross-chunk dedup + predicate rewrite)."""
    tag_s = f" ({tag})" if tag else ""
    log(f"\n{'=' * 60}")
    log(f"Phase — V6 Quality Filters{tag_s}")
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
                    ev = fact.get("evidence", "")
                    return min(len(ev) / 200, 1.0) if ev else 0

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

    filtered = dict(result)
    filtered["chunks"] = chunks
    filtered["total_facts"] = final_facts
    filtered["quality_filters"] = {
        "metadata_removed": metadata_removed,
        "cross_chunk_dedup_removed": dedup_removed,
        "predicates_rewritten": rewritten,
        "baseline_facts": total_before,
    }
    return filtered


# ── Comparison ──


def compare_all(baseline_4b, result_9b, result_7b):
    """Compare 4B (Python+LLM hybrid) vs 9B arbiter vs 7B arbiter."""
    log(f"\n{'=' * 60}")
    log("COMPARISON: 4B (Python+LLM) vs 9B arbiter vs 7B arbiter")
    log(f"{'=' * 60}")

    def get(r, key):
        return r.get(key, 0)

    def get_qf(r, key):
        qf = r.get("quality_filters", {}) or {}
        return qf.get(key, 0)

    metrics = [
        ("Arbiter output", "total_facts"),
        ("After V6 filters", "total_facts"),
        ("Metadata removed", "metadata_removed"),
        ("Cross-chunk dedup", "cross_chunk_dedup_removed"),
        ("Predicates rewritten", "predicates_rewritten"),
    ]

    log("\n  ┌──────────────────────┬──────────┬──────────┬──────────┬──────────┐")
    log("  │ Metric               │ 4B Base  │ 9B Arb   │ 7B Arb   │ Δ(9B-7B) │")
    log("  ├──────────────────────┼──────────┼──────────┼──────────┼──────────┤")

    for label, key in metrics:
        v4 = (
            baseline_4b["total_facts"]
            if label == "After V6 filters" and "quality_filters" in baseline_4b
            else baseline_4b.get("total_facts", 0)
        )
        if label == "After V6 filters":
            b_f = baseline_4b.get("quality_filters", {}).get(
                "baseline_facts", baseline_4b["total_facts"]
            )
            v4 = baseline_4b["total_facts"]
        # Re-evaluate properly
        # For baseline, v5_arbiter_result has total_facts=256, quality_filters is in v6_filtered_result
        v4 = 0
        r4 = baseline_4b
        v9 = 0
        r9 = result_9b
        v7 = 0
        r7 = result_7b

        if key == "total_facts":
            if label == "Arbiter output":
                v4 = r4.get("total_facts", 0)
                v9 = r9.get("total_facts", 0)
                v7 = r7.get("total_facts", 0)
            else:  # After V6 filters
                v4 = r4.get("quality_filters", {}).get("baseline_facts", r4.get("total_facts", 0))
                v9 = r9.get("quality_filters", {}).get("baseline_facts", r9.get("total_facts", 0))
                v7 = r7.get("quality_filters", {}).get("baseline_facts", r7.get("total_facts", 0))
        else:
            v4 = baseline_4b.get("quality_filters", {}).get(key, 0)
            v9 = result_9b.get("quality_filters", {}).get(key, 0)
            v7 = result_7b.get("quality_filters", {}).get(key, 0)

        delta_9b_7b = v9 - v7
        sign = "+" if delta_9b_7b > 0 else ""
        log(f"  │ {label:<20} │ {v4:<8} │ {v9:<8} │ {v7:<8} │ {sign}{delta_9b_7b:<6} │")

    log("  └──────────────────────┴──────────┴──────────┴──────────┴──────────┘")

    # Summary
    log("\n  Summary:")
    d9 = result_9b["total_facts"] - baseline_4b["total_facts"]
    d7 = result_7b["total_facts"] - baseline_4b["total_facts"]
    log(
        f"  9B arbiter vs 4B baseline: {d9:+d} facts ({result_9b['total_facts']} vs {baseline_4b['total_facts']})"
    )
    log(
        f"  7B arbiter vs 4B baseline: {d7:+d} facts ({result_7b['total_facts']} vs {baseline_4b['total_facts']})"
    )
    log(f"  9B vs 7B: {result_9b['total_facts'] - result_7b['total_facts']:+d} facts")

    comp = {
        "4b_baseline": {
            "total_facts": baseline_4b["total_facts"],
            "quality_filters": baseline_4b.get("quality_filters", {}),
        },
        "9b_arbiter": {
            "total_facts": result_9b["total_facts"],
            "quality_filters": result_9b.get("quality_filters", {}),
        },
        "7b_arbiter": {
            "total_facts": result_7b["total_facts"],
            "quality_filters": result_7b.get("quality_filters", {}),
        },
        "delta": {
            "9b_vs_4b": d9,
            "7b_vs_4b": d7,
            "9b_vs_7b": result_9b["total_facts"] - result_7b["total_facts"],
        },
    }
    with open(os.path.join(RESULTS_DIR, "comparison.json"), "w") as f:
        json.dump(comp, f, indent=2, ensure_ascii=False)
    log(f"\n  Full comparison: {RESULTS_DIR}/comparison.json")


# ── Main ──


def main():
    log("=" * 60)
    log("DUAL ARBITER COMPARISON: 9B (×2) vs 7B (×2)")
    log("Full LLM consolidation via WorkStealer on V6 pairs")
    log("=" * 60)

    TEST = test_setup("arbiter_7b9b", "Dual arbiter comparison: 9B ×2 vs 7B ×2 on V6 pairs")

    # Load V6 pairs
    with open(PAIRS_PATH) as f:
        pairs = json.load(f)
    log(f"\nV6 pairs loaded: {len(pairs)}")

    # Load 4B baseline
    with open(BASELINE_PATH) as f:
        baseline_4b = json.load(f)
    log(f"4B arbiter baseline: {baseline_4b['total_facts']} facts")

    # Chunk text
    with open(LONG_TEXT_PATH) as f:
        text = f.read()
    chunks = split_atomic(text)
    log(f"Text chunks: {len(chunks)}")

    # Save current envs for restore
    saved_envs = {}
    for env_path in [INFERENCE_ENV]:
        if os.path.exists(env_path):
            saved_envs[env_path] = _read_env(env_path)

    result_9b = result_7b = None

    try:
        # ═══════════════════════════════════════════════
        # Phase 1: Dual 9B Arbiter
        # ═══════════════════════════════════════════════
        test_heartbeat("Phase 1: loading dual 9B")
        ok = load_model_both(
            "Qwen3.5-9B-Q4_K_M.gguf",
            PORT_8080,
            INFERENCE_PORT,
            INFERENCE_SVC,
            INFERENCE_SVC,
            tag="9B",
        )
        if not ok:
            log("ERROR: 9B model load failed")
            test_complete("FAILED: 9B model load")
            return

        test_heartbeat("Phase 2: dual 9B arbiter")
        arbiter_9b = phase_dual_arbiter(pairs, chunks, [PORT_8080, INFERENCE_PORT], tag="9B")
        result_9b = v6_filters(arbiter_9b, tag="9B")
        with open(os.path.join(RESULTS_DIR, "arbiter_9b_result.json"), "w") as f:
            json.dump(result_9b, f, indent=2, ensure_ascii=False)
        log(f"\n  9B arbiter done — {result_9b['total_facts']} facts after V6 filters")

        # ═══════════════════════════════════════════════
        # Phase 2: Dual 7B Arbiter
        # ═══════════════════════════════════════════════
        test_heartbeat("Phase 3: loading dual 7B")
        ok = load_model_both(
            "qwen2.5-coder-7b-instruct-q4_k_m.gguf",
            PORT_8080,
            INFERENCE_PORT,
            INFERENCE_SVC,
            INFERENCE_SVC,
            tag="7B",
        )
        if not ok:
            log("ERROR: 7B model load failed")
            test_complete("FAILED: 7B model load")
            return

        test_heartbeat("Phase 4: dual 7B arbiter")
        arbiter_7b = phase_dual_arbiter(pairs, chunks, [PORT_8080, INFERENCE_PORT], tag="7B")
        result_7b = v6_filters(arbiter_7b, tag="7B")
        with open(os.path.join(RESULTS_DIR, "arbiter_7b_result.json"), "w") as f:
            json.dump(result_7b, f, indent=2, ensure_ascii=False)
        log(f"\n  7B arbiter done — {result_7b['total_facts']} facts after V6 filters")

        # ═══════════════════════════════════════════════
        # Phase 3: Comparison
        # ═══════════════════════════════════════════════
        compare_all(baseline_4b, result_9b, result_7b)

    finally:
        # Restore envs
        if INFERENCE_ENV in saved_envs:
            log("\nRestoring inference...")
            _write_env(INFERENCE_ENV, saved_envs[INFERENCE_ENV])
            restart_container(PORT_8080, INFERENCE_SVC)
        if INFERENCE_ENV in saved_envs:
            log("Restoring inference...")
            _write_env(INFERENCE_ENV, saved_envs[INFERENCE_ENV])
            restart_container(INFERENCE_PORT, INFERENCE_SVC)

        os.system(
            "systemctl --user start devforge-day-cycle.service devforge-day-cycle.timer 2>/dev/null"
        )
        log("Day cycle restarted")

    test_complete(
        f"Dual arbiter done — 9B: {result_9b['total_facts']} facts, "
        f"7B: {result_7b['total_facts']} facts "
        f"(4B baseline: {baseline_4b['total_facts']})"
    )


if __name__ == "__main__":
    main()
