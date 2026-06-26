#!/usr/bin/env python3.11
# Status: production
"""Gemini function-calling tool definitions."""

TOOLS = [
    {
        "functionDeclarations": [
            {
                "name": "get_system_status",
                "description": "Get live system status including containers, timers, services, models, and resource usage.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "get_task_list",
                "description": "Get current/pending task list from the DB-backed task manager.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "run_db_query",
                "description": "Execute a read-only SQL query on the devforge_app PostgreSQL database. Returns JSON results.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "SQL query (SELECT only)"}
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "get_container_logs",
                "description": "Get recent logs from a podman container or systemd service.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "Container or service name"},
                        "lines": {"type": "integer", "description": "Number of lines (default 20)", "default": 20}
                    },
                    "required": ["target"],
                },
            },
            {
                "name": "get_timer_status",
                "description": "List active and inactive systemd timers, filtered to devforge timers.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "run_shell",
                "description": "Run a read-only safe shell command. Allowed: grep, ls, cat, ps, ss, journalctl, podman, systemctl --user, uptime, free, df, python3 cli.py.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command"}
                    },
                    "required": ["command"],
                },
            },
            {
                "name": "read_file",
                "description": "Read file contents. Only works within /opt/projects/server/.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path under /opt/projects/server/"},
                        "limit": {"type": "integer", "description": "Max lines (default 100, max 500)"},
                        "offset": {"type": "integer", "description": "Start line (default 0)"}
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "edit_file",
                "description": "Replace text in a file. old_string must be unique. Only works within /opt/projects/server/.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path"},
                        "old_string": {"type": "string", "description": "Exact text to find (must appear once)"},
                        "new_string": {"type": "string", "description": "Replacement text"}
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            },
            {
                "name": "write_file",
                "description": "Create or overwrite a file. Only works within /opt/projects/server/.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path"},
                        "content": {"type": "string", "description": "Full file content"}
                    },
                    "required": ["path", "content"],
                },
            },
            {
                "name": "web_search",
                "description": "Search the web using Brave Search API (with DuckDuckGo fallback). Returns title, URL, description.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "count": {"type": "integer", "description": "Results to return (1-10, default 5)"}
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "fetch_url",
                "description": "Fetch a web page and return its text content.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to fetch"},
                        "max_chars": {"type": "integer", "description": "Max chars (default 5000, max 20000)"}
                    },
                    "required": ["url"],
                },
            },
        ]
    }
]
