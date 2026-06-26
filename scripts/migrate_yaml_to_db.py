#!/usr/bin/env python3
"""One-time migration: parse broken handover.yaml and import to DB.

The handover.yaml has YAML single-quote escaping issues (unescaped ' in detail
texts). This script uses regex to extract id/detail pairs directly from raw text,
bypassing yaml.safe_load.
"""

import ast
import re
import sys
from pathlib import Path
from lib.db import psql, psql_json, esc_sql

HANDOVER = Path("/opt/projects/server/handover.yaml")


def _yaml_unescape(s: str) -> str:
    """Unescape YAML single-quoted scalar: '' -> '."""
    return s.replace("''", "'")


def _py_unescape(s: str) -> str:
    """Unescape Python string literal escapes like \\'."""
    return s.replace("\\'", "'")


def try_ast_parse(raw: str):
    """Try ast.literal_eval on a Python dict repr string."""
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return None


def parse_decision_line(line: str):
    """Parse a '- '{...}' line from YAML using regex."""
    m = re.match(r"^- '(.*)'\s*$", line)
    if not m:
        return None
    inner = m.group(1)  # the raw string between outer YAML single quotes

    # Step 1: try direct ast parse after YAML unescape
    unescaped = _yaml_unescape(inner)
    d = try_ast_parse(unescaped)
    if d and isinstance(d, dict) and "id" in d:
        return {"id": str(d["id"]), "detail": str(d.get("detail", ""))}

    # Step 2: try with Python unescape too
    unescaped2 = _py_unescape(unescaped)
    d = try_ast_parse(unescaped2)
    if d and isinstance(d, dict) and "id" in d:
        return {"id": str(d["id"]), "detail": str(d.get("detail", ""))}

    # Step 3: regex extraction from raw inner (bypassing ast)
    id_m = re.search(r"''id'':\s*''([^']+)''", inner)
    detail_m = re.search(r"''detail'':\s*''(.*?)''\s*\}", inner, re.DOTALL)
    if id_m:
        decision_id = id_m.group(1)
        detail = detail_m.group(1) if detail_m else inner
        detail = _py_unescape(_yaml_unescape(detail))
        return {"id": decision_id, "detail": detail}

    # Step 4: last resort - heuristically find id and capture everything after as detail
    id_m2 = re.search(r"id:\s*'([^']+)'", inner.replace("''", "'"))
    if id_m2:
        return {"id": id_m2.group(1), "detail": inner}

    return None


def parse_issue_line(line: str):
    """Parse known_issues lines the same way."""
    return parse_decision_line(line)


def main():
    text = HANDOVER.read_text()

    # Split into sections by top-level keys
    sections = {}
    current_section = None
    current_lines = []
    for line in text.split("\n"):
        if re.match(r"^[a-z][a-z_]+:$", line) and not line.startswith(" "):
            if current_section:
                sections[current_section] = current_lines
            current_section = line.rstrip(":")
            current_lines = []
        elif current_section is not None:
            current_lines.append(line)
    if current_section:
        sections[current_section] = current_lines

    print(f"Sections found: {list(sections.keys())}")

    # Wipe old checkpoint data (decisions/issues/logs are bound to old checkpoints)
    # Create a fresh checkpoint from current YAML content
    checkpoint_id = _import_checkpoint(text, sections)
    if not checkpoint_id:
        print("ERROR: checkpoint creation failed")
        return 1

    # Import decisions
    dec_count = _import_decisions(checkpoint_id, sections.get("decisions", []))
    iss_count = _import_issues(checkpoint_id, sections.get("known_issues", []))
    log_count = _import_log(checkpoint_id, sections.get("completed_log", []))

    print(f"Imported: {dec_count} decisions, {iss_count} issues, {log_count} logs (cp#{checkpoint_id})")

    # Regenerate clean YAML from DB
    _regenerate_yaml(checkpoint_id)

    # Backup corrupted file
    backup = HANDOVER.with_suffix(".yaml.bak")
    HANDOVER.rename(backup)
    print(f"Corrupted file backed up to {backup}")

    return 0


