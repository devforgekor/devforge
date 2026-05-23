# DevForge Server — Design

**Status**: Phase 1 complete (2026-05-14)
**Server**: OCI ARM (24GB), Podman/Quadlet, PostgreSQL 16

## Core Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Podman + Quadlet (user systemd) | rootless, docker-compatible, declarative container management |
| 2 | Pinned image tags (`:2026.05.14`) | reproducible deployments, easy rollback |
| 3 | PostgreSQL 16 (data-pod) + devforge_app | pg_trgm search, JSONB meta, single DB |
| 4 | MCP SSE (remote) | Claude Code Mac ↔ server integration. stdio impossible (remote) |
| 5 | Single secrets injection (EnvironmentFile) | one secrets.env for all container secrets |
| 6 | AI inference handled by Mac | server does record/search only. LLM serves local Qwen only |
| 7 | Caddy (root, host network) | Auto-HTTPS, single reverse proxy entry point |

## Infrastructure Layout

```
ai-pod (10.89.0.8, 16GB)     data-pod (10.89.0.9, 2GB)
├─ litellm :4000             └─ postgres :5432
├─ devforge-llm :8080           └─ /mnt/lv_db (30GB LVM)
└─ devforge-api :8000
   ├─ MCP SSE
   ├─ POST /ingest
   └─ GET /stats

Caddy (host network)
├─ /api/* → litellm:4000
├─ /devforge/* → localhost:8000
└─ netdata.* → localhost:19999
```

## Network

- `devforge-net`: Podman bridge, 10.89.0.0/24, DNS enabled
- ai-pod: ports 4000, 8000 → 127.0.0.1 only
- data-pod: no published ports (pod-internal communication)
- Caddy: host network, ports 80/443

## Access Restrictions
- `/opt/workspace/` — off-limits without explicit user instruction (contains Seedling and other projects)

## Application Structure

```
/opt/projects/server/
├── Dockerfile
├── requirements.txt
├── healthcheck.py
├── api/
│   ├── mcp_server.py       ← MCP SSE server
│   ├── ingest.py           ← POST /ingest
│   ├── search.py           ← search + save logic
│   ├── db.py               ← PostgreSQL pool + init
│   ├── stats.py            ← GET /stats (7-section dashboard)
│   └── __init__.py
├── scripts/
│   └── cli.py              ← CLI (search/save/recent/worklog add/recent/search)
└── docs/
    └── schema.sql          ← DB schema canonical source
```

## MCP Tools

| Tool | Input | Behavior |
|------|-------|----------|
| `mem_save` | tag, summary, detail | stores conversations + turns |
| `mem_search` | query, tag (optional) | ILIKE search, JSON response |

## CLI

```bash
# Conversation search/save
python3 /opt/projects/server/scripts/cli.py search "<query>"
python3 /opt/projects/server/scripts/cli.py save --source claude
python3 /opt/projects/server/scripts/cli.py recent

# Worklog
python3 /opt/projects/server/scripts/cli.py worklog add "<title>" "<summary>" --tags "A,B"
python3 /opt/projects/server/scripts/cli.py worklog recent --limit 5
python3 /opt/projects/server/scripts/cli.py worklog search "<keyword>" --tag "A"
```

## DB Schema

Canonical source: `schema.sql` (inside app container)
Target DB: `devforge_app` (PostgreSQL 16)

Tables: `conversations`, `turns`, `obs_dec`, `mcp_dec`, `observations`, `worklog_entries`
Indexes: pg_trgm (search), GIN (tags, meta), UNIQUE (date, title), UNIQUE partial (status='in_progress', dormant)
worklog_entries columns: status (dormant, always 'done'), kind (dormant, always 'task'), agent (AI tool name), model (model name), turn_ids (linked turn UUID array)

## Backup

- `dump_postgres.sh`: daily pg_dump (devforge_app), 7-day retention
- `test_dump_restore.sh`: monthly restore validation
- `nightly_batch.sh`: daily batch pipeline (03:00 UTC) — light jobs first (retry 3x) → heavy jobs after
- Target: `/mnt/secure_meta/snapshots/`

## External References

- **totem** (mmnto-ai/totem, Apache-2.0) — primary reference. lesson → rule auto-conversion, doctor self-correction loop. Foundation for golden_diffs auto-collection and quality monitoring design.
- diff0, Repeton — idea extraction only when needed during implementation (multi-provider validation, rollback loops).

## Related Documents

- Status/plan: `docs/phases.md`
- Task tracking: `docs/tasks.yaml` (todo / in_progress / blocked / done, Kanban labels: To Do / In Progress / Blocked / Done)
- Work history: `devforge_app.worklog_entries` (PostgreSQL, queried via cli.py worklog)
- Operations config: `/opt/projects/server/CLAUDE.yaml`, `handover.yaml`, `blueprint.yaml`
