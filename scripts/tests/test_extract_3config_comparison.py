#!/usr/bin/env python3
# Status: experimental
# Path: none — standalone comparison test for 3 extraction configurations
"""Compare 3 extraction configs: 8B Q8 parallel=2 vs 8B Q4 dual vs 4B Q8 dual.

Metrics: total time, facts per turn, parse success, memory, swap, OOM.

Usage:
    python3 scripts/tests/test_extract_3config_comparison.py
"""

import copy
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))


from lib.db import psql_json
from lib.model_registry import MODEL_METADATA
from lib.pod_manager import wait_health as _wait_health
from lib.pod_manager import wait_probe as _wait_probe
from lib.pod_manager.container import (
    INFERENCE_CONTAINER,
    _podman_start_inference,
    _podman_stop_inference,
    _reclaim_memory,
    _write_mode_env,
    log,
)
from pipelines.extract_llm import (
    _SYSTEM_TEXT_EXTRACT_FREE_8B,
    _calc_max_tokens,
    _calc_timeout,
    _normalize_freeform_pipeline,
    _split_atomic,
)

# ── HACK: Bump ctx to 4096 for all test extractor models ──────────
# ctx=2048 is too small for the extraction system prompt (~500 tok)
# + Korean text + max_tokens (~1600) → "Context size has been exceeded" → HTTP 500
# NOTE: We modify MODEL_METADATA directly here (module-level code), but main()
#       saves/restores the original to prevent leaking changes to other tests.
_EXTRACTOR_KEYS = [
    "day-extractor",
    "day-extractor-b",
    "day-extractor-8b-q4",
    "day-extractor-8b-q4-b",
    "day-extractor-4b-q8",
    "day-extractor-4b-q8-b",
]
_ORIGINAL_MODEL_METADATA = copy.deepcopy(MODEL_METADATA)  # Backup before modification
for _k in _EXTRACTOR_KEYS:
    if _k in MODEL_METADATA:
        _meta = copy.deepcopy(MODEL_METADATA[_k])
        _meta["ctx"] = 4096
        MODEL_METADATA[_k] = _meta

# ── 4B-specific extraction prompt ────────────────────────────
# 4B Q8 is weaker than 8B — keep prompt short, concrete, direct.
# Same snake_case requirement but fewer abstract rules.

_PROMPT_4B_EXTRACT_FREE = """\
Extract factual triples from this text. Each fact: (subject, predicate, object).

RULES:
1. Max 4 facts. Fewer clean > many noisy.
2. Predicate: short snake_case (2-4 words). Example: "deploys_on_port", "requires_version".
   NOT empty, NOT Korean, NOT "has"/"is"
3. Object = extracted value. NOT a copy of the evidence.
4. Evidence = direct quote with period.
5. Self-contained: resolve pronouns.
6. Skip: greetings, questions, small talk, speculation.

Output JSON with extractions list. Each: evidence, category, subject, predicate, object, source_context.

Empty: {"extractions":[]}."""

# ── Minimum text length for meaningful extraction ──────────
MIN_TEXT_LEN = 300


def _get_db_turns(limit=10):
    """Get pending turns from DB with meaningful text content."""
    rows = psql_json(
        f"""
        SELECT id, COALESCE(user_turn, '') as user_turn,
               COALESCE(text, '') as text,
               COALESCE(thinking, '') as thinking
        FROM turns
        WHERE pipeline_state = 'pending'
          AND text IS NOT NULL AND length(text) > {MIN_TEXT_LEN}
        ORDER BY created_at DESC
        LIMIT {limit}
    """
    )
    return rows


