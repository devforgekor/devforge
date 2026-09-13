# DevForge Operations Guide

## Quick Start

### Installation
```bash
pip install -e ".[dev]"
```

### Running the Server
```bash
# API server (port 8000)
devforge mcp serve

# Or via uvicorn directly
uvicorn devforge.adapters.driving.api.app:app --host 0.0.0.0 --port 8000

# MCP server (port 8100)
devforge mcp serve --port 8100
```

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

# LLM
export DEVFORGE_LLM_PROVIDER=local
export DEVFORGE_LLM_REPLAY=1  # For testing with fixtures

# Mode
export DEVFORGE_SYSTEM_MODE=day  # or night
```

## Database

### Alembic Migrations
```bash
# Check current version
alembic current

# Upgrade to latest
alembic upgrade head

# Downgrade (rollback)
alembic downgrade -1
```

### Schema
- 16 tables defined in `docs/specs/schema.sql`
- Migrations in `alembic/versions/`
- Source of truth: `docs/specs/schema.sql`

## Testing

### Run All Tests
```bash
pytest tests/test_characterization.py tests/test_integration.py -v
```

### Test Categories
- **Characterization** (18 tests): Verify existing behavior preserved
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
```

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
