#!/usr/bin/env python3.11
# Status: production
# Path: imported by — gemini.py, gemini_mcp.py
"""Core engine: keys, API calls, tool implementations."""

import html, json, os, random, re, shlex, ssl, subprocess, sys, time, urllib.parse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from ddgs import DDGS

API_BASE = "https://generativelanguage.googleapis.com:4430/v1beta/models"
DEFAULT_MODEL = "gemini-2.5-flash"
STATE_FILE = os.path.expanduser("~/.cache/devforge/gemini_rotator_state.json")
SECRETS = os.path.expanduser("~/.config/devforge/secrets.env")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


# ── Key loading / rotation ─────────────────────────────────────────

def load_keys():
    keys = []
    if os.path.exists(SECRETS):
        with open(SECRETS) as f:
            for line in f:
                line = line.strip()
                if line.startswith("GEMINI_API_KEYS="):
                    raw = line.split("=", 1)[1].strip().strip("\"'")
                    for part in raw.split(","):
                        if ":" in part:
                            name, key = part.split(":", 1)
                            keys.append((name.strip(), key.strip()))
    return keys


def pick_key(keys):
    state = {"calls": {}, "fails": {}, "backoff_until": {}}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
        except Exception:
            pass
    now = time.time()
    candidates = []
    for i, (name, key) in enumerate(keys):
        bu = state.get("backoff_until", {}).get(str(i), 0)
        if now >= bu:
            candidates.append((i, name, key))
    if not candidates:
        idx = min(range(len(keys)), key=lambda i: state.get("backoff_until", {}).get(str(i), 0))
        candidates = [(idx, keys[idx][0], keys[idx][1])]
    idx = random.choice(range(len(candidates)))
    i, name, key = candidates[idx]
    state["calls"][str(i)] = state.get("calls", {}).get(str(i), 0) + 1
    if state["calls"].get(str(i), 0) >= 50:
        state["backoff_until"][str(i)] = now + 3600
        state["calls"][str(i)] = 0
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(state, f)
    os.rename(STATE_FILE + ".tmp", STATE_FILE)
    return name, key


def _brave_keys():
    if not os.path.exists(SECRETS):
        return []
    with open(SECRETS) as f:
        for line in f:
            line = line.strip()
            if line.startswith("BRAVE_API_KEYS="):
                raw = line.split("=", 1)[1].strip().strip("\"'")
                keys = []
                for part in raw.split(","):
                    if ":" in part:
                        keys.append(part.split(":", 1)[1])
                return keys
    return []


_BRAVE_STATE_FILE = os.path.expanduser("~/.cache/devforge/brave_rotator_state.json")


def _brave_pick_key(keys):
    state = {}
    if os.path.exists(_BRAVE_STATE_FILE):
        try:
            with open(_BRAVE_STATE_FILE) as f:
                state = json.load(f)
        except Exception:
            pass
    idx = state.get("idx", 0) % len(keys)
    state["idx"] = (idx + 1) % len(keys)
    os.makedirs(os.path.dirname(_BRAVE_STATE_FILE), exist_ok=True)
    with open(_BRAVE_STATE_FILE + ".tmp", "w") as f:
        json.dump(state, f)
    os.rename(_BRAVE_STATE_FILE + ".tmp", _BRAVE_STATE_FILE)
    return keys[idx], idx


# ── API call ───────────────────────────────────────────────────────

def call_gemini(contents, tools=None):
    """Single round-trip to Gemini API. Returns response dict or None on failure."""
    body = {"contents": contents}
    if tools:
        body["tools"] = tools
    data = json.dumps(body).encode()
    url = f"{API_BASE}/{DEFAULT_MODEL}:generateContent"
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urlopen(req, context=_CTX, timeout=120) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            if e.code == 502 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            err_body = e.read().decode(errors="replace")
            return {"error": f"API error {e.code}: {err_body[:300]}"}
        except URLError as e:
            return {"error": f"Network error: {e.reason}"}
    return {"error": "502 after 3 retries"}