def _import_checkpoint(text, sections):
    """Create a session checkpoint from the YAML's last_checkpoint data."""
    # Extract time from last_checkpoint section
    cp_lines = sections.get("last_checkpoint", [])
    cp_time = None
    cp_summary = ""
    for line in cp_lines:
        m = re.match(r"^\s+time:\s*'(.+)'", line)
        if m:
            cp_time = m.group(1)
        m = re.match(r"^\s+summary:\s*(.+)", line)
        if m:
            cp_summary = m.group(1).strip().strip("'").strip('"')

    if not cp_time:
        import datetime
        cp_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

    summary = esc_sql(cp_summary[:200])
    sql = f"""INSERT INTO session_checkpoints (summary, total_files, recent_files)
    VALUES ('{summary}', 0, '{{}}'::jsonb)
    RETURNING id"""
    result = psql(sql)
    if not result or not result.strip():
        return None
    return int(result.strip())


def _import_decisions(checkpoint_id, lines):
    count = 0
    for line in lines:
        if not line.startswith("- '"):
            continue
        parsed = parse_decision_line(line)
        if not parsed:
            continue
        did = esc_sql(parsed["id"])
        dtl = esc_sql(parsed["detail"][:5000])
        exists = psql_json(f"SELECT 1 FROM decisions WHERE decision_id = '{did}' LIMIT 1")
        if not exists:
            psql(
                f"INSERT INTO decisions (checkpoint_id, decision_id, detail, status) "
                f"VALUES ({checkpoint_id}, '{did}', '{dtl}', 'open')"
            )
            count += 1
    return count


def _import_issues(checkpoint_id, lines):
    count = 0
    for line in lines:
        if not line.startswith("- '"):
            continue
        parsed = parse_issue_line(line)
        if not parsed:
            continue
        it = esc_sql(parsed["detail"][:5000])
        exists = psql_json(f"SELECT 1 FROM known_issues WHERE issue_text = '{it}' LIMIT 1")
        if not exists:
            psql(
                f"INSERT INTO known_issues (checkpoint_id, issue_text, resolved) "
                f"VALUES ({checkpoint_id}, '{it}', false)"
            )
            count += 1
    return count


def _import_log(checkpoint_id, lines):
    count = 0
    for line in lines:
        if not line.startswith("- '"):
            continue
        content = re.sub(r"^- '(.*)'\s*$", r"\1", line)
        content = content.replace("''", "'")
        lt = esc_sql(content[:5000])
        psql(
            f"INSERT INTO completed_log (checkpoint_id, log_text) "
            f"VALUES ({checkpoint_id}, '{lt}')"
        )
        count += 1
    return count


def _regenerate_yaml(checkpoint_id):
    """Generate clean structured YAML from DB data."""
    import yaml

    cp = psql_json(f"SELECT * FROM session_checkpoints WHERE id = {checkpoint_id} LIMIT 1")
    if not cp:
        return
    cp = cp[0]

    decisions = psql_json(
        f"SELECT decision_id, detail, status FROM decisions "
        f"WHERE checkpoint_id = {checkpoint_id} AND (status IS NULL OR status != 'archived') ORDER BY id"
    )
    issues = psql_json(
        f"SELECT issue_text, resolved FROM known_issues "
        f"WHERE checkpoint_id = {checkpoint_id} AND NOT resolved ORDER BY id"
    )
    completed = psql_json(
        f"SELECT log_text FROM completed_log WHERE checkpoint_id = {checkpoint_id} ORDER BY id"
    )

    def _fmt_dec(d):
        return {"id": d["decision_id"], "detail": d["detail"], "status": d.get("status", "open")}

    def _fmt_iss(i):
        return {"text": i["issue_text"], "resolved": i["resolved"]}

    data = {
        "last_checkpoint": {
            "time": str(cp["created_at"]),
            "summary": cp.get("summary", ""),
            "total_files": cp.get("total_files", 0),
            "recent_files": cp.get("recent_files", {}),
            "git": cp.get("git_state", {}),
            "task": cp.get("task"),
        },
        "decisions": [_fmt_dec(d) for d in decisions],
        "known_issues": [_fmt_iss(i) for i in issues],
        "completed_log": [l["log_text"] for l in completed],
    }

    Path("/opt/projects/server/handover.yaml").write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120)
    )
    print(f"  Regenerated clean handover.yaml from cp#{checkpoint_id}")


if __name__ == "__main__":
    sys.exit(main())
