#!/usr/bin/env python3
# Status: production
"""Model metadata definitions for Pod A / Pod B."""

MODEL_METADATA = {
    "embeder":      {
        "file": "Qwen3-Embedding-8B-Q8_0.gguf",
        "size": "7.5GB", "port": 8081, "mode": "embed",
        "model_name": "embeder", "ctx": 16384,
        "threads": 4, "threads_batch": 4,
        "parallel": 1,
    },
    "polisher":  {
        "file": "qwen3-4b-instruct-2507-q8_0.gguf",
        "size": "4.0GB", "port": 8080, "mode": "router",
        "model_name": "polisher", "ctx": 4096,
        "threads": 4, "threads_batch": 4,
        "parallel": 2, "ubatch_size": 512,
    },
    "verify-enrich": {
        "file": "nextcoder-14b-q4_k_m.gguf",
        "size": "9.0GB", "port": 8083, "mode": "verify-enrich",
        "model_name": "verify-enrich", "ctx": 4096,
        "threads": 4, "threads_batch": 4,
        "cache_ram": 512, "mlock": 0,
        "parallel": 1, "ubatch_size": 512,
    },
    "proposer":   {
        "file": "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
        "size": "17GB", "port": 8081, "mode": "review-p",
        "model_name": "proposer", "ctx": 8192, "cache_ram": 1024, "mlock": 0,
        "evict_room": 18000, "memory_check": 18000, "memory_check_mode": "fatal",
        "report_memory": "1", "cache_type_k": "q8_0", "cache_type_v": "q8_0", "flash_attn": "1",
    },
    "reflector":  {
        "file": "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf",
        "size": "8.2GB", "port": 8082, "mode": "review-r",
        "model_name": "reflector", "ctx": 8192, "cache_ram": 1024,
        "evict_room": 16000, "memory_check": 16000, "memory_check_mode": "fatal",
    },
    "judge": {
        "file": "nextcoder-14b-q4_k_m.gguf",
        "size": "9.0GB", "port": 8083, "mode": "review-j",
        "model_name": "judge", "ctx": 6144, "cache_ram": 512, "mlock": 0,
        "evict_room": 16000, "memory_check": 5000, "memory_check_mode": "warn", "report_memory": "1",
        "parallel": 2, "ubatch_size": 512,
    },
    "verifier":   {
        "file": "Qwen3.6-27B.i1-IQ4_XS.gguf",
        "size": "13.7GB", "port": 8084, "mode": "verify",
        "model_name": "verifier-iq4xs", "ctx": 6144, "cache_ram": 1024, "mlock": 0,
        "evict_room": 10000, "memory_check": 8000, "memory_check_mode": "warn",
        "report_memory": "1", "cache_type_k": "q8_0", "cache_type_v": "q8_0", "flash_attn": "1",
    },
    "test-nextcoder": {
        "file": "nextcoder-14b-q4_k_m.gguf",
        "size": "9.0GB", "port": 8083, "mode": "test-q4",
        "model_name": "test-nextcoder", "ctx": 8192, "cache_ram": 512,
        "evict_room": 16000, "memory_check": 16000, "memory_check_mode": "warn",
    },
    "test-qwen": {
        "file": "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf",
        "size": "8.2GB", "port": 8083, "mode": "test-q4",
        "model_name": "test-qwen", "ctx": 8192, "cache_ram": 512,
        "evict_room": 16000, "memory_check": 16000, "memory_check_mode": "warn",
    },
    "day-extractor": {
        "file": "Qwen3-8B-Q8_0.gguf",
        "size": "8.2GB", "port": 8082, "mode": "day",
        "model_name": "day-extractor", "ctx": 8192,
        "threads": 4, "threads_batch": 4,
        "parallel": 2, "ubatch_size": 512,
        "cpus": "0-2",
        "cache_ram": 2048,
        "cache_type_k": "q8_0", "cache_type_v": "q8_0",
    },
    "day-verifier": {
        "file": "Qwen2.5-Coder-7B-Instruct-Q8_0.gguf",
        "size": "7.6GB", "port": 8082, "mode": "day",
        "model_name": "day-verifier", "ctx": 4096,
        "threads": 4, "threads_batch": 4,
        "parallel": 2, "ubatch_size": 512,
        "cpus": "0-2",
        "cache_ram": 2048,
        "cache_type_k": "q8_0", "cache_type_v": "q8_0",
    },
    "day-enricher": {
        "file": "Qwen-Qwen3.5-9B-Q8_0.gguf",
        "size": "8.9GB", "port": 8082, "mode": "day",
        "model_name": "day-enricher", "ctx": 8192,
        "threads": 4, "threads_batch": 4,
        "parallel": 2, "ubatch_size": 512,
        "cpus": "0-2",
        "cache_ram": 2048,
        "cache_type_k": "q8_0", "cache_type_v": "q8_0",
    },
}

DAY_PHASE_MODELS = {
    "day_extract": "day-extractor",
    "day_enrich": "day-enricher",
    "day_verify": "day-verifier",
}

NIGHT_MODELS = frozenset({"proposer", "reflector", "judge", "verifier"})
