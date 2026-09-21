# ADR-0007: KV Cache Memory Optimization

**Status**: Accepted  
**Date**: 2026-07-04  
**Context**: ARM 4-core 22GB system running dual 8B Q8 models

---

## Decision

Use quantized KV cache (`cache_type_k: q8_0`, `cache_type_v: q8_0`) for all day-mode inference models instead of default f16, reducing memory footprint by ~50%.

## Context

### Problem
Running dual 8B models (day-extractor + day-verifier or day-enricher) on 4-core ARM with 22GB RAM caused memory pressure:
- Model weights: ~8.2GB each (Q8_0)
- KV cache (f16 default): ~2048MB per model
- Total memory pressure: 16.4GB + 4GB cache → system instability

### Memory Analysis
KV cache memory = `2 × n_layers × d_model × max_context × precision`
- f16: 2 bytes/value
- q8_0: 1 byte/value (quantized 8-bit)
- **Reduction**: 50% memory usage with minimal quality loss for fact extraction tasks

## Implementation

### MODEL_METADATA Changes
All day-mode models now include:
```python
"cache_type_k": "q8_0",
"cache_type_v": "q8_0",
```

Applied to:
- `day-extractor` (port 8082, 1024MB cache)
- `day-extractor-b` (port 8083, 512MB cache)
- `day-verifier` (port 8081, 2048MB cache)
- `day-verifier-b` (port 8084, 2048MB cache)
- `day-enricher` (port 8082, 1024MB cache)

### Code Changes
- **e083ed3**: Added `cache_type_{k,v}` to day-extractor-b
- **672d017**: Refactored `_start_extract_b_8083()` to read KV settings from MODEL_METADATA
- **8bb02ea**: Fixed dual-mode CPU pinning to prevent core contention

### CPU Pinning Strategy
- Primary model (A): `cpus: "0-3"` (all cores, `cpu_strict=1`)
- Secondary model (B): `cpus: "2-3"` (cores 2-3, `cpu_strict=1`)
- Allows parallel execution without scheduler thrashing

## Consequences

### Positive
- **Memory savings**: ~2GB per dual-model setup (4GB total when both pairs running)
- **Stability**: Eliminates OOM events during parallel extraction/verification
- **Performance**: Negligible latency impact (<2% measured on fact extraction)
- **Scalability**: Enables 3-model scenarios (extractor + verifier + enricher) without swap

### Negative
- **Precision loss**: Theoretical quality degradation in long-context tasks (not observed in fact extraction workload)
- **Debugging**: KV cache bugs harder to diagnose with quantization

### Neutral
- llama.cpp default changed from f16 → q8_0 (upstream may follow similar pattern)
- No impact on night-mode models (single large model, no memory pressure)

## Alternatives Considered

1. **Reduce context length** → rejected (hurts section-major batching efficiency)
2. **Swap to disk** → rejected (70ms latency unacceptable for real-time pipeline)
3. **Switch to 4B models** → rejected (quality regression on complex facts)
4. **Buy more RAM** → deferred (OCI ARM instance maxed at 24GB)

## References

- Commit e083ed3: "fix: day-extractor-b KV cache type q8_0 halves memory pressure"
- Commit 672d017: "fix: _start_extract_b_8083 reads KV cache tuning from MODEL_METADATA"
- Commit 8bb02ea: "fix: dual-mode KV cache type + CPU pinning separation"
- Memory analysis: `docs/ops/baseline/D1-summary.md:58`
- MODEL_METADATA: `scripts/lib/model_registry.py`

## Measurement

Before (f16):
- Dual 8B models: 16.4GB weights + 4GB cache = 20.4GB
- Swap usage: 400-800MB under load
- OOM events: 2-3 per day

After (q8_0):
- Dual 8B models: 16.4GB weights + 2GB cache = 18.4GB
- Swap usage: <100MB steady state
- OOM events: 0 (14 days observation)

---

**Review**: This ADR captures the rationale for quantized KV cache adoption. Update if upstream llama.cpp changes default behavior or if quality regression is observed in production.
