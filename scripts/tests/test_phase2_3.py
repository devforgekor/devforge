#!/usr/bin/env python3
# Status: experimental
# Path: none — comprehensive Phase 2 + Phase 3 extraction quality test runner
"""Phase 2+3: 4B extraction quality + 12-sample final comparison.

Phase 2 — 4B extraction test (Qwen3-4B-Instruct-Q8_0):
  - Single 4B model on 760-char turn → 3 facts max

Phase 3 — Top combos x 12 DB samples (short 4 + medium 4 + long 4):
  Best model across 12 real turns
  Quality metrics: GOOD/MIXED/FAIL, bad preds, duplicates, tokens

Usage:
    python3 scripts/tests/test_phase2_3.py         # normal run
    python3 scripts/tests/test_phase2_3.py --quick  # 1 sample each bucket
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.test_common import log, test_complete, test_heartbeat, test_setup
from lib.test_runner import BANNED_PREDICATES, PodBClient, RetryConfig, TestResult

# ── Config ─────────────────────────────────────────────────────────────────
BASE_PROMPT = """You are a fact extractor for a developer conversation. Extract factual triples (subject, predicate, object) that are EXPLICITLY present in the text.

Do NOT infer, summarize, or add information not present in the source.

RULES:
1. ATOMIC CLAIM: Each evidence MUST contain exactly ONE atomic claim.
2. SELF-CONTAINED: Resolve pronouns and implicit references.
3. SUBJECT-PREDICATE-OBJECT: Every fact MUST have all three. The predicate is a snake_case verb phrase describing the relation (NOT is/has/does/was/equals/exists).
4. CONCISE: Keep evidence under 12 words. Short, direct sentences only.
5. FAITHFULNESS: Directly traceable to source text. NO inference.
6. NO INVENT: If nothing extractable, return empty array. Do NOT force extraction."""

SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "extraction",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "extractions": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "evidence": {"type": "string", "maxLength": 120},
                            "subject": {"type": "string", "maxLength": 30},
                            "predicate": {"type": "string", "maxLength": 40},
                            "object": {"type": "string", "maxLength": 60},
                        },
                        "required": ["evidence", "subject", "predicate", "object"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["extractions"],
            "additionalProperties": False,
        },
    },
}

# Standard test turn (same as Phase 1, 760 chars)
TURN_760 = """아닙니다, 현재 parallel=1이라 한 번에 1턴씩 순차 처리 중입니다. 코드를 보면 ThreadPoolExecutor(max_workers=PARALLEL)로 20개를 한꺼번에 submit 하지만, PARALLEL=1이면 1개씩만 실행됩니다.

**문제 재분석**: 이전 crash는 threads=4 (3코어 oversubscription)가 원인이었지 parallel=2가 원인이 아니었을 가능성이 있습니다. `threads=3 + parallel=2 + cpus=0-2`면 각 slot이 3코어를 공유하지만 oversubscription 없이 llama-server `--cont-batching`의 동적 배치를 활용할 수 있습니다.
Now update the pipeline scripts back to `parallel=2` to match.
Restart inference with threads=3 + parallel=2, re-run.
아직 env 파일이 parallel=1 상태. `ensure_model`으로 env 업데이트 후 재시작.
이제 `--parallel 2 --threads 3 --cpus 0-2` 적용 완료. 원래 crash 원인은 **threads=4가 3코어를 oversubscription**해서 ggml thread pool이 context switching 폭증 → "Remote end closed"였습니다. `threads=3`으로 코어 수에 맞추면 parallel=2도 안정적으로 동작합니다.

