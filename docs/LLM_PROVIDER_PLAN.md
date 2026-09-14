# Track B — LLM Provider Abstraction Plan

> Status: proposed · Date: 2026-09-14 · Owner: devforge · Related: `docs/REFACTORING_PLAN.md`, `docs/adr/0002-llm-provider-flag.md`
> Track B(클라우드 LLM 공급자) 설계·전환 계획. Track A(로컬 llama.cpp)는 이미 포트까지 구현됨.

---

## 1. 목적

Track A는 `LLMPort` 인터페이스와 `LocalLLMAdapter`만 구현한다. Track B는 **코드 수정 없이**
OpenAI/Anthropic 등 클라우드 공급자로 라우팅할 수 있게 한다.

## 2. 현재 상태 (Track A)

| 항목 | 구현 |
|---|---|
| 포트 | `src/devforge/ports/extract.py::LLMPort` (`chat`, `verify_claim`, `enrich_fact`, `rerank`) |
| 로컬 어댑터 | `src/devforge/adapters/driven/llm/local_adapter.py::LocalLLMAdapter` |
| 모델 레지스트리 | `local_adapter.py::MODEL_REGISTRY` (포트 8080-8085 하드코딩) |
| 공급자 선택 | `ConfigRegistry.llm_provider` (env `DEVFORGE_LLM_PROVIDER`, 기본 `local`) |
| 팩토리 | 미구현 (Track B에서 도입) |

## 3. 설계

```
LLMPort (ports/extract.py)
├── LocalLLMAdapter      (현행, Track A)
├── OpenAILLMAdapter     (Track B)
└── AnthropicLLMAdapter  (Track B)
        ▲
        └── create_llm_provider(name, ModelProvidersConfig)   # factory
                    ▲
ConfigRegistry.llm_provider = DEVFORGE_LLM_PROVIDER (local|openai|anthropic)
```

- **설정 파일**: `config/providers.yaml` (선택). 없으면 `local` 기본.
  ```yaml
  default_provider: local
  providers:
    openai:
      type: openai
      api_key: ${OPENAI_API_KEY}     # secrets.env
      base_url: https://api.openai.com/v1
      default_models:
        day_extract: gpt-4.1-mini
    anthropic:
      type: anthropic
      api_key: ${ANTHROPIC_API_KEY}
      default_models:
        day_extract: claude-sonnet-4-6
  ```
- **DI**: `create_llm_provider()`가 만든 구현을 `ExtractPipeline(llm=...)`에 주입한다.
  `core`는 `adapters`를 import하지 않는다(순환 방지) — 선택은 factory가 담당.

## 4. 전환 단계

1. `adapters/driven/llm/openai_adapter.py`, `anthropic_adapter.py` 구현 (`LLMPort` 준수).
2. `adapters/driven/llm/factory.py::create_llm_provider()` 추가.
3. `ModelProvidersConfig.resolve_provider_name()`을 `providers.yaml` 매핑 조회로 확장.
4. `MODEL_REGISTRY` 하드코딩 포트를 `providers.yaml` 모델 매핑으로 대체(로컬은 유지).
5. 회귀: replay fixture + 특성화 테스트로 Track A 동작 불변 확인.
6. e2e: `DEVFORGE_LLM_PROVIDER=openai devforge pipeline orchestrate --dry-run`.

## 5. 리스크

| 리스크 | 완화 |
|---|---|
| 공급자별 프롬프트/JSON 출력 차이 | 포트 계약 유지, replay fixture로 파서 검증 |
| API 키 관리 (OpenAI ≠ Anthropic 별도 키) | `secrets.env` + 기존 key rotator 재사용; 단일 키는 OpenRouter/LiteLLM 게이트웨이로만 가능 |
| 비용/레이트리밋 | 예산 게이트(BudgetManager) + `or_rate_limiter` 프록시 |
| 순환 참조 | factory를 adapters 계층에 둔다 (core→adapters 금지) |

## 6. 수락 기준

- `DEVFORGE_LLM_PROVIDER` 값만 바꿔 동일 파이프라인이 다른 공급자로 동작한다.
- Track A(`local`) 회귀 0 (특성화 테스트 5종 + replay 통과).
- `lint-imports`, `mypy --strict`, `ruff` CI green 유지.
