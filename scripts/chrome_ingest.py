#!/usr/bin/env python3
# Status: experimental
# Path: ~/.local/share/chrome-web-llm/scripts/web-llm-cli.sh (after append_turn) · manual: chrome_ingest.py --all
"""Push chrome-web-llm conversation captures into devforge turns via POST /api/v1/ingest."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DEFAULT_DATA_DIR = Path("/home/opc/.local/share/chrome-web-llm/conversations")
DEFAULT_ENDPOINT = "http://127.0.0.1:8000/api/v1/ingest"
AGENT = "chrome-web-llm"
SOURCE_PREFIX = "chrome"
ROLES = ("user", "assistant")


def load_rows(path: Path) -> list[dict[str, Any]]:
    """Read capture rows, dropping empty/trivial and malformed ones (governance gate)."""
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            print(f"[chrome_ingest] {path.name}:{lineno} malformed json skipped", file=sys.stderr)
            continue
        if not isinstance(row, dict):
            continue
        if row.get("role") not in ROLES:
            continue
        if not str(row.get("text") or "").strip():
            continue
        rows.append(row)
    return rows


def pair_turns(rows: list[dict[str, Any]], session: str) -> list[dict[str, Any]]:
    """Merge user/assistant rows into ingest turns; keep half pairs rather than dropping them."""
    turns: list[dict[str, Any]] = []

    def flush(user_row: dict[str, Any] | None, assistant_row: dict[str, Any] | None) -> None:
        seq = len(turns) + 1
        anchor = assistant_row or user_row or {}
        model = str(anchor.get("model") or "")
        ts = str(anchor.get("ts") or "")
        meta: dict[str, Any] = {}
        if model:
            meta["model"] = model
        if ts:
            meta["ts"] = ts
        turns.append(
            {
                "seq": seq,
                "user_turn": str((user_row or {}).get("text") or ""),
                "text": str(assistant_row.get("text") or "") if assistant_row else None,
                "meta": meta,
                "source_message_id": f"{SOURCE_PREFIX}:{session}:{seq}",
            }
        )

    pending: dict[str, Any] | None = None
    for row in rows:
        if row["role"] == "user":
            if pending is not None:
                flush(pending, None)
            pending = row
        else:
            flush(pending, row)
            pending = None
    if pending is not None:
        flush(pending, None)
    return turns


def build_payload(session: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble the /api/v1/ingest body with stable ids so re-pushes upsert instead of duplicating."""
    models = sorted({str(r.get("model") or "") for r in rows if r.get("model")})
    model = models[0] if len(models) == 1 else ""
    source = f"{SOURCE_PREFIX}:{model}" if model else SOURCE_PREFIX
    return {
        "source": source,
        "agent": AGENT,
        "title": f"chrome-web-llm:{session}"[:200],
        "model": model,
        "conversation_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"chrome-web-llm:{session}")),
        "turns": pair_turns(rows, session),
    }


def push(payload: dict[str, Any], endpoint: str, timeout: float = 5.0) -> tuple[bool, str]:
    """POST one conversation; returns (ok, detail). Never raises — capture must stay fail-silent."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return False, f"HTTP {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001 — fail-silent capture boundary
        return False, f"{type(exc).__name__}: {exc}"
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    if isinstance(parsed, dict) and parsed.get("error"):
        return False, str(parsed["error"])
    if isinstance(parsed, dict):
        return True, f"inserted={parsed.get('inserted')} skipped={parsed.get('skipped')}"
    return True, "ok"


def resolve_targets(session: str | None, data_dir: Path, all_sessions: bool) -> list[Path]:
    if session:
        return [data_dir / f"{session}.jsonl"]
    if all_sessions:
        return sorted(data_dir.glob("*.jsonl"))
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Push chrome-web-llm captures to devforge POST /api/v1/ingest."
    )
    parser.add_argument("session", nargs="?", help="session name (file stem under --data-dir)")
    parser.add_argument("--all", action="store_true", help="push every session file")
    parser.add_argument("--dry-run", action="store_true", help="print payload instead of posting")
    parser.add_argument("--strict", action="store_true", help="exit 1 when a push fails")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("WEBLLM_DATA_DIR", str(DEFAULT_DATA_DIR)),
        help="directory holding <session>.jsonl captures",
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("DEVFORGE_INGEST_URL", DEFAULT_ENDPOINT),
        help="ingest endpoint (default devforge MCP :8000)",
    )
    args = parser.parse_args(argv)

    if not args.session and not args.all:
        parser.error("pass a session name or --all")
    if args.session and args.all:
        parser.error("session and --all are mutually exclusive")

    data_dir = Path(args.data_dir)
    targets = resolve_targets(args.session, data_dir, args.all)
    if not targets:
        print(f"[chrome_ingest] no session files in {data_dir}", file=sys.stderr)
        return 1

    ok = True
    for path in targets:
        if not path.exists():
            print(f"[chrome_ingest] missing {path}", file=sys.stderr)
            return 1
        rows = load_rows(path)
        if not rows:
            print(f"[chrome_ingest] {path.name}: no capturable rows", file=sys.stderr)
            continue
        payload = build_payload(path.stem, rows)
        if args.dry_run:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            continue
        pushed, detail = push(payload, args.endpoint)
        ok = ok and pushed
        print(f"[chrome_ingest] {path.name}: {detail}", file=sys.stderr)

    if not ok and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
