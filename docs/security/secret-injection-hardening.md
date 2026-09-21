# Secret Injection Hardening

**Status:** Stage 3 in progress — cashbook pilot COMPLETE (2026-09-21)
**Origin:** refactoring-roadmap.md §5.1 (archived)
**Trigger:** WebObsidian EnvironmentFile quoting bug (2026-09-19)
**Updated:** 2026-09-21
**Guide:** docs/runbooks/option-2-secret-hardening.md

---

## Problem Statement

**Stage 2 (minimal injection, previously deployed):**

- KV secrets -> `scripts/deploy/kv-fetch-env.py` -> process environment.
- Secrets are still readable from `/proc/<pid>/environ`, `ps e`, and
  `podman exec <c> env`.
- Measured 2026-09-21 (cashbook, before Stage 3): the process had **121 env
  vars including every KV secret** (`CASHBOOK_API_KEY`, `GUDOKPIN_API_KEY`,
  `SLACK_BOT_TOKEN_KEY`, `TELEGRAM_TOKEN_KEY`, ...). The wrapper was called
  without `--keys`, so it injected the whole vault — both an exposure and a
  least-privilege violation.

---

## Solution: secret file (not environment variable)

The correct pattern for systemd user services here is:

1. `ExecStartPre` fetches **one** secret with `kv-fetch-env.py env --keys` and
   writes it to a mode-600 file under `/run/user/1000/<svc>/`.
2. The unit sets an env var holding the **path** (not the value).
3. The app reads the file at startup; the value never enters the environment.

### CRITICAL — do NOT use `LoadCredential=` with `ExecStartPre`

The option-2 guide originally proposed `ExecStartPre=` + `LoadCredential=`.
That does **not** work: systemd resolves `LoadCredential=` **before**
`ExecStartPre=` runs, so the source file does not exist yet and the unit fails
with:

```
Failed at step CREDENTIALS spawning ...: No such file or directory
(status=243/CREDENTIALS)
```

Verified 2026-09-21 on systemd 252. If true `LoadCredential=` isolation is
wanted, the secret must be produced by a **separate oneshot unit ordered
`Before=` the service** (so it exists at start), not by `ExecStartPre`.

---

## cashbook — DONE (pilot, 2026-09-21)

`~/.config/systemd/user/cashbook.service`:

```ini
[Service]
Type=simple
WorkingDirectory=/opt/projects/server/cashbook
Environment=CASHBOOK_CREDENTIAL_FILE=/run/user/1000/cashbook/cashbook_key
ExecStartPre=/opt/projects/server/scripts/deploy/kv-to-credential.sh CASHBOOK-API-KEY /run/user/1000/cashbook/cashbook_key
ExecStart=/usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8100
ExecStopPost=/bin/rm -f /run/user/1000/cashbook/cashbook_key
Restart=always
RestartSec=5
```

`cashbook/main.py`: `_load_api_key()` reads `CASHBOOK_CREDENTIAL_FILE` first,
falling back to `CASHBOOK_API_KEY` (env) for compatibility.

**Verified:**

| Check | Before | After |
|-------|--------|-------|
| env vars | 121 | 13 |
| KV secret keys in env | all | 0 |
| credential file | none | `/run/user/1000/cashbook/cashbook_key` (600) |
| API `?key=<correct>` | 200 | 200 |
| API `?key=<wrong>` | 200 | 401 |
| survives restart | - | yes (file regenerated) |

---

## Migration Sequence (remaining)

| Service | Sensitive key | Code change | Priority | Status |
|---------|---------------|-------------|----------|--------|
| cashbook | CASHBOOK_API_KEY | yes | P3 | DONE |
| postgres | POSTGRES_PASSWORD | no (native `_FILE`) | P1 | planned |
| webobsidian | master password | yes | P1 | planned |
| fastapi | multiple (10+) | yes | P2 | planned |
| mcp | DB credentials | yes | P2 | planned |

**postgres note:** devforge-postgres is a custom image; confirm it honors
`POSTGRES_PASSWORD_FILE` before switching. Because the DB backs every service,
stage separately and verify `pg_isready` + app connectivity before removing the
env password.

---

## Rollback

```bash
# cashbook
cp /tmp/opencode/cashbook-backup/cashbook.service.bak ~/.config/systemd/user/cashbook.service
git checkout HEAD -- cashbook/main.py   # if main.py change must be reverted
systemctl --user daemon-reload
systemctl --user restart cashbook.service
```

---

## Verification

```bash
PID=$(systemctl --user show -p MainPID --value cashbook.service)
/usr/bin/tr '\0' '\n' < /proc/$PID/environ | grep -c '^CASHBOOK_API_KEY='   # expect 0
ls -l /run/user/1000/cashbook/cashbook_key                                  # expect 600
curl -s -o /dev/null -w '%{http_code}\n' 'http://localhost:8100/api/cashbook?key=<key>'   # 200
curl -s -o /dev/null -w '%{http_code}\n' 'http://localhost:8100/api/cashbook?key=WRONG'   # 401
```

> Note: the file is owned by the service user (mode 600). Any same-user process
> could read it; `LoadCredential=` via a pre-service would tighten that, at the
> cost of a second unit. Accepted for the pilot.

---

## References

- systemd.exec(5): `LoadCredential=`, `$CREDENTIALS_DIRECTORY`
- scripts/deploy/kv-to-credential.sh, scripts/deploy/kv-fetch-env.py
- docs/runbooks/option-2-secret-hardening.md
