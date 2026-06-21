#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.sh — Phase 0 (before extract), day_cycle.py
"""Entity Scan — Phase 0: deterministic entity extraction via regex + DB lookup.

No LLM calls. Pure pattern-based detection + known entity lookup.
Stores results as fact_type='entity_scan' in review_facts.
Extract pipeline reads entity_scan context for hallucination reduction.

Entity sources:
  1. File extensions: regex for known extensions (.py .sh .yaml .json ...)
  2. Function/class defs: def \\w+, class \\w+
  3. DB lookup: file_registry known filenames appearing in text
  4. Conversation context: enrich_meta entities from same conversation (previous cycles)

State: NOT EXISTS entity_scan in review_facts (no retry — idempotent)

Usage:
  python3 scripts/pipelines/entity_scan.py                  # batch
  python3 scripts/pipelines/entity_scan.py --limit 50       # batch cap
  python3 scripts/pipelines/entity_scan.py --dry-run        # simulate
"""

import json
import os
import re
import sys
import time
from typing import Any, Dict, List

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql, psql_ok, esc_sql, psql_json
from lib.infra.preflight import preflight_checks
from lib.watchdog.messenger import heartbeat

BATCH_LIMIT = 10

_EXTENSIONS = (
    r"\.(?:py|sh|yaml|yml|json|md|txt|env|toml"
    r"|cfg|ini|conf|sql|html|css|js|ts|tsx|jsx"
    r"|go|rs|rb|java|kt|swift|c|cpp|h|hpp"
    r"|vue|svelte|astro|wasm|lock|xml|svg"
    r"|Dockerfile|dockerfile|Makefile|makefile)"
)

_FILE_PATTERN = re.compile(
    r'(?:^|[\s(])([\w./-]+' + _EXTENSIONS + r')(?=[\s,;:!?)\]})\n]|$)'
)

_FUNC_PATTERN = re.compile(
    r'\b(?:def|class|async\s+def|fn|func|function)\s+(\w+)(?:\(|:)'
)


def _scan_file_names(text: str) -> List[str]:
    """Extract file names via extension regex."""
    seen: set = set()
    found: List[str] = []
    for m in _FILE_PATTERN.finditer(text):
        name = m.group(1).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        found.append(name)
    return found


def _scan_functions(text: str) -> List[str]:
    """Extract function/class def names via regex."""
    seen: set = set()
    found: List[str] = []
    for m in _FUNC_PATTERN.finditer(text):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            found.append(name)
    return found


def _lookup_file_registry(text: str) -> List[str]:
    """Check text for known filenames in file_registry."""
    try:
        rows = psql_json(
            "SELECT DISTINCT filename FROM file_registry "
            "WHERE filename IS NOT NULL AND filename != ''"
        ) or []
    except Exception:
        return []
    found: List[str] = []
    for row in rows:
        fname = row.get("filename", "").strip()
        if fname and fname in text:
            found.append(fname)
    return found


def _get_conversation_entities(
    conversation_id: str, current_turn_id: str
) -> Dict[str, List[str]]:
    """Get entities from same conversation's previous enrich_meta."""
    result: Dict[str, List[str]] = {"files": [], "functions": []}
    if not conversation_id:
        return result
    try:
        sql = (
            "SELECT evidence::text FROM review_facts "
            "WHERE turn_id IN ("
            "  SELECT id FROM turns "
            f"  WHERE conversation_id = '{esc_sql(conversation_id)}'::uuid"
            f"  AND id != '{esc_sql(current_turn_id)}'::uuid"
            ")"
            " AND fact_type = 'enrich_meta'"
            " ORDER BY fact_index DESC LIMIT 5"
        )
        rows = psql_json(sql) or []
        seen_f: set = set()
        seen_fn: set = set()
        for row in rows:
            try:
                data = json.loads(row.get("evidence", "{}"))
            except json.JSONDecodeError:
                continue
            entities = data.get("entities", {})
            for f in entities.get("files", []):
                f = str(f).strip()
                if f and f not in seen_f:
                    seen_f.add(f)
                    result["files"].append(f)
            for fn in entities.get("functions", []):
                fn = str(fn).strip()
                if fn and fn not in seen_fn:
                    seen_fn.add(fn)
                    result["functions"].append(fn)
    except Exception:
        pass
    return result


def _scan_turn(turn: dict) -> Dict[str, Any]:
    """Run all deterministic entity scans on a single turn."""
    combined = "\n".join([
        turn.get("user_turn") or "",
        turn.get("thinking") or "",
        turn.get("text") or "",
    ])

    regex_files = _scan_file_names(combined)
    regex_funcs = _scan_functions(combined)
    registry_files = _lookup_file_registry(combined)
    conv_id = turn.get("conversation_id", "")
    turn_id = turn.get("id", "")
    conv_ents = _get_conversation_entities(conv_id, turn_id) if conv_id else {}

    all_files = list(dict.fromkeys(
        regex_files + registry_files
    ))
    all_functions = list(dict.fromkeys(
        regex_funcs
    ))

    return {
        "files": all_files[:20],
        "functions": all_functions[:20],
        "_meta": {
            "regex_files": len(regex_files),
            "regex_funcs": len(regex_funcs),
            "registry_files": len(registry_files),
            "conv_files": len(conv_ents.get("files", [])),
            "conv_funcs": len(conv_ents.get("functions", [])),
        },
    }


