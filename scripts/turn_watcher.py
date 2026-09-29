#!/usr/bin/env python3.12
# Status: production
# Path: systemd:devforge-turn-watcher.service (host), CLI: turn_watcher.py --once
"""turn_watcher.py — real-time session transcript → PostgreSQL (raw insert).

Polls Claude Code / Copilot jsonl files and the OpenCode sqlite DB every
few seconds. Inserts new turns with pipeline_state='raw' — text_clean is
deferred to raw_consumer (Pass 2) in the devforge-worker container.

Pipe: turn_watcher (jsonl/DB → raw) → raw_consumer (raw → pending) → day_cycle

Claude 수집 정책 (단일 경로):
- 세션은 `~/.claude/projects/-home-opc/*.jsonl`만 수집한다 (SOURCES["claude"]).
- claude는 반드시 HOME(~)에서 실행할 것. 다른 cwd(예: /opt/workspace)에서
  실행하면 `-opt-workspace` 같은 프로젝트 디렉토리가 생성되며 수집되지 않는다.
- gemini/aider 소스는 2026-09-19 폐기, copilot은 비용 소진으로 중단 상태.
"""

import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from lib.db import esc_sql, psql, psql_ok
from lib.parsers import opencode as opencode_parser
from lib.parsers.claude import parse as parse_claude
from lib.parsers.copilot import parse as parse_copilot
from lib.tracking.agent_names import normalize as normalize_agent

