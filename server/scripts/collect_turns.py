#!/usr/bin/env python3
"""collect_turns.py — ingest Claude Code + Copilot session transcripts into DevForge DB.

Incremental: tracks per-session turn count via checkpoint, only sends new turns each run.
Active sessions (mtime < 30s) skip the last turn to avoid partial ingestion.

Usage:
  python3 collect_turns.py                    # all sources, incremental
  python3 collect_turns.py --source claude    # only Claude Code
  python3 collect_turns.py --source copilot   # only Copilot
  python3 collect_turns.py --reset            # re-ingest all sessions
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from lib.agents import normalize as normalize_agent
from lib.parser_claude import parse as parse_claude
from lib.parser_copilot import parse as parse_copilot

API = "http://localhost:8000/ingest"
CHECKPOINT_FILE = Path("/opt/projects/server/collect_checkpoint.json")
STATUS_FILE = Path("/opt/projects/server/docs/collect_status.yaml")
CLAUDE_DIR = Path("/home/opc/.claude/projects/-home-opc")
COPILOT_DIR = Path("/home/opc/.copilot/session-state")


# ── checkpoint ──────────────────────────────────────────────────

def _load_checkpoint():
    if CHECKPOINT_FILE.exists():
        return json.loads(CHECKPOINT_FILE.read_text())
    return {}

def _save_checkpoint(cp):
    CHECKPOINT_FILE.write_text(json.dumps(cp, indent=2, ensure_ascii=False))


# ── session discovery ───────────────────────────────────────────

def _list_sessions(source):
    if source == "claude":
        if not CLAUDE_DIR.exists():
            return []
        return [(p.stem, p) for p in sorted(CLAUDE_DIR.glob("*.jsonl"))]
    else:
        if not COPILOT_DIR.exists():
            return []
        paths = []
        for d in sorted(COPILOT_DIR.iterdir()):
            if d.is_dir():
                ef = d / "events.jsonl"
                if ef.exists():
                    paths.append((d.name, ef))
        return paths


# ── ingestion ───────────────────────────────────────────────────

def _ingest(source, session_id, parser_fn, prev_count):
    """Parse, slice new turns, POST. Returns (new_turns_count, success_bool)."""
    parsed, model, is_active = parser_fn(session_id)
    if parsed is None:
        return 0, True

    new_turns = parsed[prev_count:]
    if not new_turns:
        return 0, True

    payload = {
        "source": normalize_agent(source),
        "model": model,
        "conversation_id": session_id,
        "title": session_id[:8],
        "turns": new_turns,
    }

    for attempt in range(3):
        try:
            r = requests.post(API, json=payload, timeout=30)
            if r.status_code == 200:
                data = r.json()
                tag = " [active]" if is_active else ""
                print(f"  {source}/{session_id[:8]}: +{data['count']} turns "
                      f"({prev_count}→{prev_count + len(new_turns)}){tag}")
                return data["count"], True
            elif 400 <= r.status_code < 500:
                print(f"  {source}/{session_id[:8]}: HTTP {r.status_code}")
                return 0, False
            else:
                print(f"  {source}/{session_id[:8]}: HTTP {r.status_code} "
                      f"(attempt {attempt+1}/3)")
        except requests.ConnectionError:
            print(f"  {source}/{session_id[:8]}: API unreachable (attempt {attempt+1}/3)")
            time.sleep(2 ** attempt)
        except Exception as e:
            print(f"  {source}/{session_id[:8]}: error — {e}")
            return 0, False
    return 0, False


# ── main ────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Collect session turns into DevForge DB")
    ap.add_argument("--source", choices=["claude", "copilot"])
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    cp = {} if args.reset else _load_checkpoint()

    sources = {
        "claude": parse_claude,
        "copilot": parse_copilot,
    }
    if args.source:
        sources = {args.source: sources[args.source]}

    status = {}
    total_turns = 0

    for source, parser_fn in sources.items():
        sessions = _list_sessions(source)
        if not sessions:
            status[source] = {"status": "ok", "sessions": 0, "turns": 0}
            continue

        src_turns = 0
        src_sessions = 0
        errors = []

        for sid, path in sessions:
            prev_count = cp.get(source, {}).get(sid, 0) if not args.reset else 0

            count, ok = _ingest(source, sid,
                                  lambda _sid, p=path: parser_fn(p), prev_count)
            if count > 0:
                src_turns += count
                src_sessions += 1
                cp.setdefault(source, {})[sid] = prev_count + count
            elif not ok:
                errors.append(sid[:8])

        cp.setdefault(source, {})["last_ingested_at"] = \
            datetime.now(timezone.utc).isoformat()
        total_turns += src_turns
        status[source] = {
            "status": "ok" if not errors else f"errors: {errors}",
            "sessions": src_sessions,
            "turns": src_turns,
        }

    _save_checkpoint(cp)

    # ── status file ───────────────────────────────────────────
    status["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    flat = ["# collect_turns status (consumed by 9am Slack hook)",
            f"timestamp: {status['timestamp']}"]
    for key, val in status.items():
        if key == "timestamp":
            continue
        if isinstance(val, dict):
            for sub_key, sub_val in val.items():
                flat.append(f"{key}.{sub_key}: {sub_val}")
        else:
            flat.append(f"{key}: {val}")
    STATUS_FILE.write_text("\n".join(flat) + "\n")

    print(f"Done: {total_turns} turns")


if __name__ == "__main__":
    sys.exit(main())
