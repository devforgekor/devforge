#!/usr/bin/env python3
# Status: experimental
# Path: none — embedding model benchmark tool
"""Embedding Model Benchmark — compare faithfulness accuracy, speed, memory.

Usage:
  python3 scripts/embed_bench.py                     # full benchmark (all models)
  python3 scripts/embed_bench.py --models bge-m3     # single model
  python3 scripts/embed_bench.py --dry-run            # load models only, no scoring

Output: JSON report at /opt/ai_data/embed_bench_report.json
"""

import json, os, sys, time, gc, tracemalloc
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql_json

# ── Config ───────────────────────────────────────────────────────────────
BENCH_DATA = "/opt/ai_data/embed_bench_pairs.json"
REPORT_OUT = "/opt/ai_data/embed_bench_report.json"
GGUF_DIR = "/opt/ai_data/models/gguf"
CACHE_DIR = "/opt/ai_data/models"

COLS = ["evidence", "source_text", "fact_type", "fact_confidence", "fact_action", "nli_verdict"]


def load_pairs() -> List[Dict]:
    with open(BENCH_DATA) as f:
        return json.load(f)


# ── Model runners ────────────────────────────────────────────────────────
class ModelRunner:
    """Base class for embedding model benchmark runner."""
    name: str
    label: str  # short display name

    def load(self) -> None:
        raise NotImplementedError

    def encode_batch(self, texts: List[str]) -> np.ndarray:
        """Return normalized embeddings of shape (N, D)."""
        raise NotImplementedError

    def memory_mb(self) -> float:
        """Return estimated memory usage in MB."""
        return 0.0


class STransformerRunner(ModelRunner):
    """sentence-transformers model (FP32 or ONNX)."""
    def __init__(self, name: str, model_id: str, label: str = None,
                 backend: str = "default", file_name: str = None):
        self.name = name
        self.model_id = model_id
        self.label = label or name
        self.backend = backend
        self.file_name = file_name
        self.model = None

    def load(self) -> None:
        from sentence_transformers import SentenceTransformer
        kw = {"cache_folder": CACHE_DIR}
        if self.backend == "onnx":
            kw["backend"] = "onnx"
            kw["model_kwargs"] = {"provider": "CPUExecutionProvider"}
            if self.file_name:
                kw["model_kwargs"]["file_name"] = self.file_name
        t0 = time.monotonic()
        self.model = SentenceTransformer(self.model_id, **kw)
        elapsed = time.monotonic() - t0
        dim = self.model.get_sentence_embedding_dimension()
        print(f"  [{self.label}] Loaded in {elapsed:.1f}s, dim={dim}")

    def encode_batch(self, texts: List[str]) -> np.ndarray:
        return self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

    def memory_mb(self) -> float:
        import psutil
        proc = psutil.Process()
        return proc.memory_info().rss / 1024 / 1024


