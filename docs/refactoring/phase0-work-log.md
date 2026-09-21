# Phase 0 Work Log — Week 1-2

**Phase:** 0 (Foundation + Characterization Tests)
**Started:** 2026-09-13
**Target completion:** 2026-09-27 (2 weeks)
**Current status:** Phase 0 COMPLETE (Week 1 + Week 2) as of 2026-09-21

Companion SSOT: `docs/refactoring/REFACTORING_STATUS.yaml`

---

## Week 1 (2026-09-13 ~ 2026-09-21) — COMPLETE

### 2026-09-13 (Day 1)
- Created `pyproject.toml` (src-layout, dependencies, entry point `devforge`).
- Created `src/devforge/` skeleton (adapters, application, cli, core, domain, pipeline_stages, ports).
- Alembic bootstrap: `alembic/env.py` + `20260913_initial.py`, `20260914_fix_initial_schema.py`.

### 2026-09-14 ~ 2026-09-16
- `core/config.py`: ConfigRegistry + 5-source merge.
- `core/logging.py`: structlog + JSON, single emission via `ProcessorFormatter`.
- `ports/extract.py`: `LLMPort` interface.
- `domain/` subpackages: `model_management`, `pipeline`, `turn_collection`, `watchdog`.

### 2026-09-21 (Week 1 completion — this session)
- `core/database.py`: SQLAlchemy 2.0 async `DatabaseGateway` (commit 72f67a4).
- `core/paths.py`: `Paths` becomes the SSOT for `DATA_DIR/SERVER_DIR/CONFIG_DIR`
  (path constants moved out of `core/config.py`, circular-import workaround removed).
- `core/exceptions.py`: `DevForgeError` hierarchy.
- `import-linter`: 4 contracts (`layering`, `hexagonal`, `domain-subpackage-independence`,
  `domain-agnostic-of-adapters`), wired into CI (`.github/workflows/ci.yml`, architecture gate).
- Tests: `tests/unit/` added (database/paths/exceptions/config), 49 passing.

**`devforge.system_sync` auto-commit incident (same day):** the 30-min timer ran
`git add -A`, sweeping this session's staged review fixes into commit `bd41445`
(`auto: sync`) before they could be committed deliberately. Fixed in commit 0b4b88a:
the auto-commit now skips when a git operation is in progress and unstages
`src/ tests/ scripts/ pyproject.toml` so authored code stays for deliberate commits.

### Review + hardening pass (2026-09-21)
- Fixed `get_session()` (async generator was documented as `async with` -> now
  `@asynccontextmanager`), added `-> None` annotations, resolved 31 ruff findings,
  replaced placeholder tests with real commit/rollback/dispose assertions (73b6c66).
- Centralized DSN normalization in `core.config.normalize_async_dsn` + `db_url_async`;
  both the core engine factory and the storage adapter use it (dedup). Added
  `ConfigurationError` fast-fail on empty DSN (4840f6f).
- Added `tests/unit/test_config.py` (DSN + env precedence).

### Commits
| hash | message |
|------|---------|
| ed6971f | docs(archive): move completed and old plan docs to _archive |
| 733139f | docs(refactoring): update Phase 0 progress and fix week count |
| f53f8b1 | docs(runbooks): add detailed guides for Options 1-4 |
| 72f67a4 | refactor(phase-0): complete Week 1 tasks |
| 73b6c66 | fix(tests): make database tests exercise real behavior |
| 4840f6f | refactor(phase-0): centralize DSN normalization and path constants |
| 0b4b88a | fix(system-sync): stop auto-commit from absorbing authored source |
| 9af3c4e | docs(handover): record core/database.py unwired issue |

---

## Week 2 (2026-09-21) — COMPLETE

### Completed — `tests/characterization/` (19 tests, all pass)
- test 1 `test_day_cycle.py`: batch reservation selects oldest pending, `LIMIT 50`,
  `pending -> batching` (the header comment still says "10" — stale; the real query
  is LIMIT 50, captured as the locked behavior).
- test 2 `test_check_all_llm.py`: T1/T2 probe shape; T2 runs only if T1 ok;
  non-serving port skipped.
- test 3 `test_call_llm.py`: returns stripped content; `return_meta=True` shape;
  registry defaults (max_tokens/temperature/port); `json_mode` sets response_format;
  unknown model raises ValueError.
- test 4 `test_watchdog.py`: liveness path constant + current-timestamp write.
- test 5 `test_text_clean.py`: language detection (`ko`/`en` fixed 0.8; others unknown).

### Notes
- No live DB/LLM/ports: tests parse `day_cycle.sh` text or mock urllib/probe functions.
  LLM record/replay fixtures remain available for deeper Phase 3.5 comparison.
- `tests/test_characterization.py` (Phase 0.6 stub) still covers config/pipeline/fixture
  checks; the 5 targeted tests live in `tests/characterization/`.

---

## Blockers

None.

---

## Open issues carried forward

- `core/database.py` is not wired to production (tests only); live gateway is
  `adapters/driven/storage/database_gateway.py`. See handover known_issue
  `CORE-DB-UNWIRED-2026-09-21` — decide wire-or-remove in Phase 1.

**Next update:** at Phase 1 start (inference container port + LLM provider port).
