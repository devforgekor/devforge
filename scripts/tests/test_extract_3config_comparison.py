#!/usr/bin/env python3
# Status: experimental
# Path: none — standalone comparison test for 3 extraction configurations
"""Compare 3 extraction configs: 8B Q8 parallel=2 vs 8B Q4 dual vs 4B Q8 dual.

Metrics: total time, facts per turn, parse success, memory, swap, OOM.

Usage:
    python3 scripts/tests/test_extract_3config_comparison.py
"""

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
    _SYSTEM_TEXT_EXTRACT_FREE,
    _calc_max_tokens,
    _calc_timeout,
    _normalize_freeform_pipeline,
    _split_atomic,
)

# ── HACK: Bump ctx to 4096 for all test extractor models ──────────
# ctx=2048 is too small for the extraction system prompt (~500 tok)
# + Korean text + max_tokens (~1600) → "Context size has been exceeded" → HTTP 500
_EXTRACTOR_KEYS = [
    "day-extractor",
    "day-extractor-b",
    "day-extractor-8b-q4",
    "day-extractor-8b-q4-b",
    "day-extractor-4b-q8",
    "day-extractor-4b-q8-b",
]
for _k in _EXTRACTOR_KEYS:
    if _k in MODEL_METADATA:
        MODEL_METADATA[_k]["ctx"] = 4096

# ── Prompt for 4B backup pass (additional facts) ────────────────