def _call_model(port, messages, max_tokens=768, temperature=0.1, timeout=120):
    """Direct HTTP call to a llama-server endpoint. Returns dict with content + timing."""
    body = json.dumps(
        {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
    ).encode()
    t0 = time.monotonic()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        data = json.loads(raw)
    except Exception as e:
        return {
            "content": None,
            "error": str(e),
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }
    choices = data.get("choices", [])
    content = choices[0].get("message", {}).get("content") if choices else None
    usage = data.get("usage", {})
    return {
        "content": content,
        "error": None,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "usage": usage,
    }


def _parse_extractions(raw, turn_id="", chunk_idx=0):
    """Parse JSON extractions from model response. Logs raw on parse failure."""
    if raw is None:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        cleaned = re.sub(r"```json\s*|\s*```", "", raw)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            log(f"    [{turn_id[:8]}] ch{chunk_idx} PARSE FAIL: raw[:200]={repr(raw[:200])}")
            return []
    ex = parsed.get("extractions", [])
    if not isinstance(ex, list):
        log(f"    [{turn_id[:8]}] ch{chunk_idx} BAD FORMAT: keys={list(parsed.keys())}")
        return []
    if len(ex) == 0:
        log(f"    [{turn_id[:8]}] ch{chunk_idx} 0 facts (model returned empty extractions)")
    return ex


def _ensure_stopped():
    """Ensure watchdog + day-cycle are stopped (belt-and-suspenders)."""
    for svc in [
        "devforge-watchdog.service",
        "devforge-day-cycle.service",
        "devforge-day-cycle.timer",
    ]:
        subprocess.run(["systemctl", "--user", "stop", svc], capture_output=True, timeout=30)
        subprocess.run(
            ["systemctl", "--user", "reset-failed", svc], capture_output=True, timeout=10
        )
    _podman_stop_inference()


def _start_primary(key):
    """Start inference container with primary model. Returns True on success."""
    meta = MODEL_METADATA.get(key)
    if not meta:
        print(f"  FATAL: unknown model key: {key}")
        return False
    log(f"  Starting primary {key} ({meta['file']}) on :{meta['port']}")
    _write_mode_env(meta["mode"], meta["port"], model_key=key)
    if not _podman_start_inference():
        return False
    ok = _wait_health(meta["port"], timeout=600)
    if ok:
        ok = _wait_probe(meta["port"], key, timeout=300)
    log(f"  Primary :{meta['port']} {'OK' if ok else 'FAILED'}")
    return ok


def _start_secondary(key):
    """Launch second llama-server via podman exec. Returns True on success."""
    meta = MODEL_METADATA.get(key)
    if not meta:
        print(f"  FATAL: unknown secondary key: {key}")
        return False
    port = meta["port"]
    model_file = meta["file"]
    ctx = meta.get("ctx", 8192)
    threads = meta.get("threads", 4)
    threads_batch = meta.get("threads_batch", 4)
    ubatch = meta.get("ubatch_size", 256)
    parallel = meta.get("parallel", 1)
    cpus = meta.get("cpus", "")
    cpu_range = meta.get("cpu_range", "")
    cpu_strict = meta.get("cpu_strict", "")
    flash_attn = meta.get("flash_attn", "")
    cache_ram = meta.get("cache_ram", "")
    cache_type_k = meta.get("cache_type_k", "")
    cache_type_v = meta.get("cache_type_v", "")

    launch_cmd = ["/app/llama-server"]
    if cpus:
        launch_cmd = ["taskset", "-c", cpus] + launch_cmd

    cmd = (
        ["podman", "exec", "-d", INFERENCE_CONTAINER]
        + launch_cmd
        + [
            "-m",
            f"/models/{model_file}",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--ctx-size",
            str(ctx),
            "--parallel",
            str(parallel),
            "--threads",
            str(threads),
            "--threads-batch",
            str(threads_batch),
            "--timeout",
            "28800",
            "--batch-size",
            "512",
            "--ubatch-size",
            str(ubatch),
            "--temp",
            "0.1",
            "--cont-batching",
            "--no-mmap",
            "-lv",
            "6",
            "--metrics",
            "--reasoning",
            "off",
            "--slot-prompt-similarity",
            "0",
        ]
    )
    if cpu_range:
        cmd += ["--cpu-range", cpu_range]
    if cpu_strict:
        cmd += ["--cpu-strict", cpu_strict]
    if flash_attn:
        cmd += ["--flash-attn", "on"]
    if cache_ram:
        cmd += [
            "--cache-ram",
            str(cache_ram),
            "--kv-unified",
            "--cache-idle-slots",
            "--cache-reuse",
            "256",
        ]
    if cache_type_k:
        cmd += ["--cache-type-k", cache_type_k]
    if cache_type_v:
        cmd += ["--cache-type-v", cache_type_v]

    log(f"  Launching secondary {key} ({model_file}) on :{port} via podman exec")
    r = subprocess.run(cmd, capture_output=True, timeout=30, text=True)
    if r.returncode != 0:
        log(f"  Secondary launch FAILED (rc={r.returncode}): {r.stderr.strip()[:200]}")
        return False
    ok = _wait_health(port, timeout=300)
    log(f"  Secondary :{port} {'OK' if ok else 'FAILED'}")
    return ok


def _get_memory():
    """Get memory stats: available MB, swap used MB."""
    try:
        r = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=10)
        lines = r.stdout.strip().split("\n")
        mem = lines[1].split()
        swap = lines[2].split()
        return {
            "total_mb": int(mem[1]),
            "used_mb": int(mem[2]),
            "avail_mb": int(mem[6]),
            "swap_total_mb": int(swap[1]),
            "swap_used_mb": int(swap[2]),
        }
    except Exception:
        return {"avail_mb": -1, "swap_used_mb": -1}


