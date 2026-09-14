# DevForge API Reference

> Status: active · Date: 2026-09-14 · Owner: devforge · Related: `docs/OPERATIONS_GUIDE.md`, `docs/ARCHITECTURE.md`
> 리팩토링 패키지(`src/devforge`) 기준. 포트/서버 구현은 레거시 라이브 런타임과 다르다 — `system-architecture.md` §2.

## CLI

```
devforge --help
```

### Subcommands

| Command | Description | Examples |
|---------|-------------|----------|
| `devforge status` | Show server status | `devforge status`, `devforge status --json` |
| `devforge pipeline orchestrate` | Run extract pipeline | `devforge pipeline orchestrate --limit 50`, `--turn-id UUID`, `--dry-run` |
| `devforge pipeline status` | Show pipeline state distribution | `devforge pipeline status` |
| `devforge mcp serve` | Start MCP SSE server | `devforge mcp serve --port 8100` |
| `devforge inference switch` | Switch day/night mode | `devforge inference switch day` |
| `devforge inference status` | Show inference config | `devforge inference status` |
| `devforge inference ensure` | Check model readiness | `devforge inference ensure day_extract` |

## HTTP API (FastAPI)

Base URL: `http://localhost:8000`  <!-- 리팩토링 기본값. 레거시 라이브 hub는 :8002 -->

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/api/v1/config` | Non-sensitive config info |
| GET | `/api/v1/turns/{turn_id}` | Get turn by UUID |
| GET | `/api/v1/search?q=...&limit=...&state=...` | Full-text search turns |
| POST | `/api/v1/observations` | Save worker observation |
| POST | `/api/v1/pipeline/extract` | Trigger extract pipeline |

### MCP SSE Server (port 8100)

> 패키지 구현은 자체 SSE 서버(`devforge.adapters.driving.mcp.server`, `devforge mcp serve`).
> 레거시 라이브 MCP는 FastMCP Streamable HTTP(`scripts/mcp_server.py`, :8000)다.

Base URL: `http://localhost:8100`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/sse` | SSE endpoint for tool discovery |
| POST | `/tools/{tool_name}` | Call MCP tool |

Available tools: `knowledge_search`, `pipeline_status`, `extract_turn`, `deepdive_step`, `store_observation`

## Python API

### Config

```python
from devforge.core.config import get_config

config = get_config()
config.db_url          # PostgreSQL connection string
config.system_mode     # "day" or "night"
config.model_name      # e.g. "day-extractor"
config.model_port      # e.g. 8082
config.paths.data_dir  # Path to /opt/ai_data
```

### Pipeline

```python
from devforge.application.extract_pipeline import ExtractPipeline
from devforge.adapters.driven.llm.local_adapter import LocalLLMAdapter
from devforge.adapters.driven.storage.extract_adapter import (
    PostgresExtractAdapter, PostgresTurnRepository
)

pipeline = ExtractPipeline(
    llm=LocalLLMAdapter(),
    db=PostgresExtractAdapter.from_config(config),
    turn_repo=PostgresTurnRepository.from_config(config),
)
result = await pipeline.run_single(turn_id)
```

### Database

```python
from devforge.adapters.driven.storage.database_gateway import DatabaseGateway

gateway = DatabaseGateway.from_config(config)
async with gateway.session() as db:
    result = await db.execute(select(Turn).where(Turn.id == turn_id))
```
