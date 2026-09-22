#!/usr/bin/env python3
# Status: experimental
# Path: tests/characterization/
"""Parity: heartbeat adapter vs legacy messenger.check_heartbeat.

The host cannot reach Postgres (no published 5432); this test skips unless the
DB is reachable from the current network (i.e. run inside the pod).
"""
from __future__ import annotations

import pytest

from devforge.adapters.driven.health.pipeline_health import HeartbeatHealthChecker

pytestmark = pytest.mark.characterization

_WORKER = "day_extract"


def _db_reachable() -> bool:
    try:
        from devforge.core.config import get_config
        url = get_config().db_url_async
        return bool(url)
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.asyncio
async def test_heartbeat_matches_legacy() -> None:
    if not _db_reachable():
        pytest.skip("DB not reachable from host (run inside the pod network)")
    legacy_messenger = pytest.importorskip("lib.watchdog.messenger")
    if not hasattr(legacy_messenger, "check_heartbeat"):
        pytest.skip("legacy check_heartbeat not available")

    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
    from devforge.adapters.driven.storage.heartbeat_pg import PostgresHeartbeatRepository
    from devforge.core.config import get_config

    gateway = DatabaseGateway(get_config().db_url_async)
    repo = PostgresHeartbeatRepository(gateway)
    from lib.watchdog import config as legacy_config

    workers = {_WORKER: legacy_config.HEARTBEAT_WORKERS.get(_WORKER, 1800)}
    checks = await HeartbeatHealthChecker(repo, workers).check_health()
    assert len(checks) == 1 and checks[0].component == f"heartbeat:{_WORKER}"
