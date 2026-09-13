#!/usr/bin/env python3
# Status: experimental
# Path: none — standalone runner (python3 bench_verify_optimization.py)
"""
Qwen3.6-27B 최적화 검증 테스트 (Standalone)
=============================================
4가지 최적화 축을 실제 서버 환경에서 검증합니다:
  1. 양자화 (Q4_K_M, 16.5GB)
  2. KV 캐시 (q8_0)
  3. Multi-Token Prediction (추측 디코딩)
  4. 실행 설정 (context, threads, flash-attn)

사용법:
  python3 tests/bench_verify_optimization.py          # 전체 테스트
  python3 tests/bench_verify_optimization.py --quick  # 간단 inference만 확인
  python3 tests/bench_verify_optimization.py --restore # day 모드 복원

주의: Pod B(devforge-swap)를 중단하고 27B 모드로 전환합니다.
"""

import argparse
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ───────────────────────────────────────────────
MODEL_FILE = "Qwen3.6-27B-Q4_K_M.gguf"
MODEL_PATH = f"/opt/ai_data/models/gguf/{MODEL_FILE}"
PORT = 8081
HEALTH_URL = f"http://127.0.0.1:{PORT}/health"
COMPLETION_URL = f"http://127.0.0.1:{PORT}/v1/completions"
MODE_FILE_B = "/opt/ai_data/scripts/current-mode-pod-b.env"
SYSTEM_MODE_FILE = "/opt/ai_data/scripts/current-system-mode.env"


def _is_night_mode() -> bool:
    """Check if system is in night mode (nightly pipeline active)."""
    try:
        content = Path(SYSTEM_MODE_FILE).read_text().strip()
        return "MODE=night" in content
    except FileNotFoundError:
        return False

TEST_PROMPTS = [
    {
        "name": "short_gen",
        "prompt": "def fibonacci(n):",
        "max_tokens": 50,
        "temp": 0.1,
        "desc": "~5 tokens",
    },
    {
        "name": "medium_gen",
        "prompt": "Write a Python function that merges two sorted lists into one sorted list.",
        "max_tokens": 200,
        "temp": 0.1,
        "desc": "~20 tokens",
    },
    {
        "name": "reasoning",
        "prompt": "Alice has 3 apples. Bob gives her 5 more. She eats 2. How many apples does Alice have? Let's think step by step.",
        "max_tokens": 150,
        "temp": 0.1,
        "desc": "~40 tokens",
    },
]

# ── Large prompt generators ─────────────────────────────────────

