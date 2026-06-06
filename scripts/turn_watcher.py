#!/usr/bin/env python3
# Status: production
# Path: systemd:devforge-turn-watcher
"""turn_watcher.py — real-time session transcript → PostgreSQL.

Polls Claude Code / Copilot / Gemini / Aider jsonl files every few seconds.
Insert new turns directly to conversations + turns tables — no API dependency.
Runs as a systemd user service, independent of Claude Code lifecycle.

SSOT: turns table. All downstream systems (worklog, review, link) derive from turns.
OOM-safe: jsonl lines written to disk synchronously by agents; watcher reads them
regardless of agent process state.
"""

import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from lib.agents import normalize as normalize_agent
from lib.db import psql, psql_ok, esc_sql
from lib.parsers.claude import parse as parse_claude
from lib.parsers.copilot import parse as parse_copilot
from lib.parsers.gemini import parse as parse_gemini
from lib.parsers.aider import parse as parse_aider

# ── config ──────────────────────────────────────────────────────
POLL_INTERVAL = 3  # seconds between full scans
CHECKPOINT_FILE = Path("/opt/projects/server/collect_checkpoint.json")

SOURCES = {
    "claude": {
        "parser": parse_claude,
        "dir": Path("/home/opc/.claude/projects/-home-opc"),
        "glob": "*.jsonl",
    },
    "copilot": {
        "parser": parse_copilot,
        "dir": Path("/home/opc/.copilot/session-state"),
        "glob": None,  # handled specially — subdirectories
    },
    "gemini": {
        "parser": parse_gemini,
        "dir": Path("/home/opc/.gemini/tmp/opc/chats"),
        "glob": "session-*.jsonl",
    },
    "aider": {
        "parser": parse_aider,
        "dir": Path("/home/opc/.aider.chat.history.md"),
        "glob": None,  # single file
    },
}


