# DevForge — Server Identity
<!-- auto-generated from collect_structural() + CLAUDE.yaml at 2026-06-14 09:00 KST -->

## Overview
- Host: DEVFORGE (ARM Neoverse-N1, 4-core, 22Gi + 4G zram (89.8M) + 12G swap (swappiness=10))
- OS: Oracle Linux Server 9.7 (aarch64)
- Runtime: Podman (rootless, user opc)
- Database: PostgreSQL 16 (pg_trgm, JSONB)
- Proxy: Caddy (host network, auto-HTTPS)
- Claude Code: DeepSeek v4-Flash (via Anthropic API compat at api.deepseek.com)
- Aider: DeepSeek v4-flash (via OpenAI API compat at api.deepseek.com)
- Resource usage: see `state.yaml#metrics` for live numbers

## Infrastructure
```
data-pod (2GB)
└─ postgres :5432

Caddy (host network)
├─ /devforge/* → turn_watcher.py (via DB)
└─ netdata → localhost:19999
```

## Health Checks
- postgres (host): `pg_isready -h localhost -p 5432` (localhost only works if port is published; postgres binds to devforge-net, not host)
- postgres (always works): `podman exec postgres psql -U devforge -d devforge_app -c "SELECT 1"`
- **Rule**: Always use `podman exec postgres psql` for DB queries. `psql -h localhost` will fail because PostgreSQL runs in a podman container without host port publishing.

## Network
- Bridge: devforge-net (10.89.0.0/24)
- SSH: 22/tcp (key-only)
- Internal APIs: 127.0.0.1 only

## Storage
- `/opt/ai_data` (100G) — ai_data
- `/mnt/lv_db` (30G) — db
- `/opt/projects` (10G) — projects
- `SWAP` (4G) — swap
- `/var/log` (10G) — logs
- `/mnt/secure_meta` (4.5G) — meta
- `/var/tmp` (10G) — tmp
- `/` (20G) — root
- `/opt/workspace` (6GB) — out of scope (consolidated into `/opt/projects/server/`)

## Key Services
| Service | Type | Status |
|---------|------|--------|
| devforge-pod-a | systemd user | inactive |
| devforge-swap | systemd user | active |
| postgres | systemd user | active |
| 15m cycle | systemd user | inactive |
| backup | systemd user | inactive |
| classify | systemd user | inactive |
| daily structure | systemd user | activating |
| night cycle | systemd user | inactive |
| refresh reminder | systemd user | inactive |
| restore test | systemd user | inactive |
| caddy | systemd system (rootful podman) | active |
| netdata | systemd system (native) | active |

## Logs
- Services: `journalctl --user -u <service-name>`
- Caddy: `journalctl -u caddy`

## Entry Points
- `cli.py status --json` — live state (containers, models, timers, services, resources, tasks, alerts)
- `/opt/projects/server/docs/` — design, phases, glossary, specs
- `devforge_app.worklog_entries` — work log (DB)

### Auto-Generated Docs (live data, no manual edit)
- `docs/architecture/infrastructure.md` — server identity (this doc)
- `docs/architecture/software.yaml` — models, modes, pipelines (live)
- `docs/architecture/code-structure.yaml` — file layout SSOT (script scan)
- `docs/specs/timer-registry.yaml` — systemd timer schedule (live)
