# Config Priority Decision Record (ADR-0001)

## Status
Accepted (Phase 0)

## Context
DevForge has 5 configuration sources scattered across different locations:
1. `/home/opc/.config/devforge/secrets.env` — API keys, DB credentials
2. `/opt/projects/server/config/providers.yaml` — LLM provider mappings
3. `/opt/ai_data/scripts/current-mode-inference.env` — runtime inference config
4. `/opt/ai_data/scripts/current-system-mode.env` — system mode (day/night)
5. `/opt/projects/server/data/state.yaml` — state + CLAUDE.yaml override

Previous approach: each consumer imported config files independently, leading to duplicated logic and inconsistent priority.

## Decision
Use `ConfigRegistry` pattern combining all 5 sources via Pydantic Settings with explicit priority:

```
env vars > secrets.env > providers.yaml > current-*.env > state.yaml > defaults
```

Implemented in `src/devforge/core/config.py`.

## Consequences
- Single `get_config()` entry point
- Hardcoded paths abstracted via `ConfigRegistry.paths`
- `HardcodedPathResolver` maps 253 hardcoded paths to config values
- Secrets never exposed via API endpoints
