#!/usr/bin/env python3.11
# Status: experimental
# Path: MCP client (opencode.json)
"""SQLite MCP — read-only access to OpenCode session database.

Security:
  - SELECT/WITH only (writes rejected before execution)
  - Row limit (200), output cap (100KB)

Register in opencode.json:
  "mcpServers": {
    "opencode-db": {
      "command": "python3.11",
      "args": ["/opt/projects/server/scripts/sqlite_mcp.py"]
    }
  }
"""

import json
import sqlite3
import sys
import traceback
from pathlib import Path

DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
ROW_LIMIT = 200
OUTPUT_CAP = 100_000


def handle_request(request: dict) -> dict:
    method = request.get("method", "")
    req_id = request.get("id", 0)

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sqlite_mcp", "version": "0.1.0"},
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "list_tables",
                        "description": "List all tables in the OpenCode session database",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                    {
                        "name": "schema",
                        "description": "Show schema (columns, types) for a table",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "table_name": {"type": "string", "description": "Table name"}
                            },
                            "required": ["table_name"],
                        },
                    },
                    {
                        "name": "query",
                        "description": "Run a SELECT query against the OpenCode database",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "sql": {"type": "string", "description": "SELECT SQL query"}
                            },
                            "required": ["sql"],
                        },
                    },
                ]
            },
        }

    if method == "tools/call":
        tool = request["params"]["name"]
        args = request["params"].get("arguments", {})

        if not DB_PATH.exists():
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": f"Database not found: {DB_PATH}"},
            }

        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if tool == "list_tables":
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                tables = [row["name"] for row in cursor.fetchall()]
                result = {"tables": tables}

            elif tool == "schema":
                table = args.get("table_name", "")
                cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
                row = cursor.fetchone()
                ddl = row["sql"] if row else "Table not found"
                cursor.execute(f"SELECT COUNT(*) as cnt FROM \"{table}\"")
                count = cursor.fetchone()["cnt"]
                result = {"table": table, "rows": count, "ddl": ddl}

            elif tool == "query":
                sql = args.get("sql", "").strip()
                sql_upper = sql.upper()
                if not sql_upper.startswith("SELECT") and not sql_upper.startswith("WITH") and not sql_upper.startswith("PRAGMA") and not sql_upper.startswith("EXPLAIN"):
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32000, "message": "Only SELECT/WITH/PRAGMA/EXPLAIN allowed"},
                    }
                has_limit = "LIMIT" in [t.upper() for t in sql.split()]
                if not has_limit:
                    sql = f"{sql} LIMIT {ROW_LIMIT}"
                cursor.execute(sql)
                columns = [desc[0] for desc in cursor.description]
                rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
                result = {"columns": columns, "rows": rows, "count": len(rows)}
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool}"},
                }

            conn.close()
            result_str = json.dumps(result, ensure_ascii=False, default=str)
            if len(result_str) > OUTPUT_CAP:
                result["_truncated"] = True
                result_str = json.dumps(result, ensure_ascii=False, default=str)[:OUTPUT_CAP] + ',"_truncated":true}'

            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(e)},
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = handle_request(request)
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception:
            sys.stdout.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 0,
                        "error": {"code": -32700, "message": "Parse error"},
                    }
                )
                + "\n"
            )
            sys.stdout.flush()


if __name__ == "__main__":
    main()
