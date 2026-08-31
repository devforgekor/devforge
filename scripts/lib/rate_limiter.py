#!/usr/bin/env python3
# Status: production
# Path: mcp_server.py flaresolverr_bypass (rate_limit 파라미터), 독립 스크립트에서 직접 사용
"""FlareSolverr 요청 속도 제한 유틸리티.

8분 간격 + 랜덤 지연(±2분)으로 Cloudflare 차단 리스크 최소화.
"""

import json
import random
import sqlite3
import time
from pathlib import Path
from typing import Optional

_DEFAULT_INTERVAL = 480  # 8분
_JITTER_RANGE = 120  # ±2분
_DB_PATH = Path("/opt/ai_data/flaresolverr/rate_limiter.db")


def _get_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or _DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            status INTEGER,
            ts REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_url_ts ON request_log(url, ts DESC)")
    conn.commit()
    return conn


def last_request_time(url: str, db_path: Optional[Path] = None) -> Optional[float]:
    conn = _get_db(db_path)
    row = conn.execute(
        "SELECT ts FROM request_log WHERE url = ? ORDER BY ts DESC LIMIT 1",
        (url,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def time_since_last(url: str, db_path: Optional[Path] = None) -> float:
    last = last_request_time(url, db_path)
    if last is None:
        return float("inf")
    return time.time() - last


def seconds_until_allowed(
    url: str,
    interval: int = _DEFAULT_INTERVAL,
    db_path: Optional[Path] = None,
) -> float:
    elapsed = time_since_last(url, db_path)
    if elapsed >= interval:
        return 0.0
    remaining = interval - elapsed
    jitter = random.uniform(0, _JITTER_RANGE)
    return remaining + jitter


def record_request(
    url: str,
    status: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> None:
    conn = _get_db(db_path)
    conn.execute(
        "INSERT INTO request_log (url, status, ts) VALUES (?, ?, ?)",
        (url, status, time.time()),
    )
    conn.commit()
    conn.close()


def wait_if_needed(
    url: str,
    interval: int = _DEFAULT_INTERVAL,
    dry_run: bool = False,
    db_path: Optional[Path] = None,
) -> float:
    wait_sec = seconds_until_allowed(url, interval, db_path)
    if wait_sec <= 0:
        return 0.0
    if dry_run:
        return wait_sec
    time.sleep(wait_sec)
    return wait_sec


def cleanup_old(days: int = 30, db_path: Optional[Path] = None) -> int:
    conn = _get_db(db_path)
    cutoff = time.time() - days * 86400
    cur = conn.execute("DELETE FROM request_log WHERE ts < ?", (cutoff,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return deleted


def stats(url: Optional[str] = None, db_path: Optional[Path] = None) -> dict:
    conn = _get_db(db_path)
    if url:
        row = conn.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM request_log WHERE url = ?",
            (url,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM request_log"
        ).fetchone()
    conn.close()
    return {
        "count": row[0],
        "first": row[1],
        "last": row[2],
    }
