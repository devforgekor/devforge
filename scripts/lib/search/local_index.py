#!/usr/bin/env python3
# Status: experimental
# Path: imported by CLI search, MCP search tools
"""SQLite FTS5 search index — Korean-aware BM25 via Kiwi terms.

Architecture:
  - SQLite FTS5 with unicode61 tokenizer (NO porter — English-only)
  - Kiwi lexical terms stored space-separated in `terms` column (primary BM25 field)
  - Full cleaned text also indexed for secondary matching
  - BM25 weights: terms=5, text_clean=3, user_turn_clean=2, thinking_clean=1
  - DB at /opt/ai_data/search/search.db (on ai_data partition)

Rebuild from PostgreSQL:
  python3 -c "from lib.search.local_index import FTS5Index; FTS5Index().rebuild()"

Search:
  python3 -c "from lib.search.local_index import FTS5Index; print(FTS5Index().bm25_search('질문'))"
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

from lib.db import psql_json

SEARCH_DIR = Path("/opt/ai_data/search")
SEARCH_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = SEARCH_DIR / "search.db"

# BM25 per-column weights: [terms, user_turn_clean, text_clean, thinking_clean]
BM25_WEIGHTS = "5, 2, 3, 1"

SCHEMA_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS turn_search USING fts5(
    terms,
    user_turn_clean,
    text_clean,
    thinking_clean,
    content='',
    tokenize='unicode61',
    prefix='2 3'
);
"""

META_SQL = """
CREATE TABLE IF NOT EXISTS turn_meta (
    turn_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    agent TEXT,
    seq INTEGER,
    fts_rowid INTEGER
);

CREATE INDEX IF NOT EXISTS idx_turn_meta_fts_rowid ON turn_meta(fts_rowid);
"""


class FTS5Index:
    """SQLite FTS5 index for Korean turns using Kiwi terms."""

    def __init__(self, db_path: str | Path = DB_PATH) -> None:
        self._path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_size=-524288")  # 512MB
        return conn

    def init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(SCHEMA_SQL)
            conn.executescript(META_SQL)
            conn.commit()
        finally:
            conn.close()

    def rebuild(self, limit: int = 0, batch: int = 500) -> Dict:
        """Full rebuild from PostgreSQL turns table.

        Args:
            limit: Max rows to index (0 = all).
            batch: Rows per SQLite transaction.
        Returns:
            {elapsed_s, total, inserted}
        """
        t0 = time.monotonic()
        self.init_schema()

        sql = (
            "SELECT id, conversation_id, seq, agent, created_at::text, "
            "  user_turn_clean, text_clean, thinking_clean, tokens "
            "FROM turns "
            "WHERE tokens IS NOT NULL AND jsonb_typeof(tokens) = 'object' "
            "ORDER BY created_at ASC"
        )
        if limit > 0:
            sql += f" LIMIT {limit}"
        rows = psql_json(sql) or []

        conn = self._connect()
        total = len(rows)
        inserted = 0
        rowid = 1  # sequential rowid for FTS5
        fts_buf: List[tuple] = []
        meta_buf: List[tuple] = []

        try:
            # Clear existing data
            conn.execute("DELETE FROM turn_search")
            conn.execute("DELETE FROM turn_meta")

            for row in rows:
                tid = row["id"]
                tokens_data = row.get("tokens", "")
                if isinstance(tokens_data, str) and tokens_data:
                    try:
                        tokens_data = json.loads(tokens_data)
                    except json.JSONDecodeError:
                        tokens_data = {}

                terms_list = []
                if isinstance(tokens_data, dict):
                    terms_list = tokens_data.get("terms", [])
                terms_str = " ".join(terms_list) if terms_list else ""

                fts_buf.append((
                    rowid,
                    terms_str,
                    row.get("user_turn_clean") or "",
                    row.get("text_clean") or "",
                    row.get("thinking_clean") or "",
                ))
                meta_buf.append((
                    tid,
                    row["conversation_id"],
                    row["created_at"],
                    row.get("agent") or "",
                    row.get("seq") or 0,
                    rowid,
                ))
                rowid += 1

                if len(fts_buf) >= batch:
                    self._flush(conn, fts_buf, meta_buf)
                    inserted += len(fts_buf)
                    fts_buf, meta_buf = [], []

            if fts_buf:
                self._flush(conn, fts_buf, meta_buf)
                inserted += len(fts_buf)

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        elapsed = time.monotonic() - t0
        return {"elapsed_s": round(elapsed, 1), "total": total, "inserted": inserted}

    def _flush(self, conn: sqlite3.Connection, fts_buf: List[tuple], meta_buf: List[tuple]) -> None:
        conn.executemany(
            "INSERT INTO turn_search (rowid, terms, user_turn_clean, text_clean, thinking_clean) "
            "VALUES (?, ?, ?, ?, ?)",
            fts_buf,
        )
        conn.executemany(
            "INSERT INTO turn_meta (turn_id, conversation_id, created_at, agent, seq, fts_rowid) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            meta_buf,
        )

    def bm25_search(self, query: str, limit: int = 20) -> List[Dict]:
        """Full-text search with BM25 ranking.

        Args:
            query: FTS5 query phrase (space-separated Kiwi terms recommended).
            limit: Max results.
        Returns:
            List of dicts with turn metadata plus ranked text snippets.
        """
        if not query.strip():
            return []

        conn = self._connect()
        try:
            sql = (
                "SELECT m.turn_id, m.conversation_id, m.created_at, "
                "  m.agent, m.seq, m.fts_rowid, "
                "  s.terms, s.user_turn_clean, s.text_clean, s.thinking_clean, "
                f"  bm25(turn_search, {BM25_WEIGHTS}) as rank "
                "FROM turn_search s "
                "JOIN turn_meta m ON m.fts_rowid = s.rowid "
                "WHERE s.terms MATCH ? "
                "ORDER BY rank "
                "LIMIT ?"
            )
            rows = conn.execute(sql, (query, limit)).fetchall()
            cols = ["turn_id", "conversation_id", "created_at", "agent", "seq",
                    "sqlite_rowid", "terms", "user_turn_clean", "text_clean",
                    "thinking_clean", "rank"]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            conn.close()

    def count(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM turn_meta").fetchone()
            return row[0] if row else 0
        finally:
            conn.close()


_index: Optional[FTS5Index] = None


def get_index() -> FTS5Index:
    global _index
    if _index is None:
        _index = FTS5Index()
    return _index
