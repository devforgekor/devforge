# Secret Injection Hardening

**Status:** Planned (Stage 3)
**Origin:** refactoring-roadmap.md §5.1 (archived)
**Trigger:** WebObsidian EnvironmentFile quoting bug (2026-09-19)
**Updated:** 2026-09-21
**Guide:** docs/runbooks/option-2-secret-hardening.md

---

## Problem Statement

**Current state (Stage 2 — minimal injection, DONE):**

- KV secrets -> `scripts/deploy/kv-fetch-env.py` -> tmpfs EnvironmentFile -> process env
- tmpfs prevents plaintext persistence on disk, and each service requests only the
  keys it needs (no more "all 109 secrets" leakage).
- **Remaining problem:** secrets still land in the process environment, so they are
  readable via `ps e`, `/proc/<pid>/environ`, and `podman exec <c> env`.

**Stage 2 rollout evidence (2026-09-21):** cashbook migrated to KV
(`CASHBOOK-API-KEY` in Azure Key Vault, loaded through the `kv-fetch-env.py`
wrapper). This is Stage 2, **not** Stage 3 — the secret is still exported into the
environment.

---

## Solution: LoadCredential / podman --secret (Stage 3)

### Goal

| Item | Current (EnvironmentFile) | Target (LoadCredential) |
|------|---------------------------|-------------------------|
| Storage | tmpfs -> env vars | tmpfs -> credential file -> app reads file |
| `/proc/<pid>/environ` | secret visible | secret NOT visible |
| Access | any process via /proc | only systemd + the service process |
| Reload | restart | restart |

### Approach A — systemd `LoadCredential=` (recommended)

For user services (uid 1000): `cashbook`, `fastapi`, `mcp`.

```ini
[Service]
ExecStartPre=/opt/projects/server/scripts/deploy/kv-to-credential.sh CASHBOOK-API-KEY /run/user/1000/credentials/cashbook_key
LoadCredential=cashbook_key:/run/user/1000/credentials/cashbook_key
ExecStart=/usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8100
# App reads from $CREDENTIALS_DIRECTORY/cashbook_key
```

The app must read the file; simply re-exporting it in `ExecStart` reintroduces the
env-var exposure (see option-2 guide §1.3).

```python
# cashbook/main.py
import os
from pathlib import Path


def load_api_key() -> str:
    creds_dir = os.getenv("CREDENTIALS_DIRECTORY")
    if creds_dir:
        key_file = Path(creds_dir) / "cashbook_key"
        if key_file.exists():
            return key_file.read_text().strip()
    return os.getenv("CASHBOOK_API_KEY", "")
```

### Approach B — podman `--secret`

For Quadlet containers (`postgres`), which natively support `POSTGRES_PASSWORD_FILE`.

```ini
# containers/devforge-postgres.container
[Service]
ExecStartPre=/opt/projects/server/scripts/deploy/kv-to-credential.sh POSTGRES-PASSWORD /run/user/1000/credentials/postgres_pw
Environment=POSTGRES_PASSWORD_FILE=/run/credentials/postgres_pw
```

---

## Migration Sequence

| Service | Sensitive key | Code change | Priority | Stage 3 status |
|---------|---------------|-------------|----------|----------------|
| cashbook | CASHBOOK_API_KEY | yes (main.py) | P3 | planned |
| postgres | POSTGRES_PASSWORD | no (native `_FILE`) | P1 | planned |
| webobsidian | master password | yes (config load) | P1 | planned |
| fastapi | multiple (10+) | yes (core/config.py) | P2 | planned |
| mcp | DB credentials | yes (connection string) | P2 | planned |

Recommended order: postgres (native) -> webobsidian (single key) -> fastapi/mcp (many keys).

---

## Rollback Plan

```bash
# 1. Restore the previous unit definition
git checkout HEAD~1 ~/.config/systemd/user/<service>.service
# 2. Reload and restart
systemctl --user daemon-reload
systemctl --user restart <service>.service
```

---

## Verification

```bash
PID=$(systemctl --user show -p MainPID --value cashbook.service)

# Before (Stage 2): secret visible
sudo cat /proc/$PID/environ | tr '\0' '\n' | grep CASHBOOK_API_KEY
# After  (Stage 3): empty output expected

# Credential is wired (link into the runtime credentials dir)
sudo ls -l /proc/$PID/fd/ | grep credentials
```

---

## References

- systemd.exec(5): `LoadCredential=`
- Podman secrets: https://docs.podman.io/en/latest/markdown/podman-secret.1.html
- docs/runbooks/option-2-secret-hardening.md
- refactoring-roadmap.md §5.1 (archived)

---

**Next actions:**

1. Implement `scripts/deploy/kv-to-credential.sh`.
2. Pilot with `postgres` (native `_FILE` support, no app change).
3. Verify no secret in `/proc/<pid>/environ`; then roll out to webobsidian/fastapi/mcp.