def _ensure_model_healthy(port, key=""):
    """Check if the model at :port is still healthy. Returns True if OK."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=5) as r:
            ok = r.status == 200
        if not ok:
            log(f"  Model :{port}{key} unhealthy (health={r.status})")
            return False
        return True
    except Exception as e:
        log(f"  Model :{port}{key} unreachable: {e}")
        return False


def _start_embed():
    """Start embeder (Qwen3-Embedding-8B) on :8081 in inference container. No-op if healthy."""
    import urllib.request as _ur

    try:
        req = _ur.Request("http://127.0.0.1:8081/health")
        with _ur.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                log("  [embed] :8081 already healthy")
                return True
    except Exception:
        pass

    meta = MODEL_METADATA.get("embeder")
    if not meta:
        log("  [embed] FATAL: embeder not in MODEL_METADATA")
        return False

    cmd = ["podman", "exec", "-d", INFERENCE_CONTAINER, "/app/llama-server"] + [
        "-m",
        f"/models/{meta['file']}",
        "--host",
        "0.0.0.0",
        "--port",
        "8081",
        "--ctx-size",
        str(meta.get("ctx", 2048)),
        "--parallel",
        "1",
        "--threads",
        str(meta.get("threads", 2)),
        "--threads-batch",
        str(meta.get("threads_batch", 2)),
        "--timeout",
        "28800",
        "--batch-size",
        "512",
        "--ubatch-size",
        "512",
        "--embedding",
        "--pooling",
        "last",
        "--embd-normalize",
        "-1",
        "--cont-batching",
        "--no-mmap",
        "-lv",
        "6",
        "--metrics",
    ]

    log(f"  [embed] launching {meta['file']} on :8081...")
    r = subprocess.run(cmd, capture_output=True, timeout=30, text=True)
    if r.returncode != 0:
        log(f"  [embed] launch failed: {r.stderr.strip()[:200]}")
        return False

    ok = _wait_health(8081, timeout=120)
    log(f"  [embed] :8081 {'healthy' if ok else 'health timeout'}")
    return ok


def _check_oom():
    """Check dmesg for OOM killer events during test."""
    try:
        r = subprocess.run(
            ["dmesg", "--level=alert,crit,err", "--since", "5 minutes ago"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = r.stdout.strip().split("\n")
        oom_lines = [l for l in lines if "oom" in l.lower() or "killed" in l.lower()]
        return oom_lines
    except Exception:
        return []


def build_source_text(turn):
    """Build source text from the assistant's response (text) field only.

    Unlike the real pipeline (which processes user_turn and text as separate
    sections), we use text-only for a controlled comparison across configs.
    The text field contains the assistant response with the highest density
    of extractable facts.
    """
    return turn.get("text") or ""


# ── Config runners ──────────────────────────────────────────────


def run_config_a(turns, timeout=180):
    """8B Q8 parallel=2: single model on 8082, parallel=2, sequential chunk processing.

    Matches current production extract_llm.py logic (without checkpoint save).
    """
    log("── Config A: 8B Q8 parallel=2 (8082, threads=4) ──")
    t0 = time.monotonic()
    results = []
    mem_before = _get_memory()
    log(f"  Memory before: {mem_before['avail_mb']}MB avail, swap={mem_before['swap_used_mb']}MB")

    for turn in turns:
        if not _ensure_model_healthy(8082, " Config A"):
            log("  Model crashed — aborting Config A")
            break
        turn_t0 = time.monotonic()
        src = build_source_text(turn)
        if not src.strip():
            results.append({"id": turn["id"], "facts": 0, "time_s": 0, "error": "empty turn"})
            continue

        chunks = _split_atomic(src, max_chars=400)
        all_facts = []

        for ci, chunk in enumerate(chunks):
            prompt = _SYSTEM_TEXT_EXTRACT_FREE_8B
            effective_max_tokens = _calc_max_tokens(len(chunk))
            effective_max_tokens = min(4096, (effective_max_tokens or 768) * 2)
            timeout_s = _calc_timeout(len(chunk), max_tokens=effective_max_tokens)

            resp = _call_model(
                8082,
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": chunk},
                ],
                max_tokens=effective_max_tokens or 768,
                timeout=timeout_s + 90,
            )
            if resp["error"]:
                log(f"    [{turn['id'][:8]}] chunk {ci} FAILED: {resp['error']}")
                continue
            facts = _parse_extractions(resp["content"], turn["id"], ci)
            all_facts.extend(facts)
            log(f"    [{turn['id'][:8]}] ch{ci}: {len(facts)} facts ({resp['elapsed_ms']}ms)")

        before_dedup = len(all_facts)
        all_facts = _normalize_freeform_pipeline(all_facts)
        if len(all_facts) != before_dedup:
            log(f"    [{turn['id'][:8]}] EDC dedup: {before_dedup} -> {len(all_facts)}")

        elapsed = time.monotonic() - turn_t0
        results.append(
            {
                "id": turn["id"],
                "facts": all_facts,
                "n_facts": len(all_facts),
                "time_s": round(elapsed, 1),
                "chunks": len(chunks),
            }
        )
        log(f"  [{turn['id'][:8]}] => {len(all_facts)} facts, {elapsed:.0f}s")

    total_time = time.monotonic() - t0
    mem_after = _get_memory()
    oom = _check_oom()
    log(f"  Memory after: {mem_after['avail_mb']}MB avail, swap={mem_after['swap_used_mb']}MB")
    return {
        "config": "A: 8B Q8 parallel=2",
        "total_time_s": round(total_time, 1),
        "n_turns": len(turns),
        "total_facts": sum(r["n_facts"] for r in results),
        "avg_facts": round(sum(r["n_facts"] for r in results) / max(len(results), 1), 1),
        "parse_failures": sum(1 for r in results if r.get("error")),
        "mem_avail_before": mem_before["avail_mb"],
        "mem_avail_after": mem_after["avail_mb"],
        "swap_before": mem_before["swap_used_mb"],
        "swap_after": mem_after["swap_used_mb"],
        "oom_events": len(oom),
        "per_turn": results,
    }


def _process_chunk(args):
    """Process a single chunk: call model, parse facts. Worker function for ThreadPoolExecutor."""
    port, chunk, turn_id, chunk_idx, prompt = args
    effective_max_tokens = _calc_max_tokens(len(chunk))
    effective_max_tokens = min(4096, (effective_max_tokens or 768) * 2)
    timeout_s = _calc_timeout(len(chunk), max_tokens=effective_max_tokens)
    resp = _call_model(
        port,
        [{"role": "system", "content": prompt}, {"role": "user", "content": chunk}],
        max_tokens=effective_max_tokens or 768,
        timeout=timeout_s + 90,
    )
    if resp["error"]:
        return turn_id, chunk_idx, [], resp["error"], port, resp["elapsed_ms"]
    facts = _parse_extractions(resp["content"], turn_id, chunk_idx)
    return turn_id, chunk_idx, facts, None, port, resp["elapsed_ms"]


def run_config_b(turns, timeout=180):
    """8B Q4 dual: workstealer pattern — chunks dispatched across 8082 and 8083 concurrently.

    ThreadPoolExecutor distributes chunks to whichever server is available.
    Both servers work on the SAME turn simultaneously.
    """
    log("── Config B: 8B Q4 dual workstealer (8082+8083, threads=2 each) ──")
    t0 = time.monotonic()
    results = []
    mem_before = _get_memory()
    log(f"  Memory before: {mem_before['avail_mb']}MB avail, swap={mem_before['swap_used_mb']}MB")
    ports = [8082, 8083]

    for turn in turns:
        # Check at least one server is healthy
        if not any(_ensure_model_healthy(p, " Config B") for p in ports):
            log("  Both models crashed — aborting Config B")
            break
        turn_t0 = time.monotonic()
        src = build_source_text(turn)
        if not src.strip():
            results.append({"id": turn["id"], "facts": 0, "time_s": 0, "error": "empty turn"})
            continue

        chunks = _split_atomic(src, max_chars=400)
        prompt = _SYSTEM_TEXT_EXTRACT_FREE_8B

        # Workstealer: submit all chunks, dispatch to any available server
        # Alternating initial assignment for load balance, then whichever finishes first
        # gets the next chunk via as_completed
        args_list = [
            (ports[ci % 2], chunk, turn["id"], ci, prompt) for ci, chunk in enumerate(chunks)
        ]

        all_facts = []
        served_by = {8082: 0, 8083: 0}

        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = {ex.submit(_process_chunk, a): a for a in args_list}
            for fut in as_completed(futures):
                a = futures[fut]
                tid, ci, facts, err, port, ms = fut.result()
                served_by[port] = served_by.get(port, 0) + 1
                if err:
                    log(f"    [{tid[:8]}] ch{ci} on :{port} FAILED: {err}")
                else:
                    all_facts.extend(facts)
                    log(f"    [{tid[:8]}] ch{ci} on :{port}: {len(facts)} facts ({ms}ms)")

        before_dedup = len(all_facts)
        all_facts = _normalize_freeform_pipeline(all_facts)
        if len(all_facts) != before_dedup:
            log(f"    [{turn['id'][:8]}] EDC dedup: {before_dedup} -> {len(all_facts)}")
        load_str = " | ".join(f":{p}={n}" for p, n in sorted(served_by.items()))
        elapsed = time.monotonic() - turn_t0
        results.append(
            {
                "id": turn["id"],
                "facts": all_facts,
                "n_facts": len(all_facts),
                "time_s": round(elapsed, 1),
                "chunks": len(chunks),
                "load": load_str,
            }
        )
        log(f"  [{turn['id'][:8]}] => {len(all_facts)} facts, {elapsed:.0f}s [{load_str}]")

    total_time = time.monotonic() - t0
    mem_after = _get_memory()
    oom = _check_oom()
    return {
        "config": "B: 8B Q4 dual",
        "total_time_s": round(total_time, 1),
        "n_turns": len(turns),
        "total_facts": sum(r["n_facts"] for r in results),
        "avg_facts": round(sum(r["n_facts"] for r in results) / max(len(results), 1), 1),
        "parse_failures": sum(1 for r in results if r.get("error")),
        "mem_avail_before": mem_before["avail_mb"],
        "mem_avail_after": mem_after["avail_mb"],
        "swap_before": mem_before["swap_used_mb"],
        "swap_after": mem_after["swap_used_mb"],
        "oom_events": len(oom),
        "per_turn": results,
    }


def run_config_c(turns, timeout=180):
    """4B Q8 dual workstealer: single-pass extraction on 8082+8083.

    Identical logic to Config B (workstealer), but uses the simpler
    4B-specific prompt _PROMPT_4B_EXTRACT_FREE.
    """
    log("--- Config C: 4B Q8 dual workstealer (8082+8083, threads=2 each) ---")
    t0 = time.monotonic()
    results = []
    mem_before = _get_memory()
    log(f"  Memory before: {mem_before['avail_mb']}MB avail, swap={mem_before['swap_used_mb']}MB")
    ports = [8082, 8083]

    for turn in turns:
        if not any(_ensure_model_healthy(p, " Config C") for p in ports):
            log("  Both models crashed -- aborting Config C")
            break
        turn_t0 = time.monotonic()
        src = build_source_text(turn)
        if not src.strip():
            results.append({"id": turn["id"], "facts": 0, "time_s": 0, "error": "empty turn"})
            continue

        chunks = _split_atomic(src, max_chars=400)
        prompt = _PROMPT_4B_EXTRACT_FREE

        args_list = [
            (ports[ci % 2], chunk, turn["id"], ci, prompt) for ci, chunk in enumerate(chunks)
        ]

        all_facts = []
        served_by = {8082: 0, 8083: 0}

        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = {ex.submit(_process_chunk, a): a for a in args_list}
            for fut in as_completed(futures):
                a = futures[fut]
                tid, ci, facts, err, port, ms = fut.result()
                served_by[port] = served_by.get(port, 0) + 1
                if err:
                    log(f"    [{tid[:8]}] ch{ci} on :{port} FAILED: {err}")
                else:
                    all_facts.extend(facts)
                    log(f"    [{tid[:8]}] ch{ci} on :{port}: {len(facts)} facts ({ms}ms)")

        before_dedup = len(all_facts)
        all_facts = _normalize_freeform_pipeline(all_facts)
        if len(all_facts) != before_dedup:
            log(f"    [{turn['id'][:8]}] EDC dedup: {before_dedup} -> {len(all_facts)}")
        load_str = " | ".join(f":{p}={n}" for p, n in sorted(served_by.items()))
        elapsed = time.monotonic() - turn_t0
        results.append(
            {
                "id": turn["id"],
                "facts": all_facts,
                "n_facts": len(all_facts),
                "time_s": round(elapsed, 1),
                "chunks": len(chunks),
                "load": load_str,
            }
        )
        log(f"  [{turn['id'][:8]}] => {len(all_facts)} facts, {elapsed:.0f}s [{load_str}]")

    total_time = time.monotonic() - t0
    mem_after = _get_memory()
    oom = _check_oom()
    return {
        "config": "C: 4B Q8 dual workstealer",
        "total_time_s": round(total_time, 1),
        "n_turns": len(turns),
        "total_facts": sum(r["n_facts"] for r in results),
        "avg_facts": round(sum(r["n_facts"] for r in results) / max(len(results), 1), 1),
        "parse_failures": sum(1 for r in results if r.get("error")),
        "mem_avail_before": mem_before["avail_mb"],
        "mem_avail_after": mem_after["avail_mb"],
        "swap_before": mem_before["swap_used_mb"],
        "swap_after": mem_after["swap_used_mb"],
        "oom_events": len(oom),
        "per_turn": results,
    }


def print_comparison(results):
    """Print comparison table across configs."""
    print()
    print("=" * 72)
    print("EXTRACTION CONFIG COMPARISON RESULTS")
    print("=" * 72)
    header = f"{'Config':<22} {'Time(s)':<10} {'Facts':<8} {'Avg/Turn':<10} {'Mem(avail)':<14} {'Swap':<8} {'OOM':<6}"
    print(header)
    print("-" * 72)
    for r in results:
        mem_delta = r["mem_avail_before"] - r["mem_avail_after"]
        swap_delta = r["swap_after"] - r["swap_before"]
        mem_str = f"{r['mem_avail_before']}→{r['mem_avail_after']}MB"
        swap_str = f"{swap_delta:+}MB" if swap_delta != 0 else "0"
        oom_str = "YES!" if r["oom_events"] > 0 else "no"
        print(
            f"{r['config']:<22} "
            f"{r['total_time_s']:<10.0f} "
            f"{r['total_facts']:<8} "
            f"{r['avg_facts']:<10.1f} "
            f"{mem_str:<14} "
            f"{swap_str:<8} "
            f"{oom_str:<6}"
        )
    print("-" * 72)
    print()


def print_detailed(results):
    """Print per-turn table for each config."""
    for r in results:
        print(f"\n--- {r['config']} ---")
        pt = r["per_turn"]
        header = f"{'Turn':<12} {'Facts':<7} {'Time(s)':<9} {'Chunks':<7}"
        if "n_pass1_raw" in (pt[0] if pt else {}):
            header += f"{'P1raw':<7} {'P1':<5} {'P2':<5}"
            header += f"{'Arb1':<22} {'Arb2':<28}"
        elif "n_pass1" in (pt[0] if pt else {}):
            header += f"{'Pass1':<7} {'Pass2':<7}"
        print(header)
        print("-" * 90)
        for t in pt:
            extra = ""
            if "n_pass1_raw" in t:
                extra = (
                    f"{t['n_pass1_raw']:<7} {t['n_pass1']:<5} {t['n_pass2']:<5}"
                    f"{t['arbiter1']:<22} {t['arbiter2']:<28}"
                )
            elif "n_pass1" in t:
                extra = f"{t['n_pass1']:<7} {t['n_pass2']:<7}"
            print(
                f"{t['id'][:10]:<12} "
                f"{t['n_facts']:<7} "
                f"{t['time_s']:<9.1f} "
                f"{t.get('chunks', 1):<7} "
                f"{extra}"
            )


# ── Main ────────────────────────────────────────────────────────


def main():
    # Redirect stderr to stdout for clean log capture
    sys.stderr = sys.stdout

    # Save results to persistent file
    output_path = "/tmp/test_comparison_results.json"

    log("=" * 60)
    log("Extraction Config Comparison: A (8B Q8) vs B (8B Q4 workstealer) vs C (4B Q8 workstealer)")
    log("=" * 60)

    # 1. Stop everything
    _ensure_stopped()
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))

    # 2. Get turns — use only real DB turns
    db_turns = _get_db_turns(limit=9)
    all_turns = db_turns
    log(f"Collected {len(all_turns)} test turns (DB only)")

    all_results = []

    # ── Config B: 8B Q4 dual (workstealer) ──
    log("\n" + "=" * 60)
    log("CONFIG B: 8B Q4 dual (8082+8083 workstealer)")
    log("=" * 60)
    _ensure_stopped()
    _reclaim_memory()
    time.sleep(5)
    if not _start_primary("day-extractor-8b-q4"):
        log("FATAL: Config B primary start failed")
    elif not _start_secondary("day-extractor-8b-q4-b"):
        log("FATAL: Config B secondary start failed")
    else:
        _start_embed()
        result_b = run_config_b(all_turns)
        all_results.append(result_b)
        print_detailed([result_b])

    # ── Cleanup ──
    log("\n--- cleanup for next config ---")
    _podman_stop_inference()
    _reclaim_memory()
    time.sleep(10)

    # ── Config A: 8B Q8 parallel=2 (baseline) ──
    log("\n" + "=" * 60)
    log("CONFIG A: 8B Q8 parallel=2 (current production)")
    log("=" * 60)
    _ensure_stopped()
    _reclaim_memory()
    time.sleep(5)
    if _start_primary("day-extractor"):
        _start_embed()
        result_a = run_config_a(all_turns)
        all_results.append(result_a)
        print_detailed([result_a])

    # ── Cleanup ──
    log("\n--- cleanup for next config ---")
    _podman_stop_inference()
    _reclaim_memory()
    time.sleep(10)

    # ── Config C: 4B Q8 dual workstealer ──
    log("\n" + "=" * 60)
    log("CONFIG C: 4B Q8 dual workstealer (8082+8083)")
    log("=" * 60)
    _ensure_stopped()
    _reclaim_memory()
    time.sleep(5)
    if not _start_primary("day-extractor-4b-q8"):
        log("FATAL: Config C primary start failed")
    elif not _start_secondary("day-extractor-4b-q8-b"):
        log("FATAL: Config C secondary start failed")
    else:
        _start_embed()
        result_c = run_config_c(all_turns)
        all_results.append(result_c)
        print_detailed([result_c])

    # ── Final cleanup ──
    log("\nFinal cleanup...")
    _podman_stop_inference()
    _reclaim_memory()

    # ── Print comparison ──
    print_comparison(all_results)

    # ── Save results ──
    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False, default=str)
    log(f"Results saved to {output_path}")

    log("All configs completed. Review table above for winner.")


if __name__ == "__main__":
    main()