다시 20턴 실행:"""

# 4B model config for inference (port 8082, day mode)
MODEL_4B = {
    "name": "qwen3-4b-2507-Q8",
    "file": "qwen3-4b-instruct-2507-q8_0.gguf",
    "size": "4.0GB",
    "ctx": 8192,
    "threads": 4,
    "parallel": 2,
    "ubatch": 512,
    "cpus": "0-2",
    "cache_ram": 2048,
}
MODEL_14B = {
    "name": "Qwen3-14B-Q4",
    "file": "Qwen3-14B-Q4_K_M.gguf",
    "size": "8.4GB",
    "ctx": 4096,
    "threads": 4,
    "parallel": 2,
    "ubatch": 256,
    "cpus": "0-2",
    "cache_ram": 512,
}
MODEL_9B = {
    "name": "Qwen3.5-9B-Q8",
    "file": "Qwen-Qwen3.5-9B-Q8_0.gguf",
    "size": "9.2GB",
    "ctx": 8192,
    "threads": 4,
    "parallel": 2,
    "ubatch": 512,
    "cpus": "0-2",
    "cache_ram": 2048,
}

ENV_PATH = "/opt/ai_data/scripts/current-mode-inference.env"
SVC_NAME = "devforge-inference.service"
MODEL_DIR = "/opt/ai_data/models/gguf"

QUICK = "--quick" in sys.argv
N_SAMPLES = 1 if QUICK else 4  # per bucket


# ── inference Control ──────────────────────────────────────────────────────────


def _env_content(model_cfg: dict) -> str:
    return f"""MODE=day