def _get_turns_for_scan(limit: int = BATCH_LIMIT) -> List[Dict]:
    """Turns without entity_scan, ordered by creation time."""
    sql = (
        "SELECT t.id, t.user_turn, t.thinking, t.text, t.conversation_id "
        "FROM turns t WHERE t.text != '' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM review_facts rf "
        "  WHERE rf.turn_id = t.id AND rf.fact_type = 'entity_scan'"
        ")"
        "ORDER BY t.created_at DESC "
        f"LIMIT {limit}"
    )
    rows = psql_json(sql) or []
    return [{
        "id": r["id"],
        "user_turn": r.get("user_turn", ""),
        "thinking": r.get("thinking", ""),
        "text": r.get("text", ""),
        "conversation_id": r.get("conversation_id", ""),
    } for r in rows]


def _insert_scan(turn_id: str, scan_data: Dict) -> bool:
    """Store entity_scan result in review_facts."""
    fi_str = psql(
        f"SELECT COALESCE(MAX(fact_index), -1) + 1 FROM review_facts "
        f"WHERE turn_id = '{esc_sql(turn_id)}'::uuid"
    )
    fi = int(fi_str) if fi_str and fi_str != "-infinity" else 0

    evidence = json.dumps(
        {k: v for k, v in scan_data.items() if not k.startswith("_")},
        ensure_ascii=False,
    )
    sql = (
        "INSERT INTO review_facts "
        "(turn_id, fact_index, fact_type, evidence, extract_model, "
        " verdict, source, fact_action) VALUES ("
        f"'{esc_sql(turn_id)}'::uuid, {fi}, "
        f"'entity_scan', '{esc_sql(evidence[:5000])}', "
        f"'entity_scan', 'passed', 'entity_scan', 'entity_scan'"
        ")"
    )
    return psql_ok(sql)


def entity_scan_pipeline(
    limit: int = BATCH_LIMIT, dry_run: bool = False
) -> Dict[str, Any]:
    """Run deterministic entity scan on turns without it."""
    t_start = time.monotonic()
    processed = 0
    failed = 0
    total_files = 0
    total_funcs = 0

    print(f"\n{'=' * 60}")
    print("Entity Scan — Phase 0: deterministic extraction (no LLM)")
    if dry_run:
        print("  [DRY RUN] No writes to DB")
    print(f"{'=' * 60}")

    turns = _get_turns_for_scan(limit)
    if not turns:
        print("[entity_scan] No turns needing scan")
        return {"processed": 0, "ok": True, "elapsed_s": 0}

    print(f"[entity_scan] Scanning {len(turns)} turn(s)", flush=True)

    for ti, turn in enumerate(turns, 1):
        result = _scan_turn(turn)
        nf = len(result["files"])
        nfn = len(result["functions"])
        meta = result.get("_meta", {})
        print(
            f"  [{ti}/{len(turns)}] {turn['id'][:8]} — "
            f"{nf} files, {nfn} funcs "
            f"(rx_f={meta.get('regex_files',0)} "
            f"rx_fn={meta.get('regex_funcs',0)} "
            f"reg={meta.get('registry_files',0)} "
            f"ctx_f={meta.get('conv_files',0)})",
            flush=True,
        )
        if dry_run:
            processed += 1
            total_files += nf
            total_funcs += nfn
            continue

        ok = _insert_scan(turn["id"], result)
        if ok:
            processed += 1
            heartbeat("entity_scan", f"turn {turn['id'][:8]} — {nf} files, {nfn} funcs")
            total_files += nf
            total_funcs += nfn
        else:
            failed += 1

    elapsed = round(time.monotonic() - t_start, 1)
    print(f"\n{'=' * 60}")
    print(
        f"Done: {processed} scanned, {failed} failed — "
        f"{total_files} files, {total_funcs} funcs ({elapsed}s)"
    )
    print(f"{'=' * 60}")

    return {
        "processed": processed,
        "failed": failed,
        "files": total_files,
        "functions": total_funcs,
        "elapsed_s": elapsed,
        "ok": failed == 0,
    }


def main() -> None:
    preflight_checks("entity_scan.py")
    import argparse

    parser = argparse.ArgumentParser(
        description="Entity Scan — Phase 0: deterministic entity extraction"
    )
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = entity_scan_pipeline(limit=args.limit, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
