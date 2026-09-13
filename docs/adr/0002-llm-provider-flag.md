# LLM Provider Flag Decision Record (ADR-0002)

## Status
Accepted (Phase 1)

## Context
Track A requires local LLM provider (llama.cpp HTTP) with port-based routing.
Track B (OpenAI/Anthropic) is planned but out of scope for v1.4.

`MODEL_REGISTRY` in legacy code hardcodes ports 8080-8084, making provider
switching impossible without code changes.

## Decision
1. Define `LLMPort` Protocol in `ports/extract.py` with `chat()` method
2. Implement `LocalLLMAdapter` in `adapters/driven/llm/local_adapter.py` (Track A)
3. Use `DEVFORGE_LLM_PROVIDER` env var for future provider selection
4. Track B implementation deferred to `docs/LLM_PROVIDER_PLAN.md`

```python
class LLMPort(Protocol):
    async def chat(self, messages: list[dict], model_key: str, **kwargs) -> dict: ...
```

## Consequences
- Track A can switch ports/models without code changes
- Track B can be added by implementing `LLMPort` and updating `ModelProvidersConfig`
- No circular dependency: `core` never imports `adapters`
