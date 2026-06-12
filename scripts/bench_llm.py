#!/usr/bin/env python3
# Status: production
# Path: manual — dev tool
"""DevForge LLM Benchmark — decode tps 측정 및 실험 레지스트리 등록.

Usage:
  python3 bench_llm.py                          # localhost:8082 측정 (3 runs)
  python3 bench_llm.py --port 8082              # 포트 지정
  python3 bench_llm.py --runs 5                 # 5회 측정
  python3 bench_llm.py --label "my-test" --register --verdict optimal --rationale "..."
  python3 bench_llm.py --port 8080 --register --category concurrent --subcategory pin-test
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional

PSQL = ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
        "-d", "devforge_app", "--no-align", "--tuples-only", "--quiet"]

PROMPT_MEDIUM = "Write a detailed explanation of how attention mechanisms work in transformer architectures, including multi-head attention, self-attention, and cross-attention."


def escape_sql_string(s: str) -> str:
    """Escape string for safe SQL literal interpolation."""
    return s.replace("\x00", "").replace("\\", "\\\\").replace("'", "''").replace("\n", " ").replace("\r", " ")
esc_sql = escape_sql_string  # alias


def psql(sql: str) -> str:
    try:
        r = subprocess.run(PSQL + ["-c", sql], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            print(f"  SQL ERROR: {r.stderr.strip()[:200]}")
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception as e:
        print(f"  SQL ERROR: {e}")
        return ""


def run_benchmark(port: int, runs: int, n_predict: int, prompt: str) -> dict:
    """Run benchmark against llama-server /completion endpoint."""
    results = []
    for i in range(runs):
        t0 = time.monotonic()
        body = json.dumps({
            "prompt": prompt,
            "n_predict": n_predict,
            "temperature": 0,
            "cache_prompt": False,
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/completion",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read())
                elapsed = time.monotonic() - t0
                timings = data.get("timings", {})
                predicted_per_s = timings.get("predicted_per_second", 0)
                predicted_n = timings.get("predicted_n", 0)
                prompt_per_s = timings.get("prompt_per_second", 0)
                results.append({
                    "run": i + 1,
                    "predicted_per_second": predicted_per_s,
                    "prompt_per_second": prompt_per_s,
                    "predicted_n": predicted_n,
                    "elapsed_s": round(elapsed, 2),
                })
                print(f"  Run {i+1}/{runs}: decode {predicted_per_s:.2f} t/s  "
                      f"prefill {prompt_per_s:.2f} t/s  ({elapsed:.1f}s)")
        except Exception as e:
            print(f"  Run {i+1}/{runs} FAILED: {e}")
            results.append({"run": i + 1, "error": str(e)})

    good = [r for r in results if "error" not in r]
    if not good:
        return {"runs": runs, "error": "all_runs_failed", "results": results}

    decode_tps_list = [r["predicted_per_second"] for r in good]
    prefill_tps_list = [r["prompt_per_second"] for r in good]
    avg_decode = sum(decode_tps_list) / len(decode_tps_list)
    avg_prefill = sum(prefill_tps_list) / len(prefill_tps_list)

    return {
        "runs": runs,
        "n_predict": n_predict,
        "prompt_len_chars": len(prompt),
        "decode_tps": round(avg_decode, 2),
        "prefill_tps": round(avg_prefill, 2),
        "min_decode": round(min(decode_tps_list), 2),
        "max_decode": round(max(decode_tps_list), 2),
        "stddev": round(
            (sum((d - avg_decode) ** 2 for d in decode_tps_list) / len(decode_tps_list)) ** 0.5, 3
        ) if len(decode_tps_list) > 1 else 0,
        "results": results,
    }


def auto_discover_config(port: int) -> dict:
    """Auto-discover config from container environment."""
    config = {"port": port}
    # Determine which pod based on port
    if port == 8082:
        config["component"] = "pod-a"
        config["mode"] = "day_reviewer"
        config["container"] = "devforge-pod-a"
    elif port == 8080:
        config["component"] = "pod-b"
        config["mode"] = "day_mcp"
        config["container"] = "devforge-pod-b"
    elif port == 8081:
        config["component"] = "pod-b"
        config["mode"] = "night_verify"
        config["container"] = "devforge-pod-b"

    # Try to get model info from health endpoint
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            health = json.loads(resp.read())
            if "model" in health:
                config["model"] = health["model"]
            if "model_path" in health:
                config["model_path"] = health["model_path"]
    except Exception:
        pass

    # Try to read active_config from DB
    row = psql(f"SELECT config FROM active_config WHERE component = '{esc_sql(config.get('component', ''))}'")
    if row:
        try:
            db_config = json.loads(row)
            config["threads"] = db_config.get("threads")
            config["cpus"] = db_config.get("cpus")
            config["batch_size"] = db_config.get("batch_size")
            config["cache_type_k"] = db_config.get("cache_type_k")
            config["cache_type_v"] = db_config.get("cache_type_v")
            config["flash_attn"] = db_config.get("flash_attn")
        except (json.JSONDecodeError, KeyError):
            pass

    return config


def register_experiment(
    experiment_id: str,
    category: str,
    subcategory: str,
    config: dict,
    results: dict,
    verdict: str,
    rationale: str,
):
    """Insert experiment into experiment_registry using dollar-quoting."""
    config_str = json.dumps(config, ensure_ascii=False)
    results_str = json.dumps(results, ensure_ascii=False)

    sql = f"""INSERT INTO experiment_registry
    (experiment_id, category, subcategory, config, results, verdict, rationale)
    VALUES (
        '{esc_sql(experiment_id)}',
        '{esc_sql(category)}',
        '{esc_sql(subcategory)}',
        $json${config_str}$json$::jsonb,
        $json${results_str}$json$::jsonb,
        '{esc_sql(verdict)}',
        '{esc_sql(rationale)}'
    )
    ON CONFLICT (experiment_id) DO UPDATE SET
        config = EXCLUDED.config,
        results = EXCLUDED.results,
        verdict = EXCLUDED.verdict,
        rationale = EXCLUDED.rationale,
        created_at = NOW()
    RETURNING id;"""

    result = psql(sql)
    if result and result.strip().isdigit():
        print(f"\n  Registered: experiment_id={experiment_id} (id={result.strip()})")
        return int(result.strip())
    else:
        print(f"\n  ERROR: registration failed for {experiment_id}")
        return None


def main():
    parser = argparse.ArgumentParser(description="DevForge LLM Benchmark")
    parser.add_argument("--port", type=int, default=8080, help="llama-server port (default: 8080)")
    parser.add_argument("--runs", type=int, default=3, help="Number of benchmark runs (default: 3)")
    parser.add_argument("--n-predict", type=int, default=100, help="Tokens to predict per run (default: 100)")
    parser.add_argument("--prompt", default=PROMPT_MEDIUM, help="Prompt text for benchmark")
    parser.add_argument("--label", help="Short label for this experiment")
    parser.add_argument("--register", action="store_true", help="Register results in experiment_registry")
    parser.add_argument("--experiment-id", help="Experiment ID (auto-generated if omitted)")
    parser.add_argument("--category", default="llm-optimization", help="Experiment category")
    parser.add_argument("--subcategory", default="runtime-test", help="Experiment subcategory")
    parser.add_argument("--verdict", choices=["optimal", "accepted", "rejected", "baseline", "pending"],
                        default="pending", help="Experiment verdict (default: pending)")
    parser.add_argument("--rationale", default="", help="Rationale for this experiment")

    args = parser.parse_args()

    print(f"Benchmarking 127.0.0.1:{args.port}  ({args.runs} runs, n_predict={args.n_predict})")
    print()

    results = run_benchmark(args.port, args.runs, args.n_predict, args.prompt)

    if "error" in results:
        print(f"\nBenchmark FAILED: {results['error']}")
        sys.exit(1)

    avg_decode = results["decode_tps"]
    avg_prefill = results["prefill_tps"]
    print(f"\n  Avg decode: {avg_decode:.2f} t/s")
    print(f"  Avg prefill: {avg_prefill:.2f} t/s")
    if results["stddev"] > 0:
        print(f"  Stddev: {results['stddev']:.3f}  (min: {results['min_decode']:.2f}, max: {results['max_decode']:.2f})")

    if not args.register:
        return

    # Auto-discover config
    config = auto_discover_config(args.port)

    # Generate experiment_id
    if args.experiment_id:
        experiment_id = args.experiment_id
    elif args.label:
        utc_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        experiment_id = f"{args.label}_{utc_ts}"
    else:
        utc_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        experiment_id = f"bench_{args.port}_{utc_ts}"

    # Build rationale from label if provided
    rationale = args.rationale
    if args.label and not rationale:
        rationale = args.label

    register_experiment(
        experiment_id=experiment_id,
        category=args.category,
        subcategory=args.subcategory,
        config=config,
        results=results,
        verdict=args.verdict,
        rationale=rationale,
    )


if __name__ == "__main__":
    main()