MODEL_NAME=test-{model_cfg["name"]}
PORT=8082
MODEL_FILE={model_cfg["file"]}
CTX_SIZE={model_cfg["ctx"]}
THREADS={model_cfg["threads"]}
THREADS_BATCH={model_cfg["threads"]}
CACHE_RAM={model_cfg["cache_ram"]}
CACHE_TYPE_K=q8_0
CACHE_TYPE_V=q8_0
UBATCH_SIZE={model_cfg["ubatch"]}
PARALLEL={model_cfg["parallel"]}
CPUS={model_cfg["cpus"]}
"""


def switch_model(model_cfg: dict, container: str = "devforge-inference") -> bool:
    """Write env, restart inference service, wait for model loaded."""
    model_file = os.path.join(MODEL_DIR, model_cfg["file"])
    if not os.path.exists(model_file):
        log(f"  MODEL FILE NOT FOUND: {model_file}")
        return False

    # Write env
    with open(ENV_PATH, "w") as f:
        f.write(_env_content(model_cfg))
    log(f"  Env written: {model_cfg['file']}")

    # Restart service
    r = subprocess.run(
        ["systemctl", "--user", "restart", SVC_NAME], capture_output=True, text=True, timeout=30
    )
    if r.returncode != 0:
        log(f"  Restart failed: {r.stderr[:200]}")
        return False
    log("  inference restart issued, waiting for model load...")

    # Wait for model loaded
    t0 = time.monotonic()
    timeout = 300  # 5 min max for 9B Q8
    while time.monotonic() - t0 < timeout:
        time.sleep(10)
        try:
            r = subprocess.run(
                [
                    "podman",
                    "exec",
                    container,
                    "curl",
                    "-s",
                    "-m",
                    "10",
                    "http://127.0.0.1:8082/v1/chat/completions",
                    "-H",
                    "Content-Type: application/json",
                    "-d",
                    '{"model":"test","messages":[{"role":"user","content":"ping"}],"max_tokens":1}',
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            data = json.loads(r.stdout) if r.stdout else {}
            if "error" not in data or data.get("error", {}).get("message") != "Loading model":
                elapsed = time.monotonic() - t0
                log(f"  Model loaded ({elapsed:.0f}s)")
                return True
        except Exception:
            pass
        if (time.monotonic() - t0) % 60 < 10:
            log(f"  Still loading... ({time.monotonic() - t0:.0f}s)")
    log(f"  TIMEOUT: model did not load in {timeout}s")
    return False


def restore_original_model():
    """Restore 9B Q8 model."""
    return switch_model(MODEL_9B)


# ── DB Samples ─────────────────────────────────────────────────────────────


def get_db_samples(bucket: str, limit: int) -> list:
    """Query turns by est_chars bucket. Returns [(id, text), ...]."""
    if bucket == "short":
        lo, hi = 200, 500
    elif bucket == "medium":
        lo, hi = 500, 1500
    else:
        lo, hi = 1500, 100000

    cmd = [
        "podman",
        "exec",
        "postgres",
        "psql",
        "-U",
        "devforge",
        "-d",
        "devforge_app",
        "-t",
        "-A",
        "-F",
        "||",
        "-c",
        f"SELECT id, text FROM turns WHERE char_length(COALESCE(text,'')) BETWEEN {lo} AND {hi} ORDER BY RANDOM() LIMIT {limit}",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        log(f"  DB query failed: {r.stderr[:200]}")
        return []

    samples = []
    for line in r.stdout.strip().split("\n"):
        if not line or "||" not in line:
            continue
        parts = line.split("||", 1)
        if len(parts) == 2:
            samples.append((parts[0].strip(), parts[1].strip()))
    return samples


# ── Single Test ─────────────────────────────────────────────────────────────


def run_test(client: PodBClient, turn_text: str, label: str, model_name: str) -> TestResult:
    """Run extraction on a single turn and return result."""
    messages = [
        {"role": "system", "content": BASE_PROMPT},
        {"role": "user", "content": turn_text},
    ]
    result = client.retry(
        client.call_llm,
        messages,
        schema=SCHEMA,
        max_tokens=1000,
        temperature=0.0,
        label=label,
        retry=RetryConfig(max_retries=2, backoff=15),
    )
    obs = json.dumps(
        {
            "domain": "extraction-test",
            "model": model_name,
            "label": label,
            "status": result.status,
            "facts": result.num_facts,
            "elapsed": round(result.elapsed, 1),
            "tokens": result.total_tokens,
            "error": result.error,
        },
        ensure_ascii=False,
    )
    log(f"  [OBS] {obs}")
    return result


# ── Phase 2 ────────────────────────────────────────────────────────────────


def run_phase2(client: PodBClient) -> dict:
    """Phase 2: Compare two 4B models on standard turn."""
    log("\n" + "=" * 60)
    log("PHASE 2: 4B Pre-filter Comparison")
    log("=" * 60)
    results = {}

    for model_key, model_cfg in [("4B", MODEL_4B)]:
        test_heartbeat(f"Phase 2: loading {model_cfg['name']}")
        log(f"\n--- Loading {model_cfg['name']} ({model_cfg['file']}) ---")
        ok = switch_model(model_cfg)
        if not ok:
            log(f"  SKIP {model_cfg['name']}: model load failed")
            results[model_key] = TestResult(
                label=f"P2-{model_key}", error=True, error_detail="model load failed"
            )
            continue

        test_heartbeat(f"Phase 2: testing {model_cfg['name']}")
        log(f"--- Running {model_cfg['name']} ---")
        result = run_test(client, TURN_760, f"P2-{model_key}", model_cfg["name"])
        results[model_key] = result

        if not result.error:
            log(result.summary())
            for line in result.facts_table(BANNED_PREDICATES):
                log(line)

    return results


# ── Phase 3 ────────────────────────────────────────────────────────────────


def run_phase3(client: PodBClient, model_cfg: dict) -> dict:
    """Phase 3: Run model on 12 DB samples."""
    log("\n" + "=" * 60)
    log(f"PHASE 3: {model_cfg['name']} x 12 DB samples")
    log("=" * 60)

    # Load model
    test_heartbeat(f"Phase 3: loading {model_cfg['name']}")
    ok = switch_model(model_cfg)
    if not ok:
        log("  SKIP Phase 3: model load failed")
        return {}

    # Get samples
    samples = []
    for bucket in ["short", "medium", "long"]:
        bucket_samples = get_db_samples(bucket, N_SAMPLES)
        samples.extend((bucket, sid, text) for sid, text in bucket_samples)
        log(f"  {bucket}: {len(bucket_samples)} samples")

    log(f"  Total samples: {len(samples)}")
    results = {}

    for i, (bucket, sid, text) in enumerate(samples, 1):
        test_heartbeat(f"Phase 3: sample {i}/{len(samples)} ({bucket})")
        label = f"P3-{model_cfg['name']}-{bucket}-{i}"
        log(f"\n--- [{i}/{len(samples)}] {label} ({len(text)} chars) ---")

        result = run_test(client, text, label, model_cfg["name"])
        results[f"{bucket}_{i}"] = {
            "id": sid,
            "bucket": bucket,
            "chars": len(text),
            "result": result,
        }

        if not result.error:
            log(result.summary())
            for line in result.facts_table(BANNED_PREDICATES):
                log(line)

    return results


# ── Summary ────────────────────────────────────────────────────────────────


def print_summary(phase2_results: dict, phase3_results: dict):
    """Pretty-print final summary."""
    log("\n" + "=" * 60)
    log("FINAL SUMMARY")
    log("=" * 60)

    # Phase 2
    log("\n--- Phase 2: 4B Single Model Test ---")
    for key, r in phase2_results.items():
        log(
            f"  {key}: {r.status} | {r.elapsed:.0f}s | {r.num_facts} facts | bad={r.bad_predicates} dup={r.duplicate_evidence}"
        )

    # Phase 3
    log("\n--- Phase 3: 12-Sample Results ---")
    by_bucket = {"short": [], "medium": [], "long": []}
    total_good = 0
    total_mixed = 0
    total_fail = 0

    for key, entry in phase3_results.items():
        r = entry["result"]
        bucket = entry["bucket"]
        status = r.status
        by_bucket[bucket].append(status)
        if status == "GOOD":
            total_good += 1
        elif status == "MIXED":
            total_mixed += 1
        elif status == "FAIL":
            total_fail += 1

    for bucket in ["short", "medium", "long"]:
        items = by_bucket[bucket]
        good = sum(1 for s in items if s == "GOOD")
        mixed = sum(1 for s in items if s == "MIXED")
        fail_ = sum(1 for s in items if s == "FAIL")
        log(f"  {bucket}: {good} GOOD, {mixed} MIXED, {fail_} FAIL (n={len(items)})")

    total = total_good + total_mixed + total_fail
    if total:
        log(f"\n  TOTAL: {total_good}/{total} GOOD ({100 * total_good // total}%)")
        log(
            f"  QUALITY: {total_good + total_mixed}/{total} usable ({(total_good + total_mixed) * 100 // total}%)"
        )


def model_name(key: str) -> str:
    names = {"4B": "qwen3-4b-2507-Q8"}
    return names.get(key, key)


# ── Main ───────────────────────────────────────────────────────────────────


def main():
    TEST = test_setup("phase2_3", "Phase 2 (4B comparison) + Phase 3 (12 samples)")

    client = PodBClient(container="devforge-inference", internal_port=8082, timeout=600)

    # Health check
    log("Health check (current model)...")
    h = client.health_check(timeout=15)
    if not h.ok:
        log(f"  Health check FAILED: {h.message}")
        log("  Will proceed anyway — model swap may fix")

    # ── Phase 2: 4B Comparison ──
    log("\n" + "#" * 60)
    log("# PHASE 2: 4B Pre-filter Comparison")
    log("#" * 60)
    phase2_results = run_phase2(client)

    # Revert to 9B Q8 for Phase 3
    log("\n--- Restoring 9B Q8 for Phase 3 ---")
    ok = restore_original_model()
    if not ok:
        log("  WARN: Could not restore 9B Q8, Phase 3 may fail")

    # ── Phase 3: 12-Sample Deep Test ──
    log("\n" + "#" * 60)
    log("# PHASE 3: 12-Sample Final Comparison")
    log("#" * 60)
    phase3_results = run_phase3(client, MODEL_9B)

    # ── Summary ──
    print_summary(phase2_results, phase3_results)

    # ── Cleanup ──
    test_complete("Phase 2+3 complete")

    # Exit code
    if phase2_results and any(r.is_usable for r in phase2_results.values()):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
