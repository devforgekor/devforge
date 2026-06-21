#!/usr/bin/env python3
# Status: experimental
# Path: none — extract strategy comparison test script
"""Extract strategy comparison for long Korean developer turns on bb9c6363."""
import json, os, sys, time
from typing import Dict, List, Optional

SCRIPTS_DIR = "/opt/projects/server/scripts"
os.chdir(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

from lib.llm_client import call_llm
from lib.llm.json_parser import parse_llm_json
from lib.common import strip_think
from lib.pod_manager import ensure_model
from lib import pod_manager

# ── Temp MODEL_METADATA for cross-model tests ──
TEST_MODELS = {
    "7b": {
        "file": "Qwen2.5-Coder-7B-Instruct-Q8_0.gguf",
        "port": 8082, "mode": "day",
        "model_name": "extractor-test", "ctx": 8192,
        "parallel": 2, "ubatch_size": 512,
    },
    "8b": {
        "file": "Qwen3-8B-Q4_K_M.gguf",
        "port": 8082, "mode": "day",
        "model_name": "extractor-test", "ctx": 8192,
        "parallel": 2, "ubatch_size": 512,
    },
    "9b": {
        "file": "Qwen3-8B-Q4_K_M.gguf",
        "port": 8082, "mode": "day",
        "model_name": "extractor-test", "ctx": 8192,
        "parallel": 2, "ubatch_size": 512,
    },
    "9b-q4": {
        "file": "Qwen3-8B-Q4_K_M.gguf",
        "port": 8082, "mode": "day",
        "model_name": "extractor-test", "ctx": 8192,
        "parallel": 2, "ubatch_size": 512,
    },
    "14b-qwen": {
        "file": "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf",
        "size": "8.2GB", "port": 8082, "mode": "day",
        "model_name": "extractor-test", "ctx": 8192,
        "parallel": 1, "ubatch_size": 512,
    },
    "14b-nextcoder": {
        "file": "nextcoder-14b-q4_k_m.gguf",
        "size": "9.0GB", "port": 8082, "mode": "day",
        "model_name": "extractor-test", "ctx": 8192,
        "parallel": 1, "ubatch_size": 512,
    },
}

# ── Constants ──
TURNS = {
    "a1faed6c": "a1faed6c-ffa1-4869-8dda-a468e8e5164f",
    "b7f65701": "b7f65701-5689-4238-91fb-9caee1d3b8e8",
    "bb9c6363": "bb9c6363-cce3-4e60-aa05-8cd1f3e99d78",
}

# Prompt from extract.py (same SYSTEM_DAY_EXTRACT)
SYSTEM_EXTRACT = """\
You are a fact extractor for a developer-assistant conversation turn.
Each turn has three parts: user_turn (the user's message), thinking (the
model's internal reasoning, may be empty), and text (the model's response).

Extract key factual statements that are EXPLICITLY present in the text.
Do NOT infer, summarize, or add information not present in the source.

Output STRICT JSON:
{
  "extractions": [
    {
      "fact_type": "user|thinking|text",
      "evidence": "Exact quote or close paraphrase from the source",
      "category": "requirement|decision|explanation|code|reasoning|other"
    }
  ]
}

Rules:
- fact_type must match which source field the evidence came from
- evidence must be directly traceable to the source text
- Skip thinking if it is empty or contains only formatting
- Extract at most 5 facts per fact_type
- If nothing extractable, return {"extractions": []}"""

TIMEOUT_EXTRACT = 900
MAX_TOKENS = 512
TEMP = 0.1


# ── Helpers ──

def _call_extract(system_prompt: str, user_parts: List[str],
                  timeout: int = TIMEOUT_EXTRACT,
                  max_tokens: int = MAX_TOKENS) -> Optional[Dict]:
    """Single LLM extraction call."""
    meta = call_llm(
        [{"role": "system", "content": system_prompt},
         {"role": "user", "content": "\n".join(user_parts)}],
        model="day_extract",
        max_tokens=max_tokens, temperature=TEMP,
        timeout=timeout, json_mode=True, return_meta=True,
    )
    raw = meta["content"]
    cleaned = strip_think(raw)
    parsed = parse_llm_json(cleaned)
    if parsed is None:
        return None
    ex = parsed.get("extractions", [])
    if not isinstance(ex, list):
        return None
    return {
        "extractions": ex,
        "usage": meta.get("usage", {}),
        "timings": meta.get("timings", {}),
        "elapsed_ms": meta.get("elapsed_ms", 0),
    }


# ── Strategy 1: Baseline (single call) ──

def strat_baseline(user_turn: str, thinking: str, text: str) -> Optional[Dict]:
    """Current production extract: single LLM call with all fields."""
    parts = [
        "=== user_turn ===", user_turn or "(empty)",
        "",
        "=== thinking ===", thinking or "(empty)",
        "",
        "=== text ===", text or "(empty)",
    ]
    total_chars = len(user_turn) + len(thinking) + len(text)
    timeout = min(60 + int(total_chars * 0.2) + 300, 1800)
    if total_chars > 5000:
        timeout = int(timeout * 2.5)
    return _call_extract(SYSTEM_EXTRACT, parts, timeout=timeout)


# ── Strategy 1b: Baseline + max_tokens=1024 ──

def strat_baseline_1024(user_turn: str, thinking: str, text: str) -> Optional[Dict]:
    """Single LLM call with doubled generation tokens."""
    parts = [
        "=== user_turn ===", user_turn or "(empty)",
        "",
        "=== thinking ===", thinking or "(empty)",
        "",
        "=== text ===", text or "(empty)",
    ]
    total_chars = len(user_turn) + len(thinking) + len(text)
    timeout = min(60 + int(total_chars * 0.2) + 300, 1800)
    if total_chars > 5000:
        timeout = int(timeout * 2.5)
    return _call_extract(SYSTEM_EXTRACT, parts, timeout=timeout, max_tokens=1024)


# ── Strategy 2: SLIDE chunking (per-field overlapping windows) ──

def _chunk_text(text: str, chunk_size: int = 2500, overlap: int = 500) -> List[tuple]:
    """Split text into (start_pos, text_segment) with overlap."""
    if len(text) <= chunk_size:
        return [(0, text)]
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append((start, text[start:end]))
        if end == len(text):
            break
        start += chunk_size - overlap
    return chunks


def strat_slide(user_turn: str, thinking: str, text: str) -> Optional[Dict]:
    """Overlapping chunk extraction + merge dedup.

    Each field (user_turn, thinking, text) is split into overlapping chunks.
    Only fields exceeding 2500 chars are chunked; smaller fields kept whole.
    All extractions merged and deduplicated by evidence text.
    """
    all_extractions: List[Dict] = []
    total_time = 0.0
    prompt_tokens = 0
    gen_tokens = 0

    fields = [
        ("user_turn", user_turn or ""),
        ("thinking", thinking or ""),
        ("text", text or ""),
    ]

    for field_name, content in fields:
        if not content.strip():
            continue
        chunks = _chunk_text(content)
        total = len(chunks)

        for ci, (start_pos, seg) in enumerate(chunks):
            prefix = f"=== {field_name} (segment {ci+1}/{total}) ==="
            is_last = (ci == total - 1)
            need_overlap_hint = not is_last and total > 1
            overlap_hint = (
                "\n[NOTE: This is a CONTINUATION segment. Extract facts that "
                "are complete within this segment. Do not extract partial facts.]"
                if not is_last else ""
            )
            parts = [prefix, seg]
            if overlap_hint:
                parts.append(overlap_hint)

            # Estimate timeout: smaller text = faster
            timeout = min(60 + int(len(seg) * 0.2) + 300, 1200)
            t0 = time.monotonic()
            result = _call_extract(SYSTEM_EXTRACT, parts, timeout=timeout)
            elapsed = time.monotonic() - t0
            total_time += elapsed

            if result:
                exs = result.get("extractions", [])
                all_extractions.extend(exs)
                u = result.get("usage", {})
                prompt_tokens += u.get("prompt_tokens", 0) or 0
                gen_tokens += u.get("completion_tokens", 0) or 0

    # Dedup by evidence text
    seen = set()
    deduped = []
    for ex in all_extractions:
        ev = (ex.get("evidence") or "").strip()
        if not ev:
            continue
        key = ev[:100].lower()
        if key not in seen:
            seen.add(key)
            deduped.append(ex)

    return {
        "extractions": deduped,
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": gen_tokens},
        "elapsed_ms": total_time * 1000,
        "_chunks": len(all_extractions),
        "_total_calls": sum(len(_chunk_text(f[1])) for f in fields if f[1].strip()),
    }


# ── Strategy 3: Combined-field chunking (ut+thinking+text → N equal chunks) ──

def strat_combined(user_turn: str, thinking: str, text: str) -> Optional[Dict]:
    """Concatenate all 3 fields, split into ~3 equal chunks, extract each, merge."""
    combined = "\n\n".join([
        "=== user_turn ===\n" + (user_turn or "(empty)"),
        "=== thinking ===\n" + (thinking or "(empty)"),
        "=== text ===\n" + (text or "(empty)"),
    ])
    total_chars = len(combined)
    n_chunks = 3
    chunk_size = max(500, (total_chars + n_chunks - 1) // n_chunks)

    all_extractions: List[Dict] = []
    total_time = 0.0
    prompt_tokens = 0
    gen_tokens = 0

    for ci in range(n_chunks):
        start = ci * chunk_size
        end = start + chunk_size
        seg = combined[start:end]
        if not seg.strip():
            continue
        prefix = f"=== full turn (segment {ci+1}/{n_chunks}) ==="
        hint = "\n[NOTE: This is a CONTINUATION. Extract only facts complete within this segment.]" if ci < n_chunks - 1 else ""
        parts = [prefix, seg, hint] if hint else [prefix, seg]
        timeout = min(60 + int(len(seg) * 0.2) + 300, 1200)
        t0 = time.monotonic()
        result = _call_extract(SYSTEM_EXTRACT, parts, timeout=timeout)
        elapsed = time.monotonic() - t0
        total_time += elapsed
        if result:
            exs = result.get("extractions", [])
            all_extractions.extend(exs)
            u = result.get("usage", {})
            prompt_tokens += u.get("prompt_tokens", 0) or 0
            gen_tokens += u.get("completion_tokens", 0) or 0

    # Dedup
    seen = set()
    deduped = []
    for ex in all_extractions:
        ev = (ex.get("evidence") or "").strip()
        if not ev:
            continue
        key = ev[:100].lower()
        if key not in seen:
            seen.add(key)
            deduped.append(ex)

    return {
        "extractions": deduped,
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": gen_tokens},
        "elapsed_ms": total_time * 1000,
        "_chunks": len(all_extractions),
        "_total_calls": n_chunks,
    }


# ── Strategy 6: Optimized SLIDE (no thinking, no overlap, bigger chunks) ──

def _chunk_text_fast(text: str, chunk_size: int = 3500) -> List[tuple]:
    """Split text into equal chunks with NO overlap."""
    if len(text) <= chunk_size:
        return [(0, text)]
    chunks = []
    for start in range(0, len(text), chunk_size):
        end = min(start + chunk_size, len(text))
        chunks.append((start, text[start:end]))
    return chunks


def strat_slide_fast(user_turn: str, thinking: str, text: str) -> Optional[Dict]:
    """Optimized SLIDE: skip thinking, merge user+text, larger chunks, no overlap."""
    all_extractions: List[Dict] = []
    total_time = 0.0
    prompt_tokens = 0
    gen_tokens = 0

    # Only user_turn + text (skip thinking — low factual density)
    combined = "\n\n".join([
        "=== user_turn ===", user_turn or "(empty)",
        "",
        "=== text ===", text or "(empty)",
    ])

    chunks = _chunk_text_fast(combined, chunk_size=3500)
    n_total = len(chunks)

    for ci, (start_pos, seg) in enumerate(chunks):
        prefix = f"=== turn (part {ci+1}/{n_total}) ==="
        hint = "\n[NOTE: CONTINUATION. Extract only facts complete within this segment.]" if ci < n_total - 1 else ""
        parts = [prefix, seg, hint] if hint else [prefix, seg]
        timeout = min(60 + int(len(seg) * 0.2) + 300, 1200)
        t0 = time.monotonic()
        result = _call_extract(SYSTEM_EXTRACT, parts, timeout=timeout)
        elapsed = time.monotonic() - t0
        total_time += elapsed
        if result:
            exs = result.get("extractions", [])
            all_extractions.extend(exs)
            u = result.get("usage", {})
            prompt_tokens += u.get("prompt_tokens", 0) or 0
            gen_tokens += u.get("completion_tokens", 0) or 0

    # Dedup by evidence text
    seen = set()
    deduped = []
    for ex in all_extractions:
        ev = (ex.get("evidence") or "").strip()
        if not ev:
            continue
        key = ev[:100].lower()
        if key not in seen:
            seen.add(key)
            deduped.append(ex)

    return {
        "extractions": deduped,
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": gen_tokens},
        "elapsed_ms": total_time * 1000,
        "_chunks": len(all_extractions),
        "_total_calls": n_total,
    }


# ── Strategy 7: SLIDE + skip thinking (same chunk size, no overlap) ──

def strat_slide_fast_v2(user_turn: str, thinking: str, text: str) -> Optional[Dict]:
    """SLIDE but skip thinking field for speed."""
    all_extractions: List[Dict] = []
    total_time = 0.0
    prompt_tokens = 0
    gen_tokens = 0

    fields = [
        ("user_turn", user_turn or ""),
        ("text", text or ""),
    ]

    for field_name, content in fields:
        if not content.strip():
            continue
        chunks = _chunk_text_fast(content, chunk_size=2500)
        total = len(chunks)

        for ci, (start_pos, seg) in enumerate(chunks):
            prefix = f"=== {field_name} (segment {ci+1}/{total}) ==="
            hint = "\n[NOTE: CONTINUATION. Extract only facts complete within this segment.]" if ci < total - 1 else ""
            parts = [prefix, seg, hint] if hint else [prefix, seg]
            timeout = min(60 + int(len(seg) * 0.2) + 300, 1200)
            t0 = time.monotonic()
            result = _call_extract(SYSTEM_EXTRACT, parts, timeout=timeout)
            elapsed = time.monotonic() - t0
            total_time += elapsed
            if result:
                exs = result.get("extractions", [])
                all_extractions.extend(exs)
                u = result.get("usage", {})
                prompt_tokens += u.get("prompt_tokens", 0) or 0
                gen_tokens += u.get("completion_tokens", 0) or 0

    seen = set()
    deduped = []
    for ex in all_extractions:
        ev = (ex.get("evidence") or "").strip()
        if not ev:
            continue
        key = ev[:100].lower()
        if key not in seen:
            seen.add(key)
            deduped.append(ex)

    return {
        "extractions": deduped,
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": gen_tokens},
        "elapsed_ms": total_time * 1000,
        "_chunks": len(all_extractions),
        "_total_calls": sum(len(_chunk_text_fast(f[1], 2500)) for f in fields if f[1].strip()),
    }


# ── Data loading ──

def load_turn(turn_id: str) -> Optional[Dict]:
    from lib.db import psql_json
    rows = psql_json(
        "SELECT id, user_turn, thinking, text"
        f" FROM turns WHERE id = '{turn_id}'::uuid"
    )
    if not rows:
        return None
    r = rows[0]
    ut = r.get("user_turn", "") or ""
    th = r.get("thinking", "") or ""
    tx = r.get("text", "") or ""
    return {
        "user_turn": ut,
        "thinking": th,
        "text": tx,
        "ut_len": len(ut),
        "th_len": len(th),
        "tx_len": len(tx),
    }


def get_baseline_from_db(turn_id: str) -> List[Dict]:
    from lib.db import psql_json
    rows = psql_json(
        "SELECT evidence, fact_type, fact_index, category"
        f" FROM review_facts WHERE turn_id = '{turn_id}'::uuid"
        " AND fact_type IN ('user','thinking','text')"
        " ORDER BY fact_index"
    )
    return rows or []


# ── Runner ──

def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

def run_strategy(name: str, fn, turn_data: Dict, tid_short: str, key: str) -> Dict:
    log(f"\n  [{key}] Running strategy: {name}")
    t0 = time.monotonic()
    result = fn(turn_data["user_turn"], turn_data["thinking"], turn_data["text"])
    elapsed_wall = time.monotonic() - t0

    if result is None:
        log(f"  [{key}] FAILED — LLM returned None")
        return {"strategy": name, "status": "FAIL"}

    extractions = result.get("extractions", [])
    llm_time_s = result.get("elapsed_ms", 0) / 1000
    usage = result.get("usage", {})
    ptok = (usage.get("prompt_tokens", 0) or 0)
    gtok = (usage.get("completion_tokens", 0) or 0)

    log(f"  [{key}] {name}: {len(extractions)} facts, {llm_time_s:.0f}s LLM,"
        f" {ptok} prompt tok, {gtok} gen tok, {elapsed_wall:.0f}s wall")

    # Ground via reranker
    grounded_count = 0
    ambig_count = 0
    ungrounded_count = 0
    total_score = 0
    total_len = 0
    type_counts = {}
    for ex in extractions:
        evidence = ex.get("evidence", "")
        total_len += len(evidence)
        ft = ex.get("fact_type", "")
        type_counts[ft] = type_counts.get(ft, 0) + 1

    from lib.llm_client import reranker_score
    if extractions:
        source = (turn_data.get("user_turn", "") or "") + "\n" + \
                 (turn_data.get("thinking", "") or "") + "\n" + \
                 (turn_data.get("text", "") or "")
        for ex in extractions:
            ev = ex.get("evidence", "")
            if not ev.strip():
                ungrounded_count += 1
                continue
            score = reranker_score(ev, source)
            if score is None:
                ambig_count += 1
            elif score >= 0.5:
                grounded_count += 1
                total_score += score
            else:
                ungrounded_count += 1

    avg_score = (total_score / grounded_count * 100) if grounded_count > 0 else 0
    avg_len = total_len / len(extractions) if extractions else 0

    type_str = ", ".join(f"{k}={v}" for k, v in sorted(type_counts.items()))
    log(f"  [{key}]   grounding: {grounded_count}✅ {ambig_count}🔶 {ungrounded_count}❌"
        f" | avg_score={avg_score:.0f} avg_len={avg_len:.0f}ch | types={{{type_str}}}")

    return {
        "strategy": name,
        "status": "OK",
        "fact_count": len(extractions),
        "grounded": grounded_count,
        "ambig": ambig_count,
        "ungrounded": ungrounded_count,
        "avg_score": avg_score,
        "avg_len": avg_len,
        "llm_time_s": llm_time_s,
    }


def show_comparison(results: List[Dict], baseline_facts: List[Dict],
                    turn_id_short: str, model_name: str = "7b"):
    log("")
    log("=" * 65)
    log(f"COMPARISON: {turn_id_short} @ {model_name}")
    log("=" * 65)
    log(f"  {'Strategy':<18} {'Facts':>6} {'Grounded':>10} {'Score':>7} {'Len(ch)':>8} {'Time(s)':>8}")
    log(f"  {'-' * 56}")

    # Baseline from DB
    bf = len(baseline_facts)
    bg = sum(1 for f in baseline_facts if f.get("evidence"))
    log(f"  {'BASELINE (DB)':<18} {bf:>6} {bg}/0/{bf-bg:>1} {'N/A':>7} {'N/A':>8} {'N/A':>8}")

    for r in results:
        if r["status"] == "FAIL":
            log(f"  {r['strategy']:<18} {'FAIL':>6}")
            continue
        g = r["grounded"]
        a = r["ambig"]
        u = r["ungrounded"]
        log(f"  {r['strategy']:<18} {r['fact_count']:>6} "
            f"{g}/{a}/{u} {r['avg_score']:>7.0f} "
            f"{r['avg_len']:>8.0f} {r['llm_time_s']:>8.0f}")
    log(f"{'=' * 65}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Extract strategy comparison")
    parser.add_argument("--turn", default="bb9c6363",
                        choices=["a1faed6c", "b7f65701", "bb9c6363", "all"])
    parser.add_argument("--strategies", nargs="+",
                        default=["slide"],
                        choices=["baseline", "baseline_1024", "slide", "slide_fast",
                                 "combined"])
    parser.add_argument("--model", default="7b",
                        choices=list(TEST_MODELS.keys()),
                        help="Which model to load on Pod B :8082")
    args = parser.parse_args()

    from lib.test_common import test_setup, test_heartbeat, test_complete
    ctx = test_setup("extract_strategies", f"Extract strategy comparison ({args.model})")

    # Register temp MODEL_METADATA + ensure Pod B
    meta = TEST_MODELS[args.model]
    meta["threads"] = 2
    meta["threads_batch"] = 2
    if args.model.startswith("14b"):
        meta["report_memory"] = "1"
        meta["cache_ram"] = 512
        meta["mlock"] = 0
    pod_manager.MODEL_METADATA[f"test-{args.model}"] = meta
    log(f"  Switching Pod B → {args.model}")
    ensure_model(f"test-{args.model}")
    test_heartbeat(f"Pod B → {args.model}")

    # Resolve turn IDs
    if args.turn == "all":
        turn_ids = list(TURNS.values())
    else:
        turn_ids = [TURNS[args.turn]]

    for full_tid in turn_ids:
        tid_short = full_tid[:12]
        log(f"\n{'#' * 60}")
        log(f"# Turn: {tid_short}")
        log(f"{'#' * 60}")

        turn_data = load_turn(full_tid)
        if not turn_data:
            log(f"  Turn not found: {full_tid}")
            continue

        log(f"  sizes: ut={turn_data['ut_len']}ch, "
            f"thinking={turn_data['th_len']}ch, "
            f"text={turn_data['tx_len']}ch")

        # Baseline from DB
        baseline_facts = get_baseline_from_db(full_tid)
        log(f"  baseline facts in DB: {len(baseline_facts)}")
        log(f"  model: {args.model}")

        # Run requested strategies
        results = []

        strategy_map = {
            "baseline": ("BASELINE (512)", strat_baseline),
            "baseline_1024": ("BASELINE (1024)", strat_baseline_1024),
            "slide": ("SLIDE chunking", strat_slide),
            "slide_fast": ("SLIDE fast", strat_slide_fast),
            "combined": ("Combined-field", strat_combined),
        }

        for skey in args.strategies:
            sname, sfunc = strategy_map[skey]
            result = run_strategy(sname, sfunc, turn_data, tid_short, skey)
            results.append(result)
            test_heartbeat(f"{skey} done for {tid_short}")

        # Show comparison
        show_comparison(results, baseline_facts, tid_short, args.model)

        test_heartbeat(f"Turn {tid_short} complete")

    log(f"\n{'=' * 60}")
    log("TEST COMPLETE")
    log(f"{'=' * 60}")
    test_complete()


if __name__ == "__main__":
    main()