class LlamaEmbedRunner(ModelRunner):
    """GGUF embedding model via llama-cpp-python."""
    def __init__(self, name: str, gguf_filename: str, label: str = None,
                 n_ctx: int = 2048, n_threads: int = 4):
        self.name = name
        self.gguf_path = os.path.join(GGUF_DIR, gguf_filename)
        self.label = label or name
        self.n_ctx = n_ctx
        self.n_threads = n_threads
        self.model = None

    def load(self) -> None:
        from llama_cpp import Llama
        if not os.path.exists(self.gguf_path):
            raise FileNotFoundError(f"GGUF not found: {self.gguf_path}")
        t0 = time.monotonic()
        size_gb = os.path.getsize(self.gguf_path) / 1024**3
        self.model = Llama(
            model_path=self.gguf_path,
            n_ctx=self.n_ctx,
            n_threads=self.n_threads,
            embedding=True,
            verbose=False,
        )
        elapsed = time.monotonic() - t0
        print(f"  [{self.label}] Loaded in {elapsed:.1f}s, size={size_gb:.1f}GB")

    def encode_batch(self, texts: List[str]) -> np.ndarray:
        embs = []
        for t in texts:
            emb = self.model.create_embedding(t)
            embs.append(emb["data"][0]["embedding"])
        arr = np.array(embs, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        return arr / np.where(norms == 0, 1, norms)

    def memory_mb(self) -> float:
        import psutil
        proc = psutil.Process()
        return proc.memory_info().rss / 1024 / 1024


# ── Benchmark runner ─────────────────────────────────────────────────────
def run_benchmark(runner: ModelRunner, pairs: List[Dict]) -> Dict[str, Any]:
    """Run full benchmark for one model runner."""
    print(f"\n{'='*60}")
    print(f"  Benchmark: {runner.label}")
    print(f"{'='*60}")

    # Load model
    gc.collect()
    t_load = time.monotonic()
    mem_before = _get_rss_mb()
    runner.load()
    mem_after = _get_rss_mb()
    load_time = time.monotonic() - t_load

    # Prepare texts
    ev_list = [p["evidence"] for p in pairs]
    src_list = [p["source_text"] for p in pairs]

    # Warmup (first batch is always slower)
    _ = runner.encode_batch(ev_list[:5])
    _ = runner.encode_batch(src_list[:5])

    # Full batch encode
    gc.collect()
    t0 = time.monotonic()
    ev_embs = runner.encode_batch(ev_list)
    src_embs = runner.encode_batch(src_list)
    batch_time = time.monotonic() - t0

    # Cosine similarities
    cosines = np.sum(ev_embs * src_embs, axis=1)
    scores = np.round(cosines * 100, 1)

    # Statistics
    stats = {
        "load_time_s": round(load_time, 1),
        "mem_delta_mb": round(mem_after - mem_before, 1),
        "batch_time_ms": round(batch_time * 1000, 1),
        "time_per_pair_ms": round(batch_time * 1000 / len(pairs), 2),
        "pairs_per_sec": round(len(pairs) / batch_time, 1),
        "mean_score": round(float(np.mean(scores)), 1),
        "median_score": round(float(np.median(scores)), 1),
        "std_score": round(float(np.std(scores)), 2),
        "min_score": round(float(np.min(scores)), 1),
        "max_score": round(float(np.max(scores)), 1),
        "p25_score": round(float(np.percentile(scores, 25)), 1),
        "p75_score": round(float(np.percentile(scores, 75)), 1),
    }

    # Distribution bins
    bins = [(0, 20), (20, 40), (40, 55), (55, 65), (65, 75), (75, 85), (85, 100)]
    distrib = {}
    for lo, hi in bins:
        cnt = int(np.sum((scores >= lo) & (scores < hi)))
        distrib[f"{lo}-{hi}"] = cnt
    stats["score_distribution"] = distrib

    # Agreement analysis
    # Ground truth from fact_confidence (100=faithful, 0=unfaithful)
    gt_binary = []
    for p in pairs:
        conf = p.get("fact_confidence")
        if conf is not None:
            gt_binary.append(1 if int(conf) >= 70 else 0)
        else:
            gt_binary.append(None)

    # Threshold sweep (0.40 to 0.85 step 0.05)
    threshold_results = []
    best_f1 = 0
    best_threshold = 0.75
    for thresh_pct in range(40, 90, 5):
        thresh = thresh_pct / 100
        tp = fp = tn = fn = 0
        for i, (cos, gt) in enumerate(zip(cosines, gt_binary)):
            if gt is None:
                continue
            pred = 1 if cos >= thresh else 0
            if pred == 1 and gt == 1:
                tp += 1
            elif pred == 1 and gt == 0:
                fp += 1
            elif pred == 0 and gt == 0:
                tn += 1
            elif pred == 0 and gt == 1:
                fn += 1
        total = tp + fp + tn + fn
        acc = (tp + tn) / total if total else 0
        prec = tp / (tp + fp) if (tp + fp) else 0
        rec = tp / (tp + fn) if (tp + fn) else 0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0
        threshold_results.append({
            "threshold": thresh,
            "accuracy": round(acc, 3),
            "precision": round(prec, 3),
            "recall": round(rec, 3),
            "f1": round(f1, 3),
            "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        })
        if f1 > best_f1:
            best_f1 = f1
            best_threshold = thresh

    stats["best_threshold"] = best_threshold
    stats["best_f1"] = round(best_f1, 3)
    stats["threshold_sweep"] = threshold_results

    # Per-fact-type analysis
    type_scores = {}
    for i, p in enumerate(pairs):
        ft = p.get("fact_type", "unknown")
        if ft not in type_scores:
            type_scores[ft] = []
        type_scores[ft].append(float(scores[i]))
    stats["by_fact_type"] = {
        ft: {
            "mean": round(float(np.mean(v)), 1),
            "count": len(v),
            "std": round(float(np.std(v)), 2),
        }
        for ft, v in type_scores.items()
    }

    print(f"  Mean score: {stats['mean_score']}")
    print(f"  Best threshold: {best_threshold} (F1={best_f1:.3f})")
    print(f"  Batch: {len(pairs)} pairs in {stats['batch_time_ms']}ms "
          f"({stats['pairs_per_sec']}/s)")
    print(f"  Memory: +{stats['mem_delta_mb']}MB")

    return stats


def _get_rss_mb() -> float:
    import psutil
    return psutil.Process().memory_info().rss / 1024 / 1024


# ── Main ─────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Embedding model benchmark")
    parser.add_argument("--models", nargs="+", default=None,
                        help="Models to test (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Load models only, no scoring")
    parser.add_argument("--output", default=REPORT_OUT,
                        help=f"Output path (default: {REPORT_OUT})")
    args = parser.parse_args()

    # Define model runners
    ALL_MODELS: List[ModelRunner] = [
        STransformerRunner(
            "bge-m3", "BAAI/bge-m3", label="BGE-M3 FP32",
        ),
        STransformerRunner(
            "bge-m3-onnx", "BAAI/bge-m3", label="BGE-M3 ONNX",
            backend="onnx",
        ),
        STransformerRunner(
            "e5-large-instruct", "intfloat/multilingual-e5-large-instruct",
            label="E5-Large-Instruct FP32",
        ),
    ]

    # Check which GGUF models are downloaded
    if os.path.isdir(GGUF_DIR):
        for fname in os.listdir(GGUF_DIR):
            if fname.endswith(".gguf"):
                label = fname.replace(".gguf", "").replace("qwen3-embed-4b-", "Qwen3-4B-")
                ALL_MODELS.append(LlamaEmbedRunner(
                    f"qwen3-{fname}", fname, label=label,
                ))

    # Filter
    if args.models:
        ALL_MODELS = [m for m in ALL_MODELS if m.name in args.models
                      or m.label in args.models]

    # Load pairs
    pairs = load_pairs()
    print(f"Loaded {len(pairs)} benchmark pairs from {BENCH_DATA}")

    if args.dry_run:
        for m in ALL_MODELS:
            print(f"\n  [dry-run] Loading {m.label}...")
            try:
                m.load()
                test = m.encode_batch(["test sentence", "hello world"])
                print(f"  [dry-run]   Embedding OK: {test.shape}")
            except Exception as e:
                print(f"  [dry-run]   FAIL: {e}")
        print("\nDry run complete.")
        return

    # Run benchmarks
    results = {}
    for runner in ALL_MODELS:
        try:
            stats = run_benchmark(runner, pairs)
            results[runner.name] = stats
        except Exception as e:
            print(f"  [{runner.label}] ERROR: {e}")
            import traceback
            traceback.print_exc()
            results[runner.name] = {"error": str(e)}

    # Summary table
    print(f"\n{'='*70}")
    print(f"  SUMMARY: Embedding Model Benchmark ({len(pairs)} pairs)")
    print(f"{'='*70}")
    print(f"  {'Model':<25s} {'Mean':>6s} {'Best@F1':>9s} {'Pairs/s':>8s} {'Mem':>6s} {'Load':>6s}")
    print(f"  {'-'*25} {'-'*6} {'-'*9} {'-'*8} {'-'*6} {'-'*6}")
    for name, r in results.items():
        if "error" in r:
            print(f"  {name:<25s} {'ERROR':>6s} {r['error']}")
            continue
        print(f"  {name:<25s} {r['mean_score']:>5.1f} "
              f"{r['best_f1']:>7.3f} @{r['best_threshold']:.2f} "
              f"{r['pairs_per_sec']:>7.1f} "
              f"{r['mem_delta_mb']:>5.0f}MB "
              f"{r['load_time_s']:>4.0f}s")

    # Save report
    report = {
        "metadata": {
            "num_pairs": len(pairs),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "models_tested": list(results.keys()),
        },
        "results": results,
    }
    with open(args.output, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\nReport saved to {args.output}")


if __name__ == "__main__":
    main()