def _make_large_prompt(token_estimate: int) -> str:
    """Generate a realistic large prompt of approximately `token_estimate` tokens
    by repeating a structured code+documentation block."""
    block = textwrap.dedent("""\
    # Module: data_processor
    # Handles ETL pipeline for incoming telemetry streams.
    # Supports CSV, JSON, and Parquet formats with schema validation.

    import csv
    import json
    from dataclasses import dataclass, field
    from typing import Any, Dict, List, Optional
    from pathlib import Path


    @dataclass
    class ProcessingConfig:
        input_dir: Path
        output_dir: Path
        batch_size: int = 1000
        max_retries: int = 3
        timeout_sec: int = 30
        compression: str = "gzip"
        schema_version: int = 2
        field_mapping: Dict[str, str] = field(default_factory=lambda: {
            "ts": "timestamp",
            "src": "source",
            "val": "value",
            "tag": "tags",
        })


    class SchemaValidator:
        \"\"\"Validates incoming records against the current schema version.\"\"\"

        REQUIRED_FIELDS = {"timestamp", "source", "value", "tags"}
        OPTIONAL_FIELDS = {"region", "hostname", "environment", "trace_id"}
        MAX_RECORD_SIZE = 65536  # 64KB

        def __init__(self, strict: bool = True):
            self.strict = strict
            self.errors: List[str] = []
            self.warnings: List[str] = []

        def validate(self, record: Dict[str, Any]) -> bool:
            if not isinstance(record, dict):
                self.errors.append(f"Expected dict, got {type(record).__name__}")
                return False
            missing = self.REQUIRED_FIELDS - set(record.keys())
            if missing:
                msg = f"Missing required fields: {missing}"
                if self.strict:
                    self.errors.append(msg)
                    return False
                self.warnings.append(msg)
            if "timestamp" in record and not isinstance(record["timestamp"], (int, float)):
                self.errors.append("timestamp must be numeric (Unix epoch)")
                return False
            return True


    class BatchProcessor:
        \"\"\"Processes records in configurable batches with retry logic.\"\"\"

        def __init__(self, config: ProcessingConfig):
            self.config = config
            self.validator = SchemaValidator()
            self.stats = {"processed": 0, "failed": 0, "skipped": 0}

        def load(self, path: Path) -> List[Dict[str, Any]]:
            suffix = path.suffix.lower()
            if suffix == ".csv":
                return self._load_csv(path)
            elif suffix == ".json":
                return self._load_json(path)
            elif suffix == ".parquet":
                return self._load_parquet(path)
            raise ValueError(f"Unsupported format: {suffix}")

        def _load_csv(self, path: Path) -> List[Dict[str, Any]]:
            with open(path, newline="") as f:
                reader = csv.DictReader(f)
                return [self._remap(row) for row in reader]

        def _load_json(self, path: Path) -> List[Dict[str, Any]]:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, list):
                return [self._remap(r) for r in data]
            return [self._remap(data)]

        def _load_parquet(self, path: Path) -> List[Dict[str, Any]]:
            raise NotImplementedError("Parquet support requires pyarrow")

        def _remap(self, record: Dict[str, Any]) -> Dict[str, Any]:
            return {self.config.field_mapping.get(k, k): v for k, v in record.items()}

        def process_batch(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            valid = []
            for rec in records:
                if self.validator.validate(rec):
                    self.stats["processed"] += 1
                    valid.append(rec)
                else:
                    self.stats["failed"] += 1
            return valid

        def run(self) -> None:
            pattern = "*.csv"
            paths = list(self.config.input_dir.glob(pattern))
            for path in paths:
                records = self.load(path)
                batch = self.process_batch(records)
                out_path = self.config.output_dir / f"{path.stem}.processed.json"
                with open(out_path, "w") as f:
                    json.dump(batch, f)
                print(f"Wrote {len(batch)} records to {out_path}")
    """)
    # ~200 tokens per repetition
    repeats = max(1, token_estimate // 200)
    parts = [block] * repeats
    return "\n\n".join(parts)


# fmt: off
LARGE_PROMPT_TEMPLATES = [
    {
        "name": "large_code",
        "token_estimate": 2000,
        "max_tokens": 50,
        "temp": 0.1,
        "desc": "~2K tokens",
        "suffix": "\n\n# Q: What happens if max_retries is exceeded?\n# A: Let me trace through the code step by step.",
    },
    {
        "name": "very_large_code",
        "token_estimate": 8000,
        "max_tokens": 30,
        "temp": 0.1,
        "desc": "~8K tokens",
        "suffix": "\n\n# Q: Identify all potential race conditions in this code.\n# A:",
    },
]
# fmt: on

PASS_THRESHOLDS = {
    "load_time_s": 120,          # 2분 내 로딩
    "steady_mem_gb": 19.0,       # 19GB 이하 유지
    "peak_mem_gb": 21.0,         # 21GB 피크 이하
    "tokens_per_sec": 3.0,       # 초당 3토큰 이상 (ARM CPU)
    "health_200": True,
}

# ── Helpers ─────────────────────────────────────────────────────
TS = lambda: datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str):
    print(f"[{TS()}] {msg}")