# ── Tool definitions (function calling schema) ─────────────────────

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

SAFE_PREFIXES = (
    "grep ", "ls ", "cat ", "ps ", "ss ", "uptime ", "free ", "df ", "date ",
    "podman ps", "podman exec", "podman logs",
    "systemctl --user", "pg_isready", "journalctl --user",
    "python3 /opt/projects/server/scripts/cli.py",
)
SAFE_DIR = "/opt/projects/server"


def _safe_path(path):
    full = os.path.realpath(os.path.normpath(path))
    safe = os.path.realpath(SAFE_DIR)
    if not full.startswith(safe + "/") and full != safe:
        raise ValueError(f"Path must be under {SAFE_DIR}/, got: {full}")
    return full


def _strip_html(text):
    """Crude HTML-to-text: strip tags, decode entities, collapse whitespace."""
    text = html.unescape(text)
    while True:
        idx = text.lower().find("<script")
        if idx == -1:
            break
        end = text.find("</script>", idx)
        if end == -1:
            end = len(text)
        text = text[:idx] + text[end + 9:]
    while True:
        idx = text.lower().find("<style")
        if idx == -1:
            break
        end = text.find("</style>", idx)
        if end == -1:
            end = len(text)
        text = text[:idx] + text[end + 8:]
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def execute_tool(name, args):
    """Execute a tool and return its result string."""
    if name == "get_system_status":
        result = subprocess.run(
            ["python3", "/opt/projects/server/scripts/cli.py", "status", "--json"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            summary = {
                "host": data.get("host", {}),
                "containers": {k: v.get("status", "?") for k, v in data.get("containers", {}).items()},
                "services": {k: v.get("state", "?") for k, v in data.get("services", {}).items()},
                "models": data.get("models", {}),
                "resources": data.get("resources", {}),
                "alerts": data.get("alerts", []),
            }
            return json.dumps(summary, indent=2)
        return f"Error: {result.stderr[:500]}"

    elif name == "get_task_list":
        result = subprocess.run(
            ["python3", "/opt/projects/server/scripts/cli.py", "task", "list"],
            capture_output=True, text=True, timeout=15
        )
        return result.stdout or result.stderr or "No tasks"

    elif name == "run_db_query":
        query = args.get("query", "")
        query_upper = query.strip().upper()
        if not query_upper.startswith("SELECT"):
            return "Error: Only SELECT queries are allowed."
        result = subprocess.run(
            ["podman", "exec", "postgres", "psql", "-U", "devforge", "-d", "devforge_app",
             "-c", query, "--csv" if "COUNT" not in query_upper else "-c", query],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return result.stdout or "(empty)"
        return f"DB error: {result.stderr[:500]}"

    elif name == "get_container_logs":
        target = args.get("target", "")
        lines = args.get("lines", 20)
        result = subprocess.run(
            ["podman", "logs", "--tail", str(lines), target],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return result.stdout[-2000:] or "(empty)"
        result = subprocess.run(
            ["journalctl", "--user", "-u", target, "--no-pager", "-n", str(lines)],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return result.stdout[-2000:] or "(empty)"
        return f"Error: {result.stderr[:300]}"

    elif name == "get_timer_status":
        result = subprocess.run(
            ["systemctl", "--user", "list-timers", "--all", "--no-pager"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            lines = [l for l in result.stdout.split("\n") if "devforge" in l.lower()]
            return "\n".join(lines) if lines else result.stdout[:1500]
        return f"Error: {result.stderr[:300]}"

    elif name == "run_shell":
        cmd = args.get("command", "").strip()
        if not any(cmd.startswith(p) for p in SAFE_PREFIXES):
            return f"Error: Command not in safe list."
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        return result.stdout[-2000:] or result.stderr[-2000:] or "(empty)"

    elif name == "read_file":
        path = args.get("path", "")
        try:
            full = _safe_path(path)
        except ValueError as e:
            return f"Error: {e}"
        if not os.path.isfile(full):
            return f"Error: File not found: {full}"
        limit = min(args.get("limit", 100), 500)
        offset = args.get("offset", 0)
        with open(full) as f:
            lines = f.readlines()
        selected = lines[offset:offset + limit]
        return "".join(selected) or "(empty)"

    elif name == "edit_file":
        path = args.get("path", "")
        old = args.get("old_string", "")
        new = args.get("new_string", "")
        if not old:
            return "Error: old_string is required"
        try:
            full = _safe_path(path)
        except ValueError as e:
            return f"Error: {e}"
        if not os.path.isfile(full):
            return f"Error: File not found: {full}"
        with open(full) as f:
            content = f.read()
        count = content.count(old)
        if count == 0:
            return "Error: old_string not found in file"
        if count > 1:
            return f"Error: old_string appears {count} times (must be unique)"
        content = content.replace(old, new)
        with open(full, "w") as f:
            f.write(content)
        return f"Edited {full} — 1 replacement made."

    elif name == "write_file":
        path = args.get("path", "")
        content = args.get("content", "")
        try:
            full = _safe_path(path)
        except ValueError as e:
            return f"Error: {e}"
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return f"Written {len(content)} bytes to {full}"

    elif name == "web_search":
        query = args.get("query", "")
        count = min(args.get("count", 5), 10)
        keys = _brave_keys()
        if not keys:
            return "Error: No Brave Search API keys found in secrets.env"
        params = urllib.parse.urlencode({"q": query, "count": count})
        last_err = None
        for attempt in range(len(keys)):
            key, _ = _brave_pick_key(keys)
            req = Request(
                f"https://api.search.brave.com/res/v1/web/search?{params}",
                headers={"Accept": "application/json", "Accept-Encoding": "gzip", "X-Subscription-Token": key}
            )
            try:
                with urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                results = data.get("web", {}).get("results", [])
                if not results:
                    return "No results found."
                lines = []
                for i, r in enumerate(results[:count], 1):
                    title = r.get("title", "?")
                    url = r.get("url", "?")
                    desc = r.get("description", "")
                    lines.append(f"{i}. [{title}]({url})")
                    if desc:
                        lines.append(f"   {desc[:200]}")
                return "\n".join(lines)
            except HTTPError as e:
                last_err = f"Search error: {e.code}"
                if e.code != 422:
                    break
            except Exception as e:
                last_err = f"Search error: {e}"
                break
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=count))
            if not results:
                return "No results found (DuckDuckGo fallback)."
            lines = []
            for i, r in enumerate(results[:count], 1):
                title = r.get("title", "?")
                url = r.get("href", r.get("link", "?"))
                desc = r.get("body", r.get("snippet", ""))
                lines.append(f"{i}. [{title}]({url})")
                if desc:
                    lines.append(f"   {desc[:200]}")
            return "\n".join(lines)
        except Exception as ddg_err:
            return f"Error: Brave exhausted, DuckDuckGo also failed: {ddg_err}"

    elif name == "fetch_url":
        url = args.get("url", "")
        max_chars = min(args.get("max_chars", 5000), 20000)
        if not url.startswith(("http://", "https://")):
            return "Error: URL must start with http:// or https://"
        req = Request(url, headers={"User-Agent": "DevForge/1.0"})
        try:
            with urlopen(req, context=_CTX, timeout=20) as resp:
                raw = resp.read()
        except HTTPError as e:
            return f"Fetch error {e.code}: {e.read().decode(errors='replace')[:200]}"
        except Exception as e:
            return f"Fetch error: {e}"
        content_type = resp.headers.get("Content-Type", "")
        if "text" not in content_type and "html" not in content_type and "json" not in content_type:
            return f"Unsupported content type: {content_type}"
        text = _strip_html(raw.decode(errors="replace")).strip()
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... [truncated, total was {len(text)} chars]"
        return text or "(empty page)"

    return f"Error: Unknown tool '{name}'"
