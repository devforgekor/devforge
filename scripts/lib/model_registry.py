#!/usr/bin/env python3
# Status: production
# Path: lib/pod_manager/models.py — SSOT for model registry
"""Model metadata registry — single source of truth for all model definitions.

Import by name: MODEL_METADATA, DAY_PHASE_MODELS, NIGHT_MODELS.
"""

MODEL_METADATA = {
    "embeder": {
        "file": "Qwen3-Embedding-8B-Q8_0.gguf",
        "size": "7.5GB",
        "port": 8081,
        "mode": "embed",
        "model_name": "embeder",
        "ctx": 2048,
        "threads": 2,
        "threads_batch": 2,
        "parallel": 1,
        "cpus": "2-3",
    },
    "reranker": {
        "file": "Qwen3-Reranker-4B-Q8_0.gguf",
        "size": "4.0GB",
        "port": 8080,
        "mode": "rerank",
        "model_name": "reranker",
        "ctx": 4096,
        "threads": 4,
        "threads_batch": 4,
        "batch_size": 2048,
        "ubatch_size": 2048,
    },
    "day-extractor": {
        "file": "Qwen3-8B-Q8_0.gguf",
        "size": "8.2GB",
        "port": 8082,
        "mode": "day",
        "model_name": "day-extractor",
        "ctx": 8192,
        "threads": 4,
        "threads_batch": 4,
        "parallel": 2,
        "ubatch_size": 256,
        "cpus": "0-3",
        "cpu_range": "0-3",
        "cpu_strict": "1",
        "cache_ram": 1024,
        "cache_type_k": "q8_0",
        "cache_type_v": "q8_0",
        "flash_attn": "1",
    },
    "day-verifier": {
        "file": "veritas-8B-fact-checker-non-thinking-1.0.Q8_0.gguf",
        "size": "8.2GB",
        "port": 8082,
        "mode": "day",
        "model_name": "day-verifier",
        "ctx": 4096,
        "threads": 2,
        "threads_batch": 2,
        "parallel": 2,
        "ubatch_size": 512,
        "cpus": "0-1",
        "cache_ram": 2048,
        "cache_type_k": "q8_0",
        "cache_type_v": "q8_0",
    },
}

DAY_PHASE_MODELS = {
    "day_extract": "day-extractor",
    "day_enrich": "day-extractor",
    "day_verify": "day-verifier",
}

NIGHT_MODELS = frozenset()  # night_cycle archived 2026-09-08
