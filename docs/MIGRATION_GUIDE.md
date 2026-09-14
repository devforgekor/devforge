# DevForge — Migration Guide

> Status: active · Date: 2026-09-14 · Owner: devforge · Related: `docs/REFACTORING_PLAN.md`, `docs/ARCHITECTURE.md`
> 리팩토링된 `src/devforge` 패키지로의 온보딩·전환 가이드. 런타임 구조는 `docs/system-architecture.md` 참조.

---

## 1. 설치

```bash
cd /opt/projects/server
pip install -e ".[dev]"          # 개발 (ruff/mypy/pytest/import-linter 포함)
# Track B(클라우드 공급자) 선택 시:
pip install -e ".[dev,track_b]"
```

## 2. 실행

```bash
devforge --help
devforge status --json
devforge pipeline orchestrate --dry-run --limit 10
devforge pipeline status
devforge mcp serve --port 8100
devforge inference status
```

HTTP API:
```bash
uvicorn devforge.adapters.driving.api.app:app --host 0.0.0.0 --port 8000
```

## 3. 설정 (우선순위)

```
env vars (DEVFORGE_*) > secrets.env > providers.yaml > current-*.env > state.yaml > defaults
```

| 소스 | 경로 |
|---|---|
| secrets | `~/.config/devforge/secrets.env` |
| providers | `config/providers.yaml` (없으면 기본값 사용) |
| runtime inference | `/opt/ai_data/scripts/current-mode-inference.env` |
| system mode | `/opt/ai_data/scripts/current-system-mode.env` |
| persistent | `state.yaml`, `CLAUDE.yaml` |

관련 ADR: `docs/adr/0001-config-priority.md`.

## 4. 레거시 ↔ 신규 매핑

| 레거시 (`scripts/`) | 신규 (`src/devforge/`) |
|---|---|
| `scripts/cli.py` | `src/devforge/cli.py` (`devforge` CLI) |
| `scripts/lib/llm_client/` | `adapters/driven/llm/local_adapter.py` |
| `scripts/lib/db.py` | `adapters/driven/storage/database_gateway.py` |
| `scripts/lib/model_registry.py` | `domain/model_management/` (계획) |
| `scripts/lib/watchdog/` | `domain/watchdog/` (계획) |
| `scripts/mcp_server.py` (FastMCP) | `adapters/driving/mcp/server.py` (SSE) |
| `scripts/pipelines/extract.py` | `application/extract_pipeline.py` + `pipeline_stages/extract/` |

> 서비스 유닛/Quadlet의 `ExecStart`는 컷오버 전까지 레거시 경로를 유지한다.

## 5. 테스트 · 검증

```bash
pytest tests/test_characterization.py tests/test_integration.py -v
DEVFORGE_LLM_REPLAY=1 pytest tests/test_integration.py -v   # 결정론적 replay
ruff check src/ tests/ && ruff format --check src/
mypy src/ --ignore-missing-imports
lint-imports
```

- Replay fixture: `tests/fixtures/llm_recordings/` (`extract_llm.json` 등). 캡처는
  `tests/fixtures/replay_harness.py` 참조. 관련 ADR: `docs/adr/0003-shadow-db.md`.

## 6. DB 마이그레이션

```bash
alembic current
alembic upgrade head
alembic downgrade -1
```

- 앱 소유 16개 테이블 스키마: `docs/specs/schema.sql` (ORM과 일치).
- 마이그레이션: `alembic/versions/`. 관련 ADR: `docs/adr/0004-alembic-migrate.md`.

## 7. 트러블슈팅

| 증상 | 원인/조치 |
|---|---|
| `ModuleNotFoundError: No module named 'lib'` | 정상 — 레거시 `scripts/lib`는 import 불가. `devforge` 패키지를 사용한다. |
| `devforge: command not found` | `pip install -e .` 미실행 또는 `~/.local/bin` PATH 누락 |
| 모델 포트 무응답 | `devforge inference ensure day_extract` / `ss -tlnp \| grep 8082` |
| CI mypy/ruff 실패 | `ruff format src/` 후 재확인, 신규 함수에 반환 타입 주석 필수(strict) |
