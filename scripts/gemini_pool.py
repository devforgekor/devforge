#!/usr/bin/env python3
# Status: production
# Path: imported by gemini_proxy.py
"""gemini_pool.py — Async Gemini API key pool with SQLite-backed persistence.

Manages a pool of API keys for Gemini requests. Keys are loaded from DB,
rotated through the pool, and rate-limited keys are temporarily excluded.

Bugs present (targets for T09-T10):
  T09: _fill_pool() has min_size=2 but no max_size — get() calls _fill_pool()
       on error, causing unbounded pool growth. Failed keys are never removed.
  T10: SQLite in default journal_mode (DELETE) — concurrent coroutine access
       via get() → _fill_pool() → _insert_key() triggers SQLITE_BUSY.
       No WAL mode, no connection pooling.
"""

import asyncio
import aiosqlite
import json
import os
import time
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Tuple

DB_PATH = os.path.expanduser("~/.local/share/devforge/gemini_pool.db")
STATE_FILE = os.path.expanduser("~/.local/share/devforge/gemini_pool_state.json")

MIN_POOL_SIZE = 2
DEFAULT_TTL = 1800
DAILY_QUOTA_THRESHOLD = 300


def _now_ts() -> float:
    return time.time()


class GeminiPoolManager:
    """Async pool of Gemini API keys with SQLite persistence.

    Pool maintains a minimum number of ready keys. Rate-limited keys are
    moved to backoff until their retry window expires.
    """

    def __init__(self, db_path: str = "", min_size: int = MIN_POOL_SIZE):
        self.db_path = db_path or DB_PATH
        self.min_size = min_size
        # BUG (T09): no max_size — pool can grow unbounded
        self._pool: List[dict] = []
        self._lock = asyncio.Lock()
        self._db: Optional[aiosqlite.Connection] = None

    async def _get_db(self) -> aiosqlite.Connection:
        """Get or create a new DB connection. BUG (T10): no WAL mode, no
        connection pooling — each call creates a new connection, and
        concurrent access triggers SQLITE_BUSY."""
        db = await aiosqlite.connect(self.db_path)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS key_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_name TEXT NOT NULL,
                api_key TEXT NOT NULL,
                calls INTEGER DEFAULT 0,
                fails INTEGER DEFAULT 0,
                last_used REAL DEFAULT 0,
                backoff_until REAL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS key_usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_name TEXT NOT NULL,
                status TEXT NOT NULL,
                timestamp TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.commit()
        return db

    async def _insert_key(self, db: aiosqlite.Connection, name: str, key: str) -> int:
        """Insert a new key into the pool DB. Returns row id."""
        cursor = await db.execute(
            "INSERT INTO key_pool (key_name, api_key) VALUES (?, ?)",
            (name, key),
        )
        await db.commit()
        return cursor.lastrowid

    async def also_start_keys(self) -> List[Tuple[str, str]]:
        """Load keys from external source (secrets file / DB) and add to pool.

        BUG (T09): returns keys from DB but never removes failed/stale keys
        already in the pool — pool grows each time this is called."""
        from lib.auth.key_loader import load_api_keys
        keys = load_api_keys()
        if not keys:
            return []

        db = await self._get_db()
        added = []
        for name, api_key in keys:
            cursor = await db.execute(
                "SELECT id FROM key_pool WHERE key_name = ?", (name,)
            )
            row = await cursor.fetchone()
            if row is None:
                await self._insert_key(db, name, api_key)
                added.append((name, api_key))
        await db.close()
        return added

    async def _fill_pool(self) -> None:
        """Ensure pool has at least min_size ready keys. BUG (T09): checks
        only ready-key count (not total), but NEVER removes failed/backoff
        keys from _pool. Each call to _fill_pool loads fresh keys from DB
        while stale entries accumulate — pool grows unbounded.

        BUG (T09): no max_size check — pool can grow without limit."""
        now = _now_ts()
        ready = [e for e in self._pool if e["backoff_until"] < now]
        if len(ready) >= self.min_size:
            return

        db = await self._get_db()

        needed = self.min_size - len(ready)
        cursor = await db.execute(
            "SELECT id, key_name, api_key, calls, fails, backoff_until "
            "FROM key_pool WHERE (backoff_until = 0 OR backoff_until < ?) "
            "ORDER BY calls ASC, fails ASC LIMIT ?",
            (now, needed),
        )
        rows = await cursor.fetchall()
        for row in rows:
            entry = {
                "id": row[0],
                "name": row[1],
                "key": row[2],
                "calls": row[3] or 0,
                "fails": row[4] or 0,
                "backoff_until": row[5] or 0,
            }
            if entry not in self._pool:
                self._pool.append(entry)
        await db.close()

    async def get(self) -> Optional[Tuple[str, str]]:
        """Get the next available key from the pool.

        BUG (T09): when no ready key is found, calls _fill_pool() to load
        more keys from DB — but failed/backoff keys are never removed from
        _pool, so each retry adds fresh keys while stale ones accumulate.
        Pool grows unbounded without max_size enforcement."""
        async with self._lock:
            now = _now_ts()
            ready = [e for e in self._pool if e["backoff_until"] < now]
            ready.sort(key=lambda e: (e["fails"], e["calls"]))

            # BUG (T09): no ready keys → call _fill_pool (which adds more
            # without removing failed ones → pool unbounded growth)
            if not ready:
                await self._fill_pool()
                now = _now_ts()
                ready = [e for e in self._pool if e["backoff_until"] < now]
                ready.sort(key=lambda e: (e["fails"], e["calls"]))

            if not ready:
                return None

            entry = ready[0]
            entry["last_used"] = now

            return entry["name"], entry["key"]

    async def success(self, name: str) -> bool:
        """Record successful API call for the given key."""
        async with self._lock:
            for entry in self._pool:
                if entry["name"] == name:
                    entry["calls"] = entry.get("calls", 0) + 1
                    entry["backoff_until"] = 0
                    break

        db = await self._get_db()
        await db.execute(
            "UPDATE key_pool SET calls = calls + 1, backoff_until = 0 "
            "WHERE key_name = ?", (name,)
        )
        await db.execute(
            "INSERT INTO key_usage_log (key_name, status) VALUES (?, 'success')",
            (name,),
        )
        await db.commit()
        await db.close()
        return True

    async def rate_limited(self, name: str, retry_seconds: int = 60) -> None:
        """Mark key as rate-limited with backoff."""
        backoff = _now_ts() + retry_seconds
        if retry_seconds >= DAILY_QUOTA_THRESHOLD:
            kst = timezone(timedelta(hours=9))
            now_kst = datetime.now(kst)
            today_5pm = now_kst.replace(hour=17, minute=0, second=0, microsecond=0)
            if now_kst >= today_5pm:
                today_5pm += timedelta(days=1)
            backoff = today_5pm.timestamp()

        async with self._lock:
            for entry in self._pool:
                if entry["name"] == name:
                    entry["fails"] = entry.get("fails", 0) + 1
                    entry["backoff_until"] = backoff
                    break

        db = await self._get_db()
        await db.execute(
            "UPDATE key_pool SET fails = fails + 1, backoff_until = ? "
            "WHERE key_name = ?", (backoff, name)
        )
        await db.execute(
            "INSERT INTO key_usage_log (key_name, status) VALUES (?, 'rate_limited')",
            (name,),
        )
        await db.commit()
        await db.close()

    async def stats(self) -> dict:
        """Return current pool statistics."""
        active = [e for e in self._pool if e["backoff_until"] < _now_ts()]
        return {
            "pool_size": len(self._pool),
            "active": len(active),
            "min_size": self.min_size,
            "keys": [
                {
                    "name": e["name"],
                    "calls": e.get("calls", 0),
                    "fails": e.get("fails", 0),
                    "in_backoff": e["backoff_until"] > _now_ts(),
                }
                for e in self._pool
            ],
        }

    async def close(self) -> None:
        """Close DB connection if open."""
        if self._db:
            await self._db.close()
            self._db = None