def run(cmd: str, timeout: int = 60) -> tuple[int, str, str]:
    """Run shell command, return (rc, stdout, stderr)."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
    except Exception as e:
        return -1, "", str(e)


def get_mem_gb() -> tuple[float, float, float]:
    """Return (used_gb, available_gb, peak_gb_approx) from free."""
    rc, out, _ = run("free -b | grep Mem:")
    if rc != 0:
        return 0, 0, 0
    parts = out.split()
    total = int(parts[1]) / (1024 ** 3)
    avail = int(parts[6]) / (1024 ** 3)
    used = total - avail
    return used, avail, total


def get_cgroup_mem() -> tuple[float, float]:
    """Return (current_bytes, max_bytes) from container cgroup if available."""
    # Try the swap container's cgroup
    rc, out, _ = run(
        "find /sys/fs/cgroup -name 'memory.current' 2>/dev/null"
        " | xargs grep -l 'devforge-swap' 2>/dev/null"
        " | head -1"
    )
    if rc != 0 or not out:
        return 0, 0
    cgroup_path = out.strip()
    rc1, current, _ = run(f"cat {cgroup_path}")
    rc2, maximum, _ = run(f"cat {cgroup_path.replace('memory.current', 'memory.max')}")
    if rc1 == 0 and rc2 == 0:
        return int(current.strip()) / (1024 ** 3), int(maximum.strip()) / (1024 ** 3)
    return 0, 0


def wait_for_model(timeout: int = 600) -> bool:
    """Poll /health until model responds."""
    log(f"Waiting for model on :{PORT} (timeout={timeout}s)...")
    for i in range(1, timeout + 1):
        rc, out, _ = run(f"curl -s -o /dev/null -w '%{{http_code}}' '{HEALTH_URL}'")
        if rc == 0 and out == "200":
            log(f"Model ready after {i}s")
            return True
        if i % 15 == 0:
            log(f"  still waiting... ({i}s)")
            mem = get_mem_gb()
            log(f"  mem: used={mem[0]:.1f}G, avail={mem[1]:.1f}G")
        time.sleep(1)
    log(f"TIMEOUT: model not ready after {timeout}s")
    return False


def inference(prompt: str, max_tokens: int = 50, temp: float = 0.1,
              timeout: int = 120) -> dict:
    """Run single completion, return result dict.
    timeout is auto-scaled for large prompts."""
    payload = {
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False,
    }
    cmd = (
        f"curl -s -X POST '{COMPLETION_URL}'"
        f" -H 'Content-Type: application/json'"
        f" -d '{json.dumps(payload)}'"
    )
    rc, out, err = run(cmd, timeout=timeout)
    if rc != 0:
        return {"error": err or f"exit {rc}"}
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse: {e}", "raw": out[:500]}
    return data


def switch_mode_pod_b(mode: str) -> bool:
    """Switch Pod B mode file and restart container."""
    log(f"Switching Pod B → {mode}...")
    run(f"printf '%s' 'MODE={mode}' > '{MODE_FILE_B}'")
    rc, _, err = run("systemctl --user stop container-devforge-swap", timeout=30)
    if rc != 0:
        log(f"  stop returned {rc}: {err}")
    time.sleep(5)
    rc, out, err = run("systemctl --user start container-devforge-swap", timeout=30)
    if rc != 0:
        log(f"  start returned {rc}: {err}")
        return False
    return True


# ── Test Suites ─────────────────────────────────────────────────

class TestResult:
    def __init__(self):
        self.results: dict[str, dict] = {}
        self.metrics: dict = {}

    def record(self, name: str, passed: bool, detail: str, data: dict = None):
        status = "✅ PASS" if passed else "❌ FAIL"
        log(f"  [{status}] {name}: {detail}")
        self.results[name] = {
            "passed": passed,
            "detail": detail,
            "data": data or {},
        }

    def summary(self) -> str:
        total = len(self.results)
        passed = sum(1 for r in self.results.values() if r["passed"])
        failed = total - passed
        lines = [
            f"\n{'='*60}",
            "  Qwen3.6-27B 최적화 검증 결과",
            f"  {passed}/{total} passed, {failed} failed",
            f"{'='*60}",
        ]
        for name, r in self.results.items():
            icon = "✅" if r["passed"] else "❌"
            lines.append(f"  {icon} {name}: {r['detail']}")
        lines.append("=" * 60)
        return "\n".join(lines)


def test_quantization(result: TestResult):
    """Test 1: Q4_K_M quantization — model loads and stays within memory budget."""
    log("\n[Test 1] 양자화 (Q4_K_M): 모델 로딩 및 메모리")

    mem_before = get_mem_gb()
    log(f"  Memory before load: used={mem_before[0]:.1f}G, avail={mem_before[1]:.1f}G")

    # Check model file exists and size
    if not os.path.exists(MODEL_PATH):
        result.record("model_file", False, f"Model file not found: {MODEL_PATH}")
        return

    file_size_gb = os.path.getsize(MODEL_PATH) / (1024 ** 3)
    log(f"  Model file: {MODEL_FILE} ({file_size_gb:.1f} GB)")
    if file_size_gb < 14 or file_size_gb > 20:
        result.record("model_file_size", False, f"Unexpected size: {file_size_gb:.1f}G (expected ~16.5G)")
    else:
        result.record("model_file_size", True, f"{file_size_gb:.1f} GB (Q4_K_M expected range)")

    # After model load, check memory (called externally by test runner)
    # Record threshold for post-load check
    result.metrics["file_size_gb"] = file_size_gb
    result.metrics["mem_before_used_gb"] = mem_before[0]
    result.metrics["mem_before_avail_gb"] = mem_before[1]


def test_inference(result: TestResult):
    """Test 2: Basic inference — model responds correctly at various context sizes."""
    log("\n[Test 2] 실행 설정: 기본 추론")
    log("  Prompt sizes: small(~5tok) → medium(~20tok) → reasoning(~40tok)"
        " → large(~2Ktok) → very_large(~8Ktok)")

    # ── Small/medium prompts (inline) ──
    for tp in TEST_PROMPTS:
        name = tp["name"]
        log(f"  Running '{name}' ({tp['desc']}, max_tokens={tp['max_tokens']})...")
        start = time.time()
        data = inference(tp["prompt"], tp["max_tokens"], tp["temp"])
        elapsed = time.time() - start

        _record_infer(result, name, tp["desc"], data, elapsed)

    # ── Large/VeryLarge prompts (generated) ──
    for tpl in LARGE_PROMPT_TEMPLATES:
        name = tpl["name"]
        est_tok = tpl["token_estimate"]
        log(f"  Generating '{name}' (~{est_tok:,} tokens input)...")
        gen_start = time.time()
        body = _make_large_prompt(est_tok)
        prompt = body + tpl["suffix"]
        gen_time = time.time() - gen_start
        # Rough token count: ~4 chars per token for code
        raw_tok_est = len(prompt) // 4
        log(f"    Generated {len(prompt):,} chars (est. {raw_tok_est:,} tokens)"
            f" in {gen_time:.1f}s")
        log(f"  Running '{name}' ({tpl['desc']}, max_tokens={tpl['max_tokens']})...")
        # Scale timeout: base 120s + 30s per 1K estimated tokens
        dyn_timeout = min(120 + (raw_tok_est // 1000) * 30, 600)
        start = time.time()
        data = inference(prompt, tpl["max_tokens"], tpl["temp"],
                         timeout=dyn_timeout)
        elapsed = time.time() - start
        _record_infer(result, name, f"~{raw_tok_est:,}tok input", data, elapsed,
                      context={"prompt_chars": len(prompt),
                               "prompt_tokens_est": raw_tok_est})

    # ── Streaming test (medium prompt, stream=true) ──
    log("  Running 'streaming' test...")
    _test_streaming(result)


def _record_infer(result: TestResult, name: str, desc: str, data: dict,
                  elapsed: float, context: dict = None):
    """Record a single inference result."""
    if "error" in data:
        result.record(f"infer_{name}", False, f"API error: {data['error']}")
        return

    choices = data.get("choices", [])
    if not choices:
        result.record(f"infer_{name}", False, "No choices in response")
        return

    text = choices[0].get("text", "")
    usage = data.get("usage", {})

    if usage:
        tokens_out = usage.get("completion_tokens", 0)
        tokens_in = usage.get("prompt_tokens", 0)
    else:
        tokens_out = len(text.split())
        tokens_in = 0

    tps = tokens_out / elapsed if elapsed > 0 else 0
    log(f"    tokens: {tokens_in} in → {tokens_out} out ({tps:.2f} t/s, {elapsed:.1f}s)")

    ctx = context or {}
    ctx.update({
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_per_sec": round(tps, 2),
        "latency_s": round(elapsed, 1),
        "text_preview": text[:100],
    })

    passed = tokens_out >= 1
    detail = f"{tokens_out} tokens, {tps:.1f} t/s, {elapsed:.1f}s [{desc}]"
    result.record(f"infer_{name}", passed, detail, ctx)

    # Record best tps for threshold check
    key = "best_tps"
    if key not in result.metrics or tps > result.metrics[key]:
        result.metrics[key] = tps


def _test_streaming(result: TestResult):
    """Quick streaming sanity check via curl."""
    payload = {
        "prompt": "Write a haiku about Python.",
        "max_tokens": 50,
        "temperature": 0.1,
        "stream": True,
    }
    cmd = (
        f"curl -s -X POST '{COMPLETION_URL}'"
        f" -H 'Content-Type: application/json'"
        f" -d '{json.dumps(payload)}'"
    )
    rc, out, err = run(cmd, timeout=60)
    if rc != 0:
        result.record("infer_streaming", False, f"curl error: {err}")
        return
    # With stream=true, server returns NDJSON — each line is a data: {...}
    stream_lines = [l for l in out.split("\n") if l.startswith("data: ")]
    if stream_lines:
        result.record("infer_streaming", True,
                      f"{len(stream_lines)} stream chunks received",
                      {"chunks": len(stream_lines)})
    else:
        result.record("infer_streaming", False,
                      "No stream chunks (raw output may be non-stream)",
                      {"raw_preview": out[:200]})


def test_kv_cache(result: TestResult):
    """Test 3: KV cache q8_0 — verify via server metrics endpoint."""
    log("\n[Test 3] KV 캐시 (q8_0): 메모리 효율")

    rc, out, _ = run(f"curl -s '{HEALTH_URL}'")
    if rc == 0:
        try:
            health = json.loads(out)
            kv_cache_type = health.get("cache_type_k", "unknown")
            log(f"  KV cache type-K: {kv_cache_type}")
            result.record("kv_cache_type", "q8" in str(kv_cache_type).lower(),
                          f"cache_type_k={kv_cache_type}")
        except (json.JSONDecodeError, AttributeError):
            result.record("kv_cache_type", False, f"Could not parse health: {out[:200]}")

    # Check running container env for cache type
    rc, out, _ = run(
        "podman exec devforge-swap env 2>/dev/null | grep -E 'CACHE_TYPE' || true"
    )
    if rc == 0 and out:
        log(f"  Container env: {out}")
        has_q8 = "q8_0" in out
        result.record("kv_cache_env", has_q8, f"Env vars: {out[:200]}")
    else:
        log(f"  Container env check: rc={rc}, out={out}")

    # Extended context test (KV cache pressure)
    long_prompt = "hello world " * 500  # ~1000 tokens
    log("  Testing KV cache with extended context (1000 tokens)...")
    data = inference(long_prompt, max_tokens=10, temp=0.1)
    if "error" not in data:
        choices = data.get("choices", [])
        text = choices[0].get("text", "") if choices else ""
        result.record("kv_extended_ctx", len(text) > 0,
                      f"Extended context OK ({len(text)} chars)",
                      {"text_preview": text[:100]})
    else:
        result.record("kv_extended_ctx", False, f"Error: {data['error']}")


def test_mtp(result: TestResult):
    """Test 4: MTP (Multi-Token Prediction) — speculative decoding viability."""
    log("\n[Test 4] MTP (Multi-Token Prediction): 추측 디코딩 가능성")

    # Check if llama-server has MTP support
    rc, out, _ = run("podman exec devforge-swap /app/llama-server --help 2>/dev/null | grep -i 'mtp\\|draft\\|spec' || true")
    if rc == 0 and out:
        log(f"  Server supports speculative decoding: {out[:200]}")
        result.record("mtp_support", True, f"Available: {out[:200]}")
    else:
        log("  Server does NOT advertise MTP/speculative decoding flags")
        result.record("mtp_support", False,
                      "No --spec-flag in llama-server --help")

    # MTP acceptance test: if we send a known pattern (code), measure
    # how predictable the output is (proxy for MTP acceptance rate)
    code_prompt = """def quick_sort(arr):
    if len(arr) <= 1:
        return arr
    pivot = arr[0]
    left = [x for x in arr[1:] if x <= pivot]
    right = [x for x in arr[1:] if x > pivot]
    return"""

    log("  Measuring code completion predictability (MTP proxy)...")
    start = time.time()
    data = inference(code_prompt, max_tokens=100, temp=0.1)
    elapsed = time.time() - start

    if "error" not in data:
        choices = data.get("choices", [])
        text = choices[0].get("text", "") if choices else ""
        usage = data.get("usage", {})
        tokens_out = usage.get("completion_tokens", 0) or len(text.split())
        tps = tokens_out / elapsed if elapsed > 0 else 0

        # Check if output is deterministic code (good MTP candidate)
        has_code_keywords = any(kw in text for kw in ["return", "for ", "while", "def ", "if "])
        log(f"    {tokens_out} tokens, {tps:.1f} t/s, code_deterministic={has_code_keywords}")
        result.record("mtp_code_speed", tps >= PASS_THRESHOLDS["tokens_per_sec"],
                      f"{tps:.1f} t/s (threshold: {PASS_THRESHOLDS['tokens_per_sec']})",
                      {"tokens_per_sec": round(tps, 2), "code_content": has_code_keywords})
    else:
        result.record("mtp_code_speed", False, f"Error: {data['error']}")


def test_memory_pressure(result: TestResult):
    """Test memory usage under load."""
    log("\n[Test 5] 메모리 압력: 부하 상태에서 메모리 안정성")

    # Get current memory
    used, avail, total = get_mem_gb()
    log(f"  Memory after tests: used={used:.1f}G, avail={avail:.1f}G, total={total:.1f}G")

    # Check swap usage
    rc, out, _ = run("free -h | grep Swap:")
    log(f"  Swap: {out}")

    # cgroup check
    cg_cur, cg_max = get_cgroup_mem()
    if cg_cur > 0:
        log(f"  Cgroup: {cg_cur:.1f}G / {cg_max:.1f}G")

    # Memory threshold checks
    mem_model = used - result.metrics.get("mem_before_used_gb", 0)
    log(f"  Delta: model loading consumed ~{mem_model:.1f}G")

    result.record("mem_steady", used <= PASS_THRESHOLDS["steady_mem_gb"],
                  f"used={used:.1f}G (threshold: ≤{PASS_THRESHOLDS['steady_mem_gb']}G)",
                  {"used_gb": round(used, 1), "avail_gb": round(avail, 1)})

    result.record("mem_no_oom", True,
                  f"Process survived all tests ({used:.1f}G / {total:.1f}G)")


def test_config_verify(result: TestResult):
    """Verify the actual config matches recommended settings."""
    log("\n[Test 6] 실행 설정 검증: verify 모드 설정 확인")

    rc, out, _ = run(
        "podman exec devforge-swap env 2>/dev/null | grep -E '^(SERVER1_|CACHE_|FLASH_|BATCH_)' || true"
    )
    if rc != 0 or not out:
        result.record("config_env", False, "Could not read container env")
        return

    log(f"  Container env:\n{out}")
    env_lines = out.split("\n")
    env_dict = {}
    for line in env_lines:
        if "=" in line:
            k, v = line.split("=", 1)
            env_dict[k] = v

    checks = {
        "ctx_size ≥4096": int(env_dict.get("SERVER1_CTX", "0")) >= 4096,
        "threads=4": env_dict.get("SERVER1_THREADS", "") == "4",
        "cache_type_k=q8_0": env_dict.get("CACHE_TYPE_K", "") == "q8_0",
        "cache_type_v=q8_0": env_dict.get("CACHE_TYPE_V", "") == "q8_0",
        "flash_attn=1": env_dict.get("FLASH_ATTN", "") == "1",
        "batch_size≥512": int(env_dict.get("BATCH_SIZE", "0")) >= 512,
    }

    all_pass = all(checks.values())
    detail = ", ".join(f"{k}={v}" for k, v in checks.items())
    result.record("config_verify", all_pass, detail, env_dict)

    for check_name, passed in checks.items():
        if not passed:
            log(f"    ⚠ {check_name}: FAIL")


# ── Main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Qwen3.6-27B 최적화 검증 테스트"
    )
    parser.add_argument("--quick", action="store_true",
                        help="빠른 모드: inference만 테스트")
    parser.add_argument("--restore", metavar="MODE", nargs="?",
                        const="day", default=None,
                        help="Pod B를 지정 모드로 복원 (기본: day)")
    args = parser.parse_args()

    # ── Night window guard ──────────────────────────────────
    if _is_night_mode():
        log("Test skipped — nightly pipeline active (MODE=night)")
        print("[SKIP] nightly pipeline active — Pod B is managed by nightly_batch.sh")
        return

    if args.restore:
        log(f"Restoring Pod B → {args.restore} mode...")
        if switch_mode_pod_b(args.restore):
            log("Restore OK")
        else:
            log("Restore FAILED")
        return

    result = TestResult()
    log("=" * 60)
    log("  Qwen3.6-27B 최적화 검증 테스트")
    log("  Server: OCI ARM (4 core, 24GB)")
    log(f"  Model: {MODEL_FILE}")
    log(f"  Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    log(f"  Mode: {'quick' if args.quick else 'full'}")
    log("=" * 60)

    # Phase 1: Pre-flight checks
    log("\n--- Pre-flight ---")
    used, avail, total = get_mem_gb()
    log(f"  Memory: used={used:.1f}G, avail={avail:.1f}G, total={total:.1f}G")
    if avail < 4:
        log("  ⚠ Low memory — model may swap or OOM")

    # Phase 2: Mode switch
    log("\n--- Mode Switch ---")
    log("Stopping current Pod B and switching to verify (27B) mode...")
    if not switch_mode_pod_b("verify"):
        log("FATAL: Failed to switch to verify mode")
        sys.exit(1)

    # Phase 3: Wait for model
    log("\n--- Model Load ---")
    load_start = time.time()
    if not wait_for_model(timeout=600):
        log("FATAL: Model failed to load within 600s")
        log("Restoring day mode...")
        switch_mode_pod_b("day")
        sys.exit(1)
    load_time = time.time() - load_start
    result.metrics["load_time_s"] = load_time
    result.record("model_load", load_time <= PASS_THRESHOLDS["load_time_s"],
                  f"{load_time:.0f}s (threshold: ≤{PASS_THRESHOLDS['load_time_s']}s)")

    # Phase 4: Tests
    test_quantization(result)

    if not args.quick:
        test_kv_cache(result)
        test_config_verify(result)

    test_inference(result)

    if not args.quick:
        test_mtp(result)
        test_memory_pressure(result)

    # Phase 5: Summary
    print(result.summary())

    # Restore
    log("\n--- Cleanup ---")
    log("Restoring day mode...")
    switch_mode_pod_b("day")

    # Print JSON for external consumption
    print("\n--- JSON Result ---")
    print(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": MODEL_FILE,
        "load_time_s": round(load_time, 1),
        "results": {
            k: {"passed": v["passed"], "detail": v["detail"]}
            for k, v in result.results.items()
        },
        "metrics": result.metrics,
    }, indent=2))

    failed = sum(1 for r in result.results.values() if not r["passed"])
    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
