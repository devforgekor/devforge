#!/usr/bin/env python3
"""
worklog.json v1.x → v2.0 마이그레이션 스크립트 (P4-4).

Phase 4 확장:
    - references를 worklog.json → SQLite DB(data/references.db)로 분리
    - decisions를 worklog.json → SQLite DB(data/references.db)로 분리
    - entries를 최근 5개만 유지, 초과분 → worklog_history 테이블로 이동
    - worklog.json에는 각각 references_config, decisions_config 메타데이터만 남김
"""
import json
import os
import sqlite3
import sys
from datetime import datetime

WORKLOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "worklog.json")
REFS_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "references.db")

# 최근 entries 유지 개수
MAX_ENTRIES = 5


def _ensure_refs_db():
    """references DB 디렉토리 및 모든 worklog 테이블 생성"""
    db_dir = os.path.dirname(REFS_DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(REFS_DB_PATH)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS worklog_references (
                ref_key TEXT PRIMARY KEY,
                source_type TEXT NOT NULL DEFAULT 'paper',
                title TEXT NOT NULL,
                year INTEGER,
                url TEXT DEFAULT '',
                last_checked TEXT,
                status TEXT DEFAULT 'active',
                affects TEXT DEFAULT '[]',
                key_findings TEXT DEFAULT '',
                concepts TEXT DEFAULT '[]',
                related_decisions TEXT DEFAULT '[]',
                last_known_version TEXT DEFAULT 'v1',
                storage TEXT DEFAULT '{}',
                reflected_chunks TEXT DEFAULT '[]',
                created_at REAL DEFAULT (unixepoch()),
                updated_at REAL DEFAULT (unixepoch())
            );
            CREATE INDEX IF NOT EXISTS idx_refs_source_type ON worklog_references(source_type);
            CREATE INDEX IF NOT EXISTS idx_refs_status ON worklog_references(status);

            CREATE TABLE IF NOT EXISTS worklog_decisions (
                decision_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'approved',
                reason TEXT,
                context_json TEXT DEFAULT '{}',
                alternatives_json TEXT DEFAULT '[]',
                resolution TEXT,
                user_note TEXT,
                related_refs_json TEXT DEFAULT '[]',
                supersedes_json TEXT DEFAULT '[]',
                created_at REAL DEFAULT (unixepoch())
            );
            CREATE INDEX IF NOT EXISTS idx_decisions_status ON worklog_decisions(status);
            CREATE INDEX IF NOT EXISTS idx_decisions_date ON worklog_decisions(date);

            CREATE TABLE IF NOT EXISTS worklog_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                session TEXT NOT NULL,
                changes_json TEXT NOT NULL DEFAULT '[]',
                notes_json TEXT DEFAULT '[]',
                archived_at REAL DEFAULT (unixepoch())
            );
            CREATE INDEX IF NOT EXISTS idx_history_date ON worklog_history(date);
        """)
        conn.commit()
    finally:
        conn.close()


def _json_to_sqlite(ref_key: str, ref_data: dict) -> tuple:
    """JSON references 객체 → SQLite INSERT용 튜플 변환"""
    return (
        ref_key,
        ref_data.get("source_type", "paper"),
        ref_data.get("title", ""),
        ref_data.get("year"),
        ref_data.get("url", ""),
        ref_data.get("last_checked", ""),
        ref_data.get("status", "active"),
        json.dumps(ref_data.get("affects", []), ensure_ascii=False),
        ref_data.get("key_findings", ""),
        json.dumps(ref_data.get("concepts", []), ensure_ascii=False),
        json.dumps(ref_data.get("related_decisions", []), ensure_ascii=False),
        ref_data.get("last_known_version", "v1"),
        json.dumps(ref_data.get("storage", {}), ensure_ascii=False),
        json.dumps(ref_data.get("reflected_chunks", []), ensure_ascii=False),
        datetime.now().timestamp(),
        datetime.now().timestamp(),
    )


def _decision_to_sqlite(key: str, dec: dict) -> tuple:
    """JSON decisions 객체 → SQLite INSERT용 튜플 변환"""
    return (
        key,
        dec.get("title", ""),
        dec.get("date", ""),
        dec.get("status", "approved"),
        dec.get("reason", ""),
        json.dumps(dec.get("context", {}), ensure_ascii=False),
        json.dumps(dec.get("alternatives", []), ensure_ascii=False),
        dec.get("resolution", ""),
        dec.get("user_note", ""),
        json.dumps(dec.get("related_references", []), ensure_ascii=False),
        json.dumps(dec.get("supersedes", []), ensure_ascii=False),
        datetime.now().timestamp(),
    )


def _entry_to_sqlite(entry: dict) -> tuple:
    """JSON entries 객체 → SQLite INSERT용 튜플 변환"""
    return (
        entry.get("date", ""),
        entry.get("session", ""),
        json.dumps(entry.get("changes", []), ensure_ascii=False),
        json.dumps(entry.get("notes", []), ensure_ascii=False),
        datetime.now().timestamp(),
    )


def migrate():
    if not os.path.exists(WORKLOG_PATH):
        print(f"[migrate] worklog.json 없음: {WORKLOG_PATH}")
        return

    with open(WORKLOG_PATH, "r", encoding="utf-8") as f:
        worklog = json.load(f)

    changes = {
        "references_migrated": 0,
        "references_to_sqlite": 0,
        "decisions_to_sqlite": 0,
        "entries_archived": 0,
    }

    # ── 1. references 마이그레이션 (JSON 객체 정규화) ──
    if "references" in worklog:
        new_refs = {}
        for key, ref in worklog["references"].items():
            if isinstance(ref, str):
                new_refs[key] = {
                    "source_type": "paper",
                    "title": ref,
                    "year": datetime.now().year,
                    "url": "",
                    "last_checked": datetime.now().strftime("%Y-%m-%d"),
                    "status": "active",
                    "affects": [],
                    "key_findings": "",
                    "concepts": [],
                    "related_decisions": [],
                    "last_known_version": "1.0",
                    "storage": {
                        "tier": "cold",
                        "crate_path": f"paper-versions/{key}/",
                        "json_db_path": f"paper-versions/{key}/v1/extracted/",
                        "cold_since": datetime.now().strftime("%Y-%m-%d"),
                        "archive_after": "",
                    },
                    "reflected_chunks": [],
                }
                changes["references_migrated"] += 1
            else:
                new_refs[key] = ref
        worklog["references"] = new_refs

    # ── 2. references → SQLite DB 마이그레이션 ──
    if "references" in worklog and worklog["references"]:
        _ensure_refs_db()
        conn = sqlite3.connect(REFS_DB_PATH)
        try:
            cursor = conn.cursor()
            for ref_key, ref_data in worklog["references"].items():
                row = _json_to_sqlite(ref_key, ref_data)
                cursor.execute(
                    """INSERT OR REPLACE INTO worklog_references
                       (ref_key, source_type, title, year, url, last_checked, status,
                        affects, key_findings, concepts, related_decisions,
                        last_known_version, storage, reflected_chunks,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    row,
                )
                changes["references_to_sqlite"] += 1
            conn.commit()
            print(f"[migrate] references SQLite 저장 완료: {changes['references_to_sqlite']}개")
        finally:
            conn.close()

        worklog["references_config"] = {
            "type": "sqlite",
            "source": "data/references.db",
            "table": "worklog_references",
            "migrated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00"),
            "note": "references는 SQLite DB로 분리됨. docs/worklog.json에는 메타데이터만 보관.",
        }
        del worklog["references"]

    # ── 3. decisions → SQLite DB 마이그레이션 ──
    if "decisions" in worklog and worklog["decisions"]:
        _ensure_refs_db()
        conn = sqlite3.connect(REFS_DB_PATH)
        try:
            cursor = conn.cursor()
            for key, dec in worklog["decisions"].items():
                row = _decision_to_sqlite(key, dec)
                cursor.execute(
                    """INSERT OR REPLACE INTO worklog_decisions
                       (decision_key, title, date, status, reason,
                        context_json, alternatives_json, resolution,
                        user_note, related_refs_json, supersedes_json,
                        created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    row,
                )
                changes["decisions_to_sqlite"] += 1
            conn.commit()
            print(f"[migrate] decisions SQLite 저장 완료: {changes['decisions_to_sqlite']}개")
        finally:
            conn.close()

        worklog["decisions_config"] = {
            "type": "sqlite",
            "source": "data/references.db",
            "table": "worklog_decisions",
            "migrated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00"),
            "note": "decisions는 SQLite DB로 분리됨. docs/worklog.json에는 메타데이터만 보관.",
        }
        del worklog["decisions"]

    # ── 4. entries → 최근 MAX_ENTRIES개 유지, 초과분 → worklog_history ──
    if "entries" in worklog and len(worklog["entries"]) > MAX_ENTRIES:
        _ensure_refs_db()
        conn = sqlite3.connect(REFS_DB_PATH)
        try:
            cursor = conn.cursor()
            # 오래된 순으로 정렬 (앞쪽이 오래됨)
            to_archive = worklog["entries"][:-MAX_ENTRIES]
            for entry in to_archive:
                row = _entry_to_sqlite(entry)
                cursor.execute(
                    """INSERT INTO worklog_history
                       (date, session, changes_json, notes_json, archived_at)
                       VALUES (?,?,?,?,?)""",
                    row,
                )
                changes["entries_archived"] += 1
            conn.commit()
            print(f"[migrate] entries 아카이브 완료: {changes['entries_archived']}개 → worklog_history")
        finally:
            conn.close()

        # 최근 MAX_ENTRIES개만 유지
        worklog["entries"] = worklog["entries"][-MAX_ENTRIES:]

    # ── 5. worklog.json 저장 ──
    with open(WORKLOG_PATH, "w", encoding="utf-8") as f:
        json.dump(worklog, f, ensure_ascii=False, indent=2)

    print(f"[migrate] 완료: "
          f"references {changes['references_migrated']}개 정규화 + {changes['references_to_sqlite']}개 SQLite, "
          f"decisions {changes['decisions_to_sqlite']}개 SQLite, "
          f"entries {changes['entries_archived']}개 아카이브")


if __name__ == "__main__":
    migrate()
