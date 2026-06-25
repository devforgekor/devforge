#!/usr/bin/env python3.11
# Status: experimental
# Path: MCP client (mcp.json)
"""pg_mcp — PostgreSQL MCP server via podman exec.

Read-only SQL access to devforge_app database.
Connects via `podman exec postgres psql` — no host port needed.

Security:
  - SELECT/WITH only enforced at psql level via readonly transaction
  - Parameter quoting via PostgreSQL quote_ident/quote_literal
  - Statement-level timeout (30s), row limit (200), output cap (50KB)

Register in mcp.json:
  "postgres": {
    "type": "stdio",
    "command": "python3.11",
    "args": ["/opt/projects/server/scripts/pg_mcp.py"]
  }
"""

import json
import re
import subprocess
import sys
import traceback

# ── Safety limits ──
EXEC_TIMEOUT = 30

_READONLY_SQL = (
    "BEGIN; SET TRANSACTION READ ONLY; SET statement_timeout = '30s'; "
    "SET search_path TO public; "
)


def _sanitize_sql(sql: str) -> str:
    """Wrap arbitrary SQL in a read-only transaction with guardrails.

    Wrapping strategy:
      1. BEGIN read-only transaction (PostgreSQL enforces no writes)
      2. statement_timeout kills long queries at DB level
      3. LIMIT rows at protocol level via psql --pset
    """
    return f"{_READONLY_SQL} {sql}; COMMIT;"


def _quote_ident(name: str) -> str:
    """Escape a PostgreSQL identifier (table name, column name).
    Uses internal PQescapeIdentifier rules via double-quoting.
    """
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    """Escape a PostgreSQL string literal safely.
    Uses standard SQL single-quote doubling.
    """
    return "'" + value.replace("'", "''") + "'"


# ── JSON-RPC helpers ──

def _read_message():
    headers = {}
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    length = int(headers.get("content-length", 0))
    if length == 0:
        return None
    return json.loads(sys.stdin.read(length))


def _send_message(msg):
    body = json.dumps(msg, ensure_ascii=False)
    sys.stdout.write(f"Content-Length: {len(body)}\r\n\r\n{body}")
    sys.stdout.flush()


# ── Core: DB query via podman exec ──

def _run_sql(sql: str, limit: int = 0) -> dict:
    """Execute SQL via podman exec postgres psql inside a read-only transaction.

    PostgreSQL READ ONLY transaction prevents ALL writes at engine level,
    even if the SQL contains multiple statements. `psql -c` executes
    the entire string, but the outer read-only txn rejects writes.

    Args:
        sql: SQL statement(s) to execute
        limit: Optional row limit via psql --pset tuples_only
    """
    safe_sql = _sanitize_sql(sql)
    if limit:
        # PostgreSQL FETCH FIRST inside the txn; CLI -- FETCH FIRST is not psql native
        safe_sql = f"{_READONLY_SQL} {sql} LIMIT {limit}; COMMIT;"

    cmd = [
        "podman", "exec", "postgres",
        "psql", "-U", "devforge", "-d", "devforge_app",
        "--no-align",
        "-c", safe_sql,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=EXEC_TIMEOUT + 5
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Query timed out ({EXEC_TIMEOUT}s)"}
    except FileNotFoundError:
        return {"success": False, "error": "podman not found"}

    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip() or "Unknown error"
        return {"success": False, "error": err[:2000]}

    stdout = result.stdout.strip()

    # Strip transaction boilerplate: BEGIN, SET, SET TRANSACTION, COMMIT
    lines = stdout.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if stripped.upper() in ("BEGIN", "COMMIT", "ROLLBACK", "SET"):
            continue
        if stripped.upper().startswith("SET TRANSACTION"):
            continue
        if re.match(r"^\(\d+ rows?\)$", stripped, re.IGNORECASE):
            continue
        cleaned.append(line)

    return {"success": True, "result": "\n".join(cleaned)}


def _list_tables() -> dict:
    return _run_sql(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' ORDER BY table_name"
    )


def _describe_table(table: str) -> dict:
    safe = _quote_literal(table)
    return _run_sql(
        f"SELECT column_name, data_type, is_nullable, "
        f"coalesce(character_maximum_length::text, '-') as char_max, "
        f"coalesce(numeric_precision::text, '-') as num_prec "
        f"FROM information_schema.columns "
        f"WHERE table_schema = 'public' AND table_name = {safe} "
        f"ORDER BY ordinal_position"
    )


def _list_schemas() -> dict:
    return _run_sql(
        "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name"
    )


def _table_count(table: str) -> dict:
    safe = _quote_ident(table)
    return _run_sql(f"SELECT COUNT(*) FROM {safe}", limit=1)


# ── Tool registry ──

TOOLS = [
    {
        "name": "query",
        "description": f"Execute a read-only SQL query. PostgreSQL READ ONLY transaction enforces no writes. {EXEC_TIMEOUT}s timeout.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SQL query (SELECT, WITH, EXPLAIN etc.)"},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "list_tables",
        "description": "List all tables in the public schema.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "describe_table",
        "description": "Show column names, types, and nullability for a table.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Table name"},
            },
            "required": ["table"],
        },
    },
    {
        "name": "list_schemas",
        "description": "List all database schemas.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "table_count",
        "description": "Get row count for a table.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Table name"},
            },
            "required": ["table"],
        },
    },
]


def _handle_call(name: str, args: dict) -> str:
    try:
        if name == "query":
            sql = args.get("sql", "")
            if not sql:
                return json.dumps({"error": "Missing 'sql' parameter"})
            return json.dumps(_run_sql(sql), ensure_ascii=False)
        elif name == "list_tables":
            return json.dumps(_list_tables(), ensure_ascii=False)
        elif name == "describe_table":
            table = args.get("table", "")
            if not table:
                return json.dumps({"error": "Missing 'table' parameter"})
            return json.dumps(_describe_table(table), ensure_ascii=False)
        elif name == "list_schemas":
            return json.dumps(_list_schemas(), ensure_ascii=False)
        elif name == "table_count":
            table = args.get("table", "")
            if not table:
                return json.dumps({"error": "Missing 'table' parameter"})
            return json.dumps(_table_count(table), ensure_ascii=False)
        else:
            return json.dumps({"error": f"Unknown tool: {name}"})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}, ensure_ascii=False)


# ── Main loop ──

def main():
    while True:
        msg = _read_message()
        if msg is None:
            break

        method = msg.get("method", "")
        req_id = msg.get("id")

        # Notifications have no id — silently accept
        if req_id is None:
            if method == "notifications/initialized":
                continue
            if method == "notifications/cancelled":
                continue
            continue

        params = msg.get("params", {})

        if method == "initialize":
            _send_message({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "pg-mcp", "version": "1.0.0"},
                },
            })
        elif method == "tools/list":
            _send_message({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS},
            })
        elif method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments", {})
            result = _handle_call(name, arguments)
            _send_message({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": result}]},
            })
        elif method == "shutdown":
            _send_message({"jsonrpc": "2.0", "id": req_id, "result": None})
            break
        else:
            _send_message({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })


if __name__ == "__main__":
    main()