def load_checkpoint() -> Dict:
    if CHECKPOINT_FILE.exists():
        try:
            return json.loads(CHECKPOINT_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_checkpoint(cp: Dict):
    """Merge-write: reload file first to preserve metadata keys (e.g. last_ingested_at)
    written by collect_turns.py, then overlay our session counts with max() to prevent
    count regression from race conditions."""
    existing = {}
    if CHECKPOINT_FILE.exists():
        try:
            existing = json.loads(CHECKPOINT_FILE.read_text())
        except json.JSONDecodeError:
            pass
    for source, sessions in cp.items():
        base = existing.setdefault(source, {})
        for sid, entry in sessions.items():
            # Skip metadata keys (last_ingested_at, etc.) — preserve from existing
            if isinstance(entry, str) or sid in ("last_ingested_at",):
                continue
            if isinstance(entry, dict):
                prev = base.get(sid, {})
                if isinstance(prev, dict):
                    base[sid] = {
                        "count": max(prev.get("count", 0), entry["count"]),
                        "mtime": max(prev.get("mtime", 0), entry["mtime"]),
                    }
                else:
                    base[sid] = {
                        "count": max(prev if isinstance(prev, int) else 0, entry["count"]),
                        "mtime": entry["mtime"],
                    }
            else:
                # integer count (backward compat)
                base[sid] = max(base.get(sid, 0) if isinstance(base.get(sid, 0), int) else 0, entry)
    CHECKPOINT_FILE.write_text(json.dumps(existing, indent=2, ensure_ascii=False))


def _list_session_files(source: str, config: Dict) -> List[tuple]:
    """Return list of (session_id, Path) for a source."""
    d = config["dir"]
    if not d.exists():
        return []

    if source == "claude":
        return [(p.stem, p) for p in sorted(d.glob("*.jsonl"))]
    elif source == "copilot":
        paths = []
        for sub in sorted(d.iterdir()):
            if sub.is_dir():
                ef = sub / "events.jsonl"
                if ef.exists():
                    paths.append((sub.name, ef))
        return paths
    elif source == "gemini":
        paths = []
        for p in sorted(d.glob("session-*.jsonl")):
            try:
                with open(p) as f:
                    header = json.loads(f.readline().strip())
                sid = header.get("sessionId", p.stem.replace("session-", ""))
            except Exception:
                sid = p.stem.replace("session-", "")
            paths.append((sid, p))
        return paths
    elif source == "aider":
        return [(str(uuid.uuid5(uuid.NAMESPACE_DNS, "aider.devforge")), d)]
    return []


def _cp_get(checkpoint: Dict, source: str, session_id: str) -> dict:
    """Get {count, mtime} for a session from checkpoint. Handles old int-only format."""
    entry = checkpoint.get(source, {}).get(session_id, {})
    if isinstance(entry, int):
        return {"count": entry, "mtime": 0}
    return {"count": entry.get("count", 0), "mtime": entry.get("mtime", 0)}


def ensure_conversation(session_id: str, source: str, model: str = "",
                        title: str = "") -> bool:
    """Upsert conversation row."""
    sid = esc_sql(session_id)
    src = esc_sql(normalize_agent(source))
    mdl = esc_sql(model) if model else ""
    ttl = esc_sql(title or session_id[:8])
    return psql_ok(
        f"INSERT INTO conversations (id, source, model, title) "
        f"VALUES ('{sid}', '{src}', '{mdl}', '{ttl}') "
        f"ON CONFLICT (id) DO NOTHING"
    )


def insert_turns(conversation_id: str, source: str, model: str,
                 new_turns: List[Dict], start_seq: int) -> int:
    """Batch INSERT new turns. Returns count inserted."""
    src = normalize_agent(source)

    # Pre-filter: skip turns whose source_message_id already exists in DB
    msg_ids = [t.get("source_message_id") for t in new_turns if t.get("source_message_id")]
    existing_ids = set()
    if msg_ids:
        ids_sql = ",".join(f"'{esc_sql(m)}'" for m in msg_ids)
        rows = psql(f"SELECT source_message_id FROM turns "
                    f"WHERE source_message_id IN ({ids_sql})")
        if rows:
            for line in rows.strip().split("\n"):
                if line.strip():
                    existing_ids.add(line.strip())

    # Filter and build batch VALUES
    rows_values = []
    filtered_turns = []
    for i, turn in enumerate(new_turns):
        smid = turn.get("source_message_id", "")
        if smid and smid in existing_ids:
            continue
        filtered_turns.append((i, turn))

    if not filtered_turns:
        return 0

    for i, turn in filtered_turns:
        seq = start_seq + i
        user_turn = esc_sql(turn.get("user_turn", "")[:8000])
        thinking = esc_sql((turn.get("thinking") or "")[:4000])
        text = esc_sql((turn.get("text") or "")[:8000])
        smid = turn.get("source_message_id", "")
        msg_id = esc_sql(smid) if smid else ""
        created_at = turn.get("created_at") or datetime.now(timezone.utc).isoformat()
        agent = esc_sql(src)
        msg_id_col = f"'{msg_id}'" if msg_id else "NULL"
        meta = json.dumps({"model": model}, ensure_ascii=False)
        meta_esc = esc_sql(meta)

        rows_values.append(
            f"('{conversation_id}', {seq}, '{user_turn}', '{thinking}', "
            f"'{text}', {msg_id_col}, '{agent}', '{meta_esc}', "
            f"'{created_at}'::timestamptz)"
        )

    if not rows_values:
        return 0

    # Single batch INSERT
    values_sql = ",\n".join(rows_values)
    ok = psql_ok(
        f"INSERT INTO turns (conversation_id, seq, user_turn, thinking, text, "
        f"  source_message_id, agent, meta, created_at) "
        f"VALUES {values_sql} "
        f"ON CONFLICT (conversation_id, seq) DO NOTHING"
    )

    # Count how many actually inserted (approximate: all non-duplicate rows)
    return len(rows_values) if ok else 0


def process_session(source: str, session_id: str, path: Path,
                    parser_fn, checkpoint: Dict) -> int:
    """Parse session, insert new turns. Returns count of newly inserted turns."""
    cp_entry = _cp_get(checkpoint, source, session_id)
    prev_count = cp_entry["count"]
    prev_mtime = cp_entry["mtime"]

    # mtime-based skip: if file hasn't changed since last ingest, skip parsing entirely
    current_mtime = path.stat().st_mtime
    if current_mtime == prev_mtime and prev_count > 0:
        return 0

    parsed, model, is_active = parser_fn(path)

    # Record empty sessions so we don't re-parse them every cycle
    if parsed is None or len(parsed) == 0:
        checkpoint.setdefault(source, {})[session_id] = {
            "count": prev_count, "mtime": current_mtime,
        }
        return 0

    if len(parsed) <= prev_count:
        # mtime changed but no new turns (e.g., file touched). Update mtime only.
        checkpoint.setdefault(source, {})[session_id] = {
            "count": prev_count, "mtime": current_mtime,
        }
        return 0

    new_turns = parsed[prev_count:]
    if not new_turns:
        return 0

    ensure_conversation(session_id, source, model or "")
    inserted = insert_turns(session_id, source, model or "", new_turns, prev_count)

    checkpoint.setdefault(source, {})[session_id] = {
        "count": prev_count + inserted, "mtime": current_mtime,
    }

    if inserted > 0:
        tag = " [active]" if is_active else ""
        print(f"  {source}/{session_id[:8]}: +{inserted} turns "
              f"({prev_count}→{prev_count + inserted}){tag}")

    return inserted


def run_once() -> int:
    """One full scan across all sources. Returns total turns inserted."""
    checkpoint = load_checkpoint()
    total = 0

    for source, config in SOURCES.items():
        sessions = _list_session_files(source, config)
        if not sessions:
            continue

        for session_id, path in sessions:
            try:
                n = process_session(source, session_id, path,
                                   config["parser"], checkpoint)
                total += n
            except Exception as e:
                print(f"  ERROR {source}/{session_id[:8]}: {e}")

    if total > 0:
        save_checkpoint(checkpoint)

    return total


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Real-time session turn watcher")
    ap.add_argument("--once", action="store_true",
                    help="Run one scan and exit (for testing / cron)")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL,
                    help=f"Poll interval in seconds (default: {POLL_INTERVAL})")
    args = ap.parse_args()

    print(f"[{datetime.now(timezone.utc).isoformat()}] turn_watcher starting "
          f"(interval={args.interval}s)")

    if args.once:
        n = run_once()
        print(f"  done: {n} new turns")
        return 0

    # Persistent loop
    while True:
        try:
            n = run_once()
            if n > 0:
                print(f"[{datetime.now(timezone.utc).isoformat()}] "
                      f"scan complete: {n} new turns")
        except Exception as e:
            print(f"[{datetime.now(timezone.utc).isoformat()}] "
                  f"scan error: {e}", file=sys.stderr)

        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