_PROMPT_ADDITIONAL_FACTS = """\
Extract ADDITIONAL factual triples NOT covered by previous extraction.
Review the source text and identify facts that were missed in the first pass.
Focus on specific details, numbers, references, and actionable information.

Each fact MUST be traceable to an exact span in the source text.
The `evidence` field MUST be a direct quote from the source.

Output ONLY valid JSON. No extra text.

RULES:
1. Max 2 additional facts. If none remain, return empty array.
2. Evidence MUST be a direct quote from source, ending with period.
3. Self-contained: Resolve pronouns.
4. No duplicates with first pass.
5. Do NOT fabricate — only extract facts directly stated.

Output format:
{"extractions": [
  {
    "evidence": "<direct quote from source ending with .>",
    "predicate": "<descriptive verb phrase>",
    "subject": "<entity name>",
    "object": "<value>",
    "category": "code|decision|explanation|requirement|other"
  }
]}

If nothing extractable: {"extractions": []}."""

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
            prompt = _SYSTEM_TEXT_EXTRACT_FREE
            max_tok = _calc_max_tokens(len(chunk))
            max_tok = min(4096, (max_tok or 768) * 2)
            timeout_s = _calc_timeout(len(chunk), max_tokens=max_tok)

            resp = _call_model(
                8082,
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": chunk},
                ],
                max_tokens=max_tok or 768,
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
    max_tok = _calc_max_tokens(len(chunk))
    max_tok = min(4096, (max_tok or 768) * 2)
    timeout_s = _calc_timeout(len(chunk), max_tokens=max_tok)
    resp = _call_model(
        port,
        [{"role": "system", "content": prompt}, {"role": "user", "content": chunk}],
        max_tokens=max_tok or 768,
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
        prompt = _SYSTEM_TEXT_EXTRACT_FREE

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
    """4B Q8 dual with 4+2 backup logic: two 4B Q8 on 8082+8083.

    Two-pass extraction with dual FactArbiter consolidation:
    Pass 1: up to 4 facts per chunk (port A)
    → FactArbiter self-consolidate (dedup pass1 by chunk parity split)
    Pass 2: up to 2 additional facts per chunk (port B)
    → FactArbiter(pass1_consolidated, pass2) final merge
    """
    from lib.arbiter import FactArbiter

    log("── Config C: 4B Q8 dual 4+2 (8082 pass1, 8083 pass2) ──")
    t0 = time.monotonic()
    results = []
    mem_before = _get_memory()

    for i, turn in enumerate(turns):
        port_a = 8082 if i % 2 == 0 else 8083
        port_b = 8083 if i % 2 == 0 else 8082
        if not (
            _ensure_model_healthy(port_a, " Config C pass1")
            and _ensure_model_healthy(port_b, " Config C pass2")
        ):
            log("  Model(s) crashed — aborting Config C")
            break
        turn_t0 = time.monotonic()
        src = build_source_text(turn)
        if not src.strip():
            results.append({"id": turn["id"], "facts": 0, "time_s": 0, "error": "empty turn"})
            continue

        chunks = _split_atomic(src, max_chars=400)

        # ── Pass 1: up to 4 facts per chunk ──
        pass1_facts = []
        for ci, chunk in enumerate(chunks):
            prompt = _SYSTEM_TEXT_EXTRACT_FREE
            max_tok = _calc_max_tokens(len(chunk))
            max_tok = min(4096, (max_tok or 768) * 2)
            timeout_s = _calc_timeout(len(chunk), max_tokens=max_tok)

            resp = _call_model(
                port_a,
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": chunk},
                ],
                max_tokens=max_tok or 768,
                timeout=timeout_s + 30,
            )
            if resp["error"]:
                log(f"    [{turn['id'][:8]}] pass1 ch{ci} FAILED: {resp['error']}")
                continue
            facts = _parse_extractions(resp["content"], turn["id"], ci)
            pass1_facts.extend(facts[:4])
            log(
                f"    [{turn['id'][:8]}] pass1 ch{ci} on :{port_a}: {len(facts[:4])} facts ({resp['elapsed_ms']}ms)"
            )

        # ── FactArbiter 1: self-consolidate pass1 (split by chunk parity) ──
        arbiter = FactArbiter(threshold=0.95)
        pass1_even = [f for fi, f in enumerate(pass1_facts) if fi % 2 == 0]
        pass1_odd = [f for fi, f in enumerate(pass1_facts) if fi % 2 == 1]
        refs_1 = arbiter.consolidate(pass1_even, pass1_odd)
        pass1_merged = [ref.fact for ref in refs_1]
        n1_consensus = sum(1 for r in refs_1 if r.status.value == "CONSENSUS")
        n1_ua = sum(1 for r in refs_1 if r.status.value == "UNIQUE_A")
        n1_ub = sum(1 for r in refs_1 if r.status.value == "UNIQUE_B")

        # ── Pass 2: up to 2 additional facts per chunk ──
        pass2_facts = []
        for ci, chunk in enumerate(chunks):
            already_evidence = "\n".join(f"- {f.get('evidence', '')[:100]}" for f in pass1_merged)
            pass2_prompt = (
                f"{_PROMPT_ADDITIONAL_FACTS}\n\nAlready extracted evidence:\n{already_evidence}"
            )

            max_tok = _calc_max_tokens(len(chunk))
            max_tok = min(2048, (max_tok or 512) * 2)
            timeout_s = _calc_timeout(len(chunk), max_tokens=max_tok)

            resp = _call_model(
                port_b,
                [
                    {"role": "system", "content": pass2_prompt},
                    {"role": "user", "content": chunk},
                ],
                max_tokens=512,
                timeout=min(timeout_s + 90, 240),
            )
            if resp["error"]:
                log(f"    [{turn['id'][:8]}] pass2 ch{ci} FAILED: {resp['error']}")
                continue
            extra = _parse_extractions(resp["content"], turn["id"], ci)
            pass2_facts.extend(extra)

        # ── FactArbiter 2: merge pass2 into pass1 consolidated result ──
        refs_2 = arbiter.consolidate(pass1_merged, pass2_facts)
        all_facts = [ref.fact for ref in refs_2]
        before_dedup = len(all_facts)
        all_facts = _normalize_freeform_pipeline(all_facts)
        if len(all_facts) != before_dedup:
            log(f"    [{turn['id'][:8]}] EDC dedup: {before_dedup} -> {len(all_facts)}")
        n2_consensus = sum(1 for r in refs_2 if r.status.value == "CONSENSUS")
        n2_ua = sum(1 for r in refs_2 if r.status.value == "UNIQUE_A")
        n2_ub = sum(1 for r in refs_2 if r.status.value == "UNIQUE_B")
        n2_cf = sum(1 for r in refs_2 if r.status.value == "CONFLICT")

        elapsed = time.monotonic() - turn_t0
        results.append(
            {
                "id": turn["id"],
                "facts": all_facts,
                "n_facts": len(all_facts),
                "n_pass1_raw": len(pass1_facts),
                "n_pass1": len(pass1_merged),
                "n_pass2": len(pass2_facts),
                "arbiter1": f"con={n1_consensus},ua={n1_ua},ub={n1_ub}",
                "arbiter2": f"con={n2_consensus},ua={n2_ua},ub={n2_ub},cf={n2_cf}",
                "time_s": round(elapsed, 1),
                "chunks": len(chunks),
            }
        )
        log(
            f"  [{turn['id'][:8]}] => {len(all_facts)} facts "
            f"(pass1:{len(pass1_facts)}→{len(pass1_merged)}, pass2:{len(pass2_facts)}, "
            f"arb1={n1_consensus}c+{n1_ua}ua+{n1_ub}ub, "
            f"arb2={n2_consensus}c+{n2_ua}ua+{n2_ub}ub+{n2_cf}cf), "
            f"{elapsed:.0f}s"
        )

    total_time = time.monotonic() - t0
    mem_after = _get_memory()
    oom = _check_oom()
    return {
        "config": "C: 4B Q8 dual 4+2 + FactArbiter",
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

    total_time = time.monotonic() - t0
    mem_after = _get_memory()
    oom = _check_oom()
    return {
        "config": "C: 4B Q8 dual 4+2 + FactArbiter",
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
    log("Extraction Config Comparison: A (8B Q8) vs B (8B Q4 workstealer) vs C (4B Q8 4+2)")
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
        result_a = run_config_a(all_turns)
        all_results.append(result_a)
        print_detailed([result_a])

    # ── Cleanup ──
    log("\n--- cleanup for next config ---")
    _podman_stop_inference()
    _reclaim_memory()
    time.sleep(10)

    # ── Config C: 4B Q8 dual 4+2 (production backup) ──
    log("\n" + "=" * 60)
    log("CONFIG C: 4B Q8 dual 4+2 (8082 pass1, 8083 pass2)")
    log("=" * 60)
    _ensure_stopped()
    _reclaim_memory()
    time.sleep(5)
    if not _start_primary("day-extractor-4b-q8"):
        log("FATAL: Config C primary start failed")
    elif not _start_secondary("day-extractor-4b-q8-b"):
        log("FATAL: Config C secondary start failed")
    else:
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