from devforge.domain.turn_collection.collection import (
    SessionAction,
    filter_unseen,
    get_entry,
    merge_checkpoint,
    plan_session,
    should_skip_parse,
    truncate_turn,
)

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
    "opencode": {
        "parser": opencode_parser.parse,
        "dir": Path("/home/opc/.local/share/opencode/opencode.db"),
        "glob": None,  # handled specially — sqlite DB with per-session rows
        "mtime_fn": opencode_parser.session_mtime,  # (path, session_id) -> float sec
        "sid_fn": lambda s: str(uuid.uuid5(uuid.NAMESPACE_DNS, f"opencode:{s}")),
        "title_fn": opencode_parser.session_title,  # (path, session_id) -> str|None
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
    """Merge-write: reload file first so metadata keys (e.g. last_ingested_at)
    written by collect_turns.py survive, and counts never regress under race."""
    existing = {}
    if CHECKPOINT_FILE.exists():
        try:
            existing = json.loads(CHECKPOINT_FILE.read_text())
        except json.JSONDecodeError:
            pass
    CHECKPOINT_FILE.write_text(
        json.dumps(merge_checkpoint(existing, cp), indent=2, ensure_ascii=False)
    )


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
    elif source == "opencode":
        # One entry per session in the sqlite DB. Session ids stay raw here;
        # process_session maps them to deterministic UUIDs via sid_fn.
        return [(s["id"], d) for s in opencode_parser.list_sessions(d)]
    return []


def ensure_conversation(session_id: str, source: str, model: str = "", title: str = "") -> bool:
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


_INSERT_CHUNK = 300  # max rows per INSERT / IN query
_INSERT_BYTES = 60000  # max SQL bytes per chunk (ARG_MAX / MAX_ARG_STRLEN safety)


def insert_turns(
    conversation_id: str,
    source: str,
    model: str,
    new_turns: List[Dict],
    start_seq: int,
    dry_run: bool = False,
) -> int:
    """Batch INSERT new turns. Returns rows inserted (rows that would be
    attempted when dry_run is set — no INSERT is issued)."""
    src = normalize_agent(source)

    # Pre-filter: skip turns whose source_message_id already exists in DB
    existing_ids = set()
    msg_ids = [t.get("source_message_id") for t in new_turns if t.get("source_message_id")]
    for i in range(0, len(msg_ids), _INSERT_CHUNK):
        chunk = msg_ids[i : i + _INSERT_CHUNK]
        ids_sql = ",".join(f"'{esc_sql(m)}'" for m in chunk)
        rows = psql(f"SELECT source_message_id FROM turns WHERE source_message_id IN ({ids_sql})")
        if rows:
            for line in rows.strip().split("\n"):
                if line.strip():
                    existing_ids.add(line.strip())

    # Filter and build VALUES rows (index i preserved for seq)
    rows_values = []
    filtered_turns = filter_unseen(new_turns, existing_ids)
    if not filtered_turns:
        return 0

    for i, turn in filtered_turns:
        seq = start_seq + i
        fields = truncate_turn(turn)
        user_turn = esc_sql(fields["user_turn"])
        thinking = esc_sql(fields["thinking"])
        text = esc_sql(fields["text"])
        smid = turn.get("source_message_id", "")
        msg_id = esc_sql(smid) if smid else ""
        created_at = turn.get("created_at") or datetime.now(timezone.utc).isoformat()
        agent = esc_sql(src)
        msg_id_col = f"'{msg_id}'" if msg_id else "NULL"

        meta = json.dumps({"model": model}, ensure_ascii=False)
        meta_esc = esc_sql(meta)

        rows_values.append(
            f"('{conversation_id}', {seq}, '{user_turn}', '{thinking}', "
            f"'{text}', {msg_id_col}, '{agent}', '{esc_sql(source)}', '{meta_esc}', "
            f"'{created_at}'::timestamptz, 'raw')"
        )

    if not rows_values:
        return 0

    # Chunk the VALUES rows — cap rows AND total bytes to stay under ARG_MAX
    chunks: List[List[str]] = []
    buf: List[str] = []
    buf_bytes = 0
    for rv in rows_values:
        buf.append(rv)
        buf_bytes += len(rv)
        if len(buf) >= _INSERT_CHUNK or buf_bytes >= _INSERT_BYTES:
            chunks.append(buf)
            buf = []
            buf_bytes = 0
    if buf:
        chunks.append(buf)

    if dry_run:
        return sum(len(chunk) for chunk in chunks)

    return sum(_insert_chunk(chunk) for chunk in chunks)


def _insert_chunk(rows_values: List[str]) -> int:
    """Execute one chunked INSERT; return rows attempted."""
    values_sql = ",\n".join(rows_values)
    ok = psql_ok(
        f"INSERT INTO turns (conversation_id, seq, user_turn, thinking, text, "
        f"  source_message_id, agent, source, meta, created_at, pipeline_state) "
        f"VALUES {values_sql} "
        f"ON CONFLICT (conversation_id, seq) DO NOTHING"
    )
    return len(rows_values) if ok else 0


def process_session(
    source: str,
    session_id: str,
    path: Path,
    parser_fn,
    checkpoint: Dict,
    config: Dict = None,
    dry_run: bool = False,
) -> int:
    """Parse session, insert new turns. Returns count of newly inserted turns."""
    config = config or {}
    # opencode shares one DB file mtime across sessions → use per-session mtime_fn.
    mtime_fn = config.get("mtime_fn", lambda p, s: p.stat().st_mtime)
    # conversation id (deterministic UUID for non-UUID source ids like opencode)
    sid_fn = config.get("sid_fn", lambda s: s)
    conv_id = sid_fn(session_id)
    title_fn = config.get("title_fn")

    prev = get_entry(checkpoint, source, conv_id)
    # mtime-based skip: if file hasn't changed since last ingest, skip parsing entirely
    current_mtime = mtime_fn(path, session_id)
    if should_skip_parse(prev, current_mtime):
        return 0

    parsed, model, is_active = parser_fn(path, session_id=session_id)
    plan = plan_session(prev, current_mtime, parsed)

    if plan.action is not SessionAction.INGEST:
        # Empty / unchanged session: refresh mtime so it isn't re-parsed every
        # cycle. A dry run must not touch the file the live watcher is writing.
        if not dry_run and plan.record is not None:
            checkpoint.setdefault(source, {})[conv_id] = {
                "count": plan.record.count,
                "mtime": plan.record.mtime,
            }
        return 0

    title = title_fn(path, session_id) if title_fn else ""
    if not dry_run:
        ensure_conversation(conv_id, source, model or "", title or "")
    inserted = insert_turns(
        conv_id, source, model or "", plan.new_turns, prev.count, dry_run=dry_run
    )

    if not dry_run:
        checkpoint.setdefault(source, {})[conv_id] = {
            "count": prev.count + inserted,
            "mtime": current_mtime,
        }

    if inserted > 0:
        if dry_run:
            print(f"  DRY-RUN {source}/{session_id[:8]}: would attempt {inserted} turns")
        else:
            tag = " [active]" if is_active else ""
            print(
                f"  {source}/{session_id[:8]}: +{inserted} turns "
                f"({prev.count}→{prev.count + inserted}){tag}"
            )

    return inserted


def run_once(dry_run: bool = False) -> int:
    """One full scan across all sources. Returns total turns inserted."""
    checkpoint = load_checkpoint()
    total = 0

    for source, config in SOURCES.items():
        sessions = _list_session_files(source, config)
        if not sessions:
            continue

        for session_id, path in sessions:
            try:
                n = process_session(
                    source, session_id, path, config["parser"], checkpoint, config, dry_run=dry_run
                )
                total += n
            except Exception as e:
                print(f"  ERROR {source}/{session_id[:8]}: {e}")

    if total > 0 and not dry_run:
        save_checkpoint(checkpoint)

    return total


def main():
    import argparse

    ap = argparse.ArgumentParser(description="Real-time session turn watcher")
    ap.add_argument(
        "--once", action="store_true", help="Run one scan and exit (for testing / cron)"
    )
    ap.add_argument(
        "--interval",
        type=int,
        default=POLL_INTERVAL,
        help=f"Poll interval in seconds (default: {POLL_INTERVAL})",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and report rows without writing to DB or checkpoint",
    )
    args = ap.parse_args()

    print(
        f"[{datetime.now(timezone.utc).isoformat()}] turn_watcher starting "
        f"(interval={args.interval}s, dry_run={args.dry_run})"
    )

    if args.once:
        n = run_once(dry_run=args.dry_run)
        label = "would attempt" if args.dry_run else "new"
        print(f"  done: {n} {label} turns")
        return 0

    # Persistent loop
    while True:
        try:
            n = run_once(dry_run=args.dry_run)
            if n > 0:
                print(f"[{datetime.now(timezone.utc).isoformat()}] scan complete: {n} new turns")
        except Exception as e:
            print(f"[{datetime.now(timezone.utc).isoformat()}] scan error: {e}", file=sys.stderr)

        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
