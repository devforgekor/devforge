#!/usr/bin/env python3.11
# Status: experimental
# Path: devforge_fastapi/app.py (mounted) — Vercel viewer read API
"""Portal read API for the Vercel viewer — display-only JSON.

Endpoints (prefix /api/portal):
  GET /health      : server alive
  GET /summary     : one-shot for the home status strip (open incidents + last backup)
  GET /incidents   : watchdog_incidents list (?open=1&limit=)
  GET /backups     : recent OCI backups/database objects (?limit=)

All data comes from DB/OCI (container-friendly); no systemd access here.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

router = APIRouter(prefix="/api/portal", tags=["portal"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/health")
def health():
    return {"status": "ok", "server": "devforge", "time": _now()}


@router.get("/incidents")
def incidents(open: int = 0, limit: int = 20):
    try:
        from lib.watchdog import incidents as inc

        return {"items": inc.list_incidents(open_only=bool(open), limit=min(limit, 100))}
    except Exception as e:  # noqa: BLE001
        return {"items": [], "error": str(e)}


def _backup_items(limit: int) -> list[dict]:
    from lib.oci_storage import list_objects

    objs = [o for o in list_objects("backups/database/") if not o["name"].endswith("/")]
    objs.sort(key=lambda o: o.get("updated") or "", reverse=True)
    return [
        {"name": o["name"].split("/")[-1], "size": o.get("size"), "time": o.get("updated")}
        for o in objs[:limit]
    ]


@router.get("/backups")
def backups(limit: int = 10):
    try:
        return {"items": _backup_items(limit)}
    except Exception as e:  # noqa: BLE001
        return {"items": [], "error": str(e)}


def _latest_news(limit: int = 3) -> list[dict]:
    from lib.db import psql_json

    return psql_json(
        "SELECT id, title, title_ko, source, "
        "to_char(DATE(collected_at AT TIME ZONE 'Asia/Seoul'),'YYYY-MM-DD') AS date "
        "FROM news_articles WHERE title != 'Test Article' "
        "AND DATE(collected_at AT TIME ZONE 'Asia/Seoul') = "
        "(SELECT MAX(DATE(collected_at AT TIME ZONE 'Asia/Seoul')) FROM news_articles "
        " WHERE title != 'Test Article') "
        f"ORDER BY published_at DESC NULLS LAST LIMIT {int(limit)}"
    )


@router.get("/summary")
def summary():
    open_n = 0
    try:
        from lib.watchdog import incidents as inc

        open_n = len(inc.list_incidents(open_only=True, limit=100))
    except Exception:
        pass
    last = None
    try:
        items = _backup_items(1)
        last = items[0] if items else None
    except Exception:
        pass
    news: list[dict] = []
    try:
        news = _latest_news(3)
    except Exception:
        pass
    return {
        "status": "ok",
        "time": _now(),
        "open_incidents": open_n,
        "last_backup": last,
        "news": news,
    }
