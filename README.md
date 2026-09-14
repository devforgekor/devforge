# DevForge

AI agent workflow pipeline server — src-layout Python package
(`pip install -e .`), single CLI entry point (`devforge`), built around
Ports & Adapters (hexagonal) and DDD bounded contexts.

## Install

```bash
pip install -e ".[dev]"
```

## CLI

```bash
devforge --help
devforge status [--json]
devforge pipeline orchestrate [--limit 50] [--turn-id UUID] [--dry-run]
devforge pipeline status
devforge mcp serve [--host 0.0.0.0] [--port 8100]
devforge inference switch day|night
devforge inference status
devforge inference ensure day_extract
```

HTTP API: `uvicorn devforge.adapters.driving.api.app:app --host 0.0.0.0 --port 8000`

## Layout

```
src/devforge/
├── cli.py          # Typer entry point
├── core/           # config (ConfigRegistry), logging
├── ports/          # Protocol interfaces (LLMPort, ExtractPort, ...)
├── domain/         # SQLAlchemy models + bounded contexts
├── adapters/
│   ├── driven/     # LLM (local llama.cpp), storage (PostgreSQL)
│   └── driving/    # api (FastAPI), mcp (SSE), cli_cmds
├── application/    # ExtractPipeline
└── pipeline_stages/# stage execution
```

## Docs

- [`docs/REFACTORING_PLAN.md`](docs/REFACTORING_PLAN.md) — migration plan (canonical)
- [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md) — CLI + HTTP + Python API
- [`docs/OPERATIONS_GUIDE.md`](docs/OPERATIONS_GUIDE.md) — install/run/migrations
- [`docs/system-architecture.md`](docs/system-architecture.md) — full system structure
- [`docs/adr/`](docs/adr/) — architecture decision records

## Test

```bash
pytest tests/test_characterization.py tests/test_integration.py -v
```
