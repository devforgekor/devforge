#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.sh — Phase 0 (before extract), day_cycle.py
"""Entity Scan — Phase 0: deterministic entity extraction via regex + DB lookup.

No LLM calls. Pure pattern-based detection + known entity lookup.
Stores results as fact_type='entity_scan' in review_facts.
Extract pipeline reads entity_scan context for hallucination reduction.

Entity types (7): file, func, class, library, model, variable, service
  - file:  regex for known extensions (.py .sh .yaml .json ...)
  - func:  def pattern
  - class: class pattern
  - library: import statements + pip cache lookup
  - model: known model names (MODEL_METADATA + common names)
  - variable: CONSTANT_NAME = pattern
  - service: *.service *.timer references

Entity sources:
  1. File extensions: regex for known extensions
  2. Function defs: def \\w+
  3. Class defs: class \\w+
  4. Import statements: import X, from X import Y
  5. Model references: known model names
  6. DB lookup: file_registry known filenames appearing in text
  7. Conversation context: previous enrich_meta entities (all types)

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

BATCH_LIMIT = 50

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

_DEF_PATTERN = re.compile(
    r'\b(?:def|async\s+def|fn|func|function)\s+(\w+)\s*\('
)

_CLASS_PATTERN = re.compile(
    r'\bclass\s+(\w+)(?:\s*[\(:])'
)

_IMPORT_FROM_PATTERN = re.compile(
    r'(?:^|[\s;])from\s+(\S+)\s+import\s+\S+'
)
_IMPORT_DIRECT_PATTERN = re.compile(
    r'^import\s+(\S+)', re.MULTILINE
)

_MODEL_PATTERN = re.compile(
    r'(?:^|\s|[(\"])(qwen[\w.-]*|deepseek[\w.-]*|llama[\w.-]*|'
    r'nemotron[\w.-]*|gemma[\w.-]*|mistral[\w.-]*|phi[\w.-]*|'
    r'codestral[\w.-]*|starcoder[\w.-]*|gpt[\d.-]*|claude[\w.-]*|'
    r'bert[\w.-]*|minilm[\w.-]*|bge-[\w.-]*|e5-[\w.-]*|'
    r'jina-embed[\w.-]*|nomic-embed[\w.-]*)(?<!\.)(?=[\s,;:.!?)\]}\n]|$)',
    re.IGNORECASE
)

_VAR_PATTERN = re.compile(
    r'(?:^|[\s(])([A-Z][A-Z_0-9]{2,})\s*[:=]'
)

_SERVICE_PATTERN = re.compile(
    r'(?:^|[\s(\"])([\w-]+\.(?:service|timer|socket|path|target))(?=[\s,;:!?)\]})\n]|$)'
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
    """Extract function def names via regex."""
    seen: set = set()
    found: List[str] = []
    for m in _DEF_PATTERN.finditer(text):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            found.append(name)
    return found


def _scan_classes(text: str) -> List[str]:
    """Extract class names via regex."""
    seen: set = set()
    found: List[str] = []
    for m in _CLASS_PATTERN.finditer(text):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            found.append(name)
    return found


def _scan_imports(text: str) -> List[str]:
    """Extract library names from import statements."""
    seen: set = set()
    found: List[str] = []
    # from X import Y → X is the library
    for m in _IMPORT_FROM_PATTERN.finditer(text):
        mod = m.group(1).split(".")[0].strip()
        if mod and mod not in seen:
            seen.add(mod)
            found.append(mod)
    # import X → X is the library
    for m in _IMPORT_DIRECT_PATTERN.finditer(text):
        mod = m.group(1).split(".")[0].strip()
        if mod and mod not in seen:
            seen.add(mod)
            found.append(mod)
    return found


def _scan_model_names(text: str) -> List[str]:
    """Extract known model name references."""
    seen: set = set()
    found: List[str] = []
    for m in _MODEL_PATTERN.finditer(text):
        name = m.group(1).lower().strip()
        if name and name not in seen and len(name) >= 3:
            seen.add(name)
            found.append(m.group(1).strip())
    return found


def _scan_variables(text: str) -> List[str]:
    """Extract UPPER_CASE constant/variable references."""
    seen: set = set()
    found: List[str] = []
    for m in _VAR_PATTERN.finditer(text):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            found.append(name)
    return found


def _scan_services(text: str) -> List[str]:
    """Extract systemd unit references (name.service, name.timer)."""
    seen: set = set()
    found: List[str] = []
    for m in _SERVICE_PATTERN.finditer(text):
        name = m.group(1).strip().lower()
        if name and name not in seen:
            seen.add(name)
            found.append(m.group(1).strip())
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
    """Get entities (all types) from same conversation's previous enrich_meta."""
    result: Dict[str, List[str]] = {
        "files": [], "functions": [], "classes": [],
        "libraries": [], "models": [], "variables": [], "services": [],
    }
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
        seen_sets: Dict[str, set] = {k: set() for k in result}
        for row in rows:
            try:
                data = json.loads(row.get("evidence", "{}"))
            except json.JSONDecodeError:
                continue
            entities = data.get("entities", data)
            for key in list(result.keys()):
                vals = entities.get(key, [])
                for v in vals:
                    v = str(v).strip()
                    if isinstance(v, dict):
                        v = v.get("name", str(v))
                    if v and v not in seen_sets[key]:
                        seen_sets[key].add(v)
                        result[key].append(v)
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
    regex_classes = _scan_classes(combined)
    regex_libs = _scan_imports(combined)
    regex_models = _scan_model_names(combined)
    regex_vars = _scan_variables(combined)
    regex_services = _scan_services(combined)
    registry_files = _lookup_file_registry(combined)
    conv_id = turn.get("conversation_id", "")
    turn_id = turn.get("id", "")
    conv_ents = _get_conversation_entities(conv_id, turn_id) if conv_id else {}

    def _dedup(base: List[str], extra: List[str]) -> List[str]:
        seen = set(base)
        return list(dict.fromkeys(base + [x for x in extra if x not in seen]))[:20]

    return {
        "files": _dedup(regex_files, registry_files + conv_ents.get("files", [])),
        "functions": _dedup(regex_funcs, conv_ents.get("functions", [])),
        "classes": _dedup(regex_classes, conv_ents.get("classes", [])),
        "libraries": _dedup(regex_libs, conv_ents.get("libraries", [])),
        "models": _dedup(regex_models, conv_ents.get("models", [])),
        "variables": _dedup(regex_vars, conv_ents.get("variables", [])),
        "services": _dedup(regex_services, conv_ents.get("services", [])),
        "_meta": {
            "regex_files": len(regex_files),
            "regex_funcs": len(regex_funcs),
            "regex_classes": len(regex_classes),
            "regex_libs": len(regex_libs),
            "regex_models": len(regex_models),
            "regex_vars": len(regex_vars),
            "regex_services": len(regex_services),
            "registry_files": len(registry_files),
            "conv_files": len(conv_ents.get("files", [])),
            "conv_funcs": len(conv_ents.get("functions", [])),
            "conv_classes": len(conv_ents.get("classes", [])),
            "conv_libs": len(conv_ents.get("libraries", [])),
            "conv_models": len(conv_ents.get("models", [])),
            "conv_vars": len(conv_ents.get("variables", [])),
            "conv_services": len(conv_ents.get("services", [])),
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
        "AND t.pipeline_state = 'embedded' "
        "ORDER BY t.est_chars ASC NULLS LAST, t.created_at DESC "
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
    totals = {"files": 0, "functions": 0, "classes": 0,
              "libraries": 0, "models": 0, "variables": 0, "services": 0}

    print(f"\n{'=' * 60}")
    print("Entity Scan — Phase 0: deterministic extraction (no LLM)")
    print("  Types: file, func, class, library, model, variable, service")
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
        meta = result.get("_meta", {})

        # Build concise per-turn summary
        parts = []
        for t in totals:
            n = len(result.get(t, []))
            if n:
                parts.append(f"{n} {t}")
        print(
            f"  [{ti}/{len(turns)}] {turn['id'][:8]} — "
            f"{', '.join(parts)} "
            f"(rx_f={meta.get('regex_files',0)} "
            f"cl={meta.get('regex_classes',0)} "
            f"lib={meta.get('regex_libs',0)} "
            f"mdl={meta.get('regex_models',0)} "
            f"var={meta.get('regex_vars',0)} "
            f"svc={meta.get('regex_services',0)})",
            flush=True,
        )
        if dry_run:
            processed += 1
            for t in totals:
                totals[t] += len(result.get(t, []))
            continue

        ok = _insert_scan(turn["id"], result)
        if ok:
            psql_ok(f"UPDATE turns SET pipeline_state = 'scanned' WHERE id = '{turn['id']}'::uuid")
            processed += 1
            heartbeat("entity_scan", f"turn {turn['id'][:8]} — {', '.join(parts)}")
            for t in totals:
                totals[t] += len(result.get(t, []))
        else:
            failed += 1

    elapsed = round(time.monotonic() - t_start, 1)
    parts_summary = ", ".join(f"{totals[t]} {t}" for t in totals if totals[t])
    print(f"\n{'=' * 60}")
    print(
        f"Done: {processed} scanned, {failed} failed — "
        f"{parts_summary} ({elapsed}s)"
    )
    print(f"{'=' * 60}")

    return {
        "processed": processed,
        "failed": failed,
        **totals,
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
