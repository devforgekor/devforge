#!/usr/bin/env python3.11
# Status: production
"""Tool execution: run_shell, read_file, edit_file, web_search, etc."""

import html, json, os, re, shlex, ssl, subprocess, urllib.parse
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from ddgs import DDGS

from gemini_core.keys import _brave_keys, _brave_pick_key

SAFE_PREFIXES = (
    "grep ", "ls ", "cat ", "ps ", "ss ", "uptime ", "free ", "df ", "date ",
    "podman ps", "podman exec", "podman logs",
    "systemctl --user", "pg_isready", "journalctl --user",
    "python3 /opt/projects/server/scripts/cli.py",
)
SAFE_DIR = "/opt/projects/server"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _safe_path(path):
    full = os.path.realpath(os.path.normpath(path))
    safe = os.path.realpath(SAFE_DIR)
    if not full.startswith(safe + "/") and full != safe:
        raise ValueError(f"Path must be under {SAFE_DIR}/, got: {full}")
    return full


def _strip_html(text):
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
