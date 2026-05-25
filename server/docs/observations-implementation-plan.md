# Observations Table Implementation Plan

**Status**: Pending approval  
**Written**: 2026-05-17  
**Related**: Qwen worker observations storage disconnect resolution

## Background

- Qwen worker runs every 15 minutes, generating `observations` (facts/anomaly detection), but they are lost after `print()` log output
- `obs_dec` table stores only approved decisions (MCP `mem_save` → `meta.type=="decision"`)
- Qwen observations (evidence) and agent decisions (judgment) must be stored separately

## Change Scope

### P0 — Implementation (5 files)

| # | File | Change |
|---|------|-----------|
| 1 | `api/db.py` | Add observations table to SCHEMA_SQL |
| 2 | `docs/schema.sql` | Document observations schema |
| 3 | `scripts/lib/qwen_executor.py` | Add `execute_observations()` function |
| 4 | `scripts/qwen_worker.py` | Replace `print()` → `execute_observations()` call |
| 5 | `scripts/gen_motd_task.py` | Query observations total count, display in MOTD-Task |

### P1 — Follow-up (7 files)

| # | File | Change |
|---|------|-----------|
| 6 | `api/stats.py` | Add total_observations to GET /stats |
| 7 | `scripts/session_start.py` | Include recent observations in SessionStart context |
| 8 | `scripts/gen_server_state.py` | Add observations field to changelog entries |
| 9 | `scripts/update_handover.py` | Preserve observations key |
| 10 | `scripts/cli.py` | Add `observation recent` subcommand |
| 11 | `api/mcp_server.py` | Add `obs_recent` MCP tool |
| 12 | `docs/design.md` | Document observations table |

### System Files — No Changes

- `devforge-qwen-worker.timer` — Reuse existing 15-minute interval
- `motd-gen.timer` — gen_motd_task.py includes observations, so automatically reflected

## DB Schema

```sql
CREATE TABLE IF NOT EXISTS observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    observation TEXT NOT NULL CHECK (length(trim(observation)) > 0),
    category TEXT NOT NULL DEFAULT 'general',
    source TEXT NOT NULL DEFAULT 'qwen_worker',
    context JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_observations_created ON observations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_category ON observations(category);
```

- Fully separated from `obs_dec` table (no FK, no turn_id required)
- `context` JSONB: Related metadata (orphan_agents, turn_count, etc.)
- `category`: `orphan_turns`, `orphan_worklogs`, `no_match`, `anomaly`, etc.

## Core Implementation Details

### `execute_observations()` — qwen_executor.py

```python
def execute_observations(observations: list, category: str = "general",
                         context: dict = None) -> int:
    if not observations:
        return 0
    
    # Guard: create table if nonexistent
    _psql("""
        CREATE TABLE IF NOT EXISTS observations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            observation TEXT NOT NULL CHECK (length(trim(observation)) > 0),
            category TEXT NOT NULL DEFAULT 'general',
            source TEXT NOT NULL DEFAULT 'qwen_worker',
            context JSONB DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    
    ctx_json = json.dumps(context or {}, ensure_ascii=False)
    count = 0
    for obs in observations:
        safe = obs.replace("'", "''")  # SQL injection prevention
        _psql(
            f"INSERT INTO observations (observation, category, source, context) "
            f"VALUES ('{safe}', '{category}', 'qwen_worker', '{ctx_json}'::jsonb)"
        )
        count += 1
    
    print(f"  Observations saved: {count}")
    return count
```

### Banner Display — gen_motd_task.py

```
DevForge | Containers 5/5 | Services 6 | CPU 0.3/0.6/0.6 | Mem 12Gi/22Gi (56%) | Obs 15
  Tasks      Claude  3/15     Copilot 11/15    Gemini  1/15     Qwen    0/15   
  Decisions  Claude  0/120    Copilot 0/172    Gemini  0/1      Qwen    0/0    
```

- `Obs 15`: Yesterday's observations total (added to health bar)
- `obs_dec` rows: Maintained as before (per-agent, based on obs_dec table)
- When nightly status or above, observations count also gets nightly coloring

## Execution Order

1. **Schema**: Modify `api/db.py` + `docs/schema.sql`
2. **Storage logic**: Add `execute_observations()` to `qwen_executor.py`
3. **Call site**: Replace `qwen_worker.py` L362-364
4. **Banner**: Add observations query + display to `gen_motd_task.py`
5. **Verification**: Test `python3 scripts/gen_motd_task.py --stdout`
6. **DB Migration**: Create table via API restart or manually
7. **Cache refresh**: Run `python3 scripts/gen_motd_task.py`

## Verification Items

- [ ] Confirm `SELECT COUNT(*) FROM observations` increases after Qwen worker execution
- [ ] Confirm observations with single quotes insert correctly
- [ ] Confirm observations total displayed in banner
- [ ] Confirm nightly coloring applies to observations as well
