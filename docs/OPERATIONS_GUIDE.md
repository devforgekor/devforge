# DevForge Operations Guide

> Status: active · Date: 2026-09-14 · Owner: devforge · Related: `docs/API_REFERENCE.md`, `docs/MIGRATION_GUIDE.md`

## Quick Start

### Installation
```bash
pip install -e ".[dev]"
```

### Running the Server
```bash
# HTTP API server (FastAPI, port 8000)
uvicorn devforge.adapters.driving.api.app:app --host 0.0.0.0 --port 8000

# MCP SSE server (port 8100)
devforge mcp serve --host 0.0.0.0 --port 8100
```

> 포트는 **리팩토링 패키지 기본값**이다. 현행 라이브 런타임은 FastAPI hub `:8002`,
> MCP `:8000`(리팩터드 `devforge.adapters.driving.mcp.server`)이다. `scripts/mcp_server.py`(FastMCP)는 비활성 레거시 — `system-architecture.md` §2 참조.

### Running the Pipeline
```bash
# Dry run (no DB writes)
devforge pipeline orchestrate --dry-run --limit 10

# Full run
devforge pipeline orchestrate --limit 100

# Single turn
devforge pipeline orchestrate --turn-id <UUID>
```

### Checking Status
```bash
devforge status --json          # Full system status
devforge pipeline status         # Pipeline state distribution
devforge inference status        # Model config
devforge inference ensure day_extract  # Check model readiness
```

## Configuration

### Config Sources (priority order)
1. Environment variables (`DEVFORGE_*`)
2. `~/.config/devforge/secrets.env`
3. `config/providers.yaml`
4. `/opt/ai_data/scripts/current-*.env`
5. `data/state.yaml`
6. Built-in defaults

### Key Settings
```bash
# Database
export DEVFORGE_DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/dbname"
```
> **컨테이너 (devforge-mcp / devforge-fastapi)**: `DEVFORGE_DATABASE_URL`은
> `~/.config/containers/systemd/container-devforge-{mcp,fastapi}.container`의
> `Environment=` 로 주입한다 (Azure KV의 `DEVFORGE-DATABASE-URL`은 2026-09-19 현재 미존재).
> 변경 후: `systemctl --user daemon-reload && systemctl --user restart container-devforge-{mcp,fastapi}`

# LLM
export DEVFORGE_LLM_PROVIDER=local
export DEVFORGE_LLM_REPLAY=1  # For testing with fixtures

# Mode
export DEVFORGE_SYSTEM_MODE=day  # or night
```

## Database

### Alembic Migrations
```bash
# Run where the DB is reachable (e.g. inside the svc pod / container)
alembic current
alembic upgrade head
alembic downgrade -1
alembic revision --autogenerate -m "description"   # review before applying
```
> Live DB baselined 2026-09-14 via `alembic stamp head`; ORM reconciled to live so
> `alembic check` is clean. Autogenerate is additive-only (no destructive drops) and
> needs review. See `docs/adr/0004-alembic-migrate.md`.

### Schema
- 16 app tables defined in `docs/specs/schema.sql`
- Migrations in `alembic/versions/`
- ORM (app schema): `src/devforge/domain/models.py`

## Testing

### Run All Tests
```bash
pytest tests/test_characterization.py tests/test_integration.py -v
```

### Test Categories
- **Characterization** (31 tests): Verify existing behavior preserved
- **Integration** (12 tests): Pipeline + DB integration

### Replay Mode (for deterministic LLM testing)
```bash
DEVFORGE_LLM_REPLAY=1 pytest tests/test_integration.py -v
```

## Containerization

### Build
```bash
docker build -t devforge:latest .
```

### Run
```bash
docker run -p 8000:8000 -p 8100:8100 \
  -v ~/.config/devforge:/config:ro \
  -v /opt/ai_data:/data \
  devforge:latest
```

## Troubleshooting

### "Cannot connect to database"
```bash
# Check PostgreSQL
pg_isready -h localhost -p 5432

# Check config
devforge status --json | jq '.db_url'
```

### "Model not responding on port"
```bash
# Check model pod
devforge inference ensure day_extract

# Check if port is listening
ss -tlnp | grep 8082

# Check MODEL_METADATA configuration
devforge status --json | jq '.models'
```

**최근 변경사항 (2026-07-04)**:
- KV cache quantization (`cache_type_k/v: q8_0`) 적용으로 메모리 사용량 50% 감소
- 모든 day-mode 모델에 적용 (extractor, verifier, enricher)
- MODEL_METADATA가 KV cache 설정의 SSOT (Single Source of Truth)
- 자세한 내용: `docs/adr/0007-kv-cache-optimization.md`

### "Pipeline stuck in 'extracting' state"
```bash
# Check pipeline status
devforge pipeline status --json

# Reset stuck turn (direct SQL)
psql $DEVFORGE_DATABASE_URL -c \
  "UPDATE turns SET pipeline_state='scanned' WHERE pipeline_state='extracting' AND id != NOW() - INTERVAL '30 minutes';"
```

### "import lib" ImportError
This is expected — the legacy `scripts/lib/` is NOT importable. All functionality has been migrated to the `devforge` package.
