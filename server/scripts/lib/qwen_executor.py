"""qwen_executor.py — context gathering, action execution, and verification for qwen_worker."""

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from .agents import AGENT_MAP
from .parser_claude import parse as parse_claude
from .parser_copilot import parse as parse_copilot
from .parser_gemini import parse as parse_gemini
from .parser_qwen import parse as parse_qwen

PARSERS = {
    "claude": parse_claude,
    "copilot": parse_copilot,
    "gemini": parse_gemini,
    "qwen": parse_qwen,
}

CANONICAL_AGENTS = set(AGENT_MAP.values())

CLASSIFY_SYSTEM_PROMPT = (
    "You are a worklog classifier. Your ONLY task: infer the AI agent from a worklog title.\n"
    "Output ONLY a JSON object:\n"
    '{"classifications": [{"worklog_id": <int>, "agent": "<agent>"}]}\n\n'
    "Canonical agents: claude-code, copilot, gemini, qwen, deepseek\n\n"
    "Rules:\n"
    "- \"YYYY-MM-DD claude-code session\" → agent = \"claude-code\"\n"
    "- \"Copilot CLI statusline quota tracking\" → agent = \"copilot\"\n"
    "- If the title describes a specific AI tool's activity, use that agent.\n"
    "- If you cannot confidently determine the agent, do NOT include that worklog_id.\n"
    "- Do NOT invent agents. Only use the canonical names above.\n"
    "- Do NOT include any text outside the JSON object."
)

OBSERVE_SYSTEM_PROMPT = (
    "You are an intermediate dialogue observer. "
    "Your ONLY task is to extract explicit facts from conversation content.\n"
    "Output ONLY a JSON object:\n"
    '{"observations": [\n'
    '  {\n'
    '    "id": 1,\n'
    '    "timestamp": "<ISO8601 or null>",\n'
    '    "speaker": "<agent name>",\n'
    '    "fact_type": "<statement|decision|action_item|question|answer|data_given>",\n'
    '    "subject": "<agent or entity>",\n'
    '    "predicate": "<action or relation>",\n'
    '    "object": "<target or value>",\n'
    '    "evidence": "<verbatim quote from TURN CONTENT, max 200 chars>",\n'
    '    "entities": ["<key term>"],\n'
    '    "status": "<confirmed|tentative>"\n'
    '  }\n'
    ']}\n\n'
    "RULES:\n"
    "- Do NOT summarize. Do NOT infer. Do NOT interpret emotion or intent.\n"
    "- Only record facts explicitly present in the turn content below.\n"
    "- evidence MUST be verbatim from a turn line WITHOUT the [agent] label prefix.\n"
    '  Example: "[claude-code] user: hello" → evidence = "hello"\n'
    "- speaker must match the agent tag shown in the turn line exactly.\n"
    "- fact_type: \"statement\" for claims, \"decision\" for conclusions, "
    "\"action_item\" for tasks, \"question\" for queries, \"answer\" for responses, "
    "\"data_given\" for numbers/stats.\n"
    "- If no facts to extract, return empty observations array [].\n"
    "- Do NOT include any text outside the JSON object."
)

SESSION_DIRS = {
    "claude": Path("/home/opc/.claude/projects/-home-opc"),
    "copilot": Path("/home/opc/.copilot/session-state"),
    "gemini": Path("/home/opc/.gemini/tmp/opc/chats"),
    "qwen": Path("/home/opc/.qwen/projects"),
}

PSQL = [
    "podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
    "-d", "devforge_app", "--no-align", "--tuples-only", "--quiet",
]


def _psql(sql: str) -> str:
    try:
        r = subprocess.run(PSQL + ["-c", sql], capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception as e:
        print(f"DB error: {e}", file=sys.stderr)
        return ""


def _discover_sessions(source: str) -> List[Tuple[str, Path, Optional[str]]]:
    """Return [(session_id, path, optional_project), ...] for a source."""
    d = SESSION_DIRS.get(source)
    if not d or not d.exists():
        return []

    if source == "claude":
        return [(p.stem, p, None) for p in sorted(d.glob("*.jsonl"))]

    elif source == "copilot":
        return [(sd.name, sd / "events.jsonl", None)
                for sd in sorted(d.iterdir()) if sd.is_dir() and (sd / "events.jsonl").exists()]

    elif source == "gemini":
        paths = []
        for p in sorted(d.glob("session-*.jsonl")):
            try:
                header = json.loads(p.open().readline().strip())
                sid = header.get("sessionId", p.stem.replace("session-", ""))
            except Exception:
                sid = p.stem.replace("session-", "")
            paths.append((sid, p, None))
        return paths

    else:  # qwen
        return [(p.stem, p, p.parent.parent.name)
                for p in sorted(d.glob("*/chats/*.jsonl"))]


def _read_secret(key: str) -> str:
    try:
        for line in Path("/home/opc/.config/devforge/secrets.env").read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def enrich_with_search(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Add web search results to context for orphan groups and unknowns."""
    sys.path.insert(0, "/opt/projects/server")
    from lib.search_manager import WebSearchManager

    orphans = ctx.get("orphan_turns", [])
    bad_agents = ctx.get("bad_agents", [])

    topics = []
    # Search for orphan agents without matching worklogs
    orphan_agents = {o["agent"] for o in orphans}
    linked_agents = {
        w["agent"] for w in ctx.get("unlinked_worklogs", []) if w["agent"]
    }
    unmatched = orphan_agents - linked_agents
    for agent in unmatched:
        topics.append(f"{agent} AI coding tool worklog tracking")

    # Search for normalization rules for bad agents
    for bad in bad_agents:
        topics.append(f"{bad} AI agent canonical name normalization")

    if not topics:
        return ctx

    sm = WebSearchManager()
    results = []
    for topic in topics[:3]:  # max 3 searches per run (quota conservation)
        r = sm.search(topic)
        if r:
            results.append({"query": topic, "source": r["source"], "results": r["results"][:3]})

    ctx["search_results"] = results
    return ctx


def gather_context(
    checkpoint: Dict[str, Any], source_filter: Optional[str] = None
) -> Dict[str, Any]:
    """Build the structured context dict for Qwen's analysis."""
    sources = [source_filter] if source_filter else list(PARSERS.keys())

    # ── Session ingestion candidates ──
    sessions = []
    for source in sources:
        for sid, path, project in _discover_sessions(source):
            try:
                parsed, model, is_active = PARSERS[source](path)
            except Exception:
                continue
            if parsed is None:
                continue
            prev = checkpoint.get("sessions", {}).get(source, {}).get(sid, 0)
            new_count = len(parsed) - prev
            if new_count > 0:
                # Active session: skip last turn
                effective = new_count - (1 if is_active else 0)
                if effective > 0:
                    sessions.append({
                        "source": source,
                        "session_id": sid,
                        "project": project or "",
                        "checkpoint": prev,
                        "new_available": effective,
                        "active": is_active,
                    })

    # ── DB queries ──
    # Orphan turns (unlinked to any worklog)
    orphan_rows = _psql(
        "SELECT t.agent, COUNT(*), MIN(t.created_at)::text, MAX(t.created_at)::text "
        "FROM turns t "
        "WHERE t.id NOT IN (SELECT unnest(COALESCE(w.turn_ids, '{}'::uuid[])) "
        "                   FROM worklog_entries w WHERE w.turn_ids IS NOT NULL) "
        "GROUP BY t.agent ORDER BY COUNT(*) DESC"
    )
    orphans = []
    for line in orphan_rows.split("\n"):
        if not line:
            continue
        parts = line.split("|")
        if len(parts) >= 4:
            orphans.append({
                "agent": parts[0],
                "count": int(parts[1]),
                "earliest": parts[2][:19] if parts[2] else "",
                "latest": parts[3][:19] if parts[3] else "",
            })

    # Unlinked worklog entries (last 30 days, empty turn_ids)
    # Prioritize worklogs WITH agents (actionable) over empty-agent ones
    unlinked_rows = _psql(
        "SELECT w.id, w.agent, w.title, w.created_at::text "
        "FROM worklog_entries w "
        "WHERE w.created_at >= NOW() - INTERVAL '30 days' "
        "  AND (w.turn_ids IS NULL OR array_length(w.turn_ids, 1) = 0 "
        "       OR array_length(w.turn_ids, 1) IS NULL) "
        "ORDER BY (CASE WHEN w.agent IS NULL OR w.agent = '' THEN 1 ELSE 0 END), "
        "         w.created_at DESC LIMIT 20"
    )
    unlinked_worklogs = []
    for line in unlinked_rows.split("\n"):
        if not line:
            continue
        parts = line.split("|")
        if len(parts) >= 4:
            unlinked_worklogs.append({
                "worklog_id": int(parts[0]),
                "agent": parts[1],
                "title": parts[2],
                "created_at": parts[3][:19] if parts[3] else "",
            })

    # Recent activity (last 6 hours)
    recent_rows = _psql(
        "SELECT agent, COUNT(*) FROM turns "
        "WHERE created_at >= NOW() - INTERVAL '6 hours' "
        "GROUP BY agent ORDER BY COUNT(*) DESC"
    )
    recent = {}
    for line in recent_rows.split("\n"):
        if not line:
            continue
        parts = line.split("|")
        if len(parts) >= 2:
            recent[parts[0]] = int(parts[1])

    # Total turn count
    total_str = _psql("SELECT COUNT(*) FROM turns")
    total_turns = int(total_str.strip()) if total_str.strip().isdigit() else 0

    # Non-canonical agents
    bad_agents_str = _psql(
        "SELECT DISTINCT agent FROM turns WHERE agent NOT IN "
        "('" + "','".join(CANONICAL_AGENTS) + "')"
    )
    bad_agents = [a for a in bad_agents_str.split("\n") if a.strip()]

    # Recent turn content (last 8 turns with actual text for Qwen to observe)
    # Use replace() (literal, not regex) to collapse newlines → space and | → /
    recent_turns_rows = _psql(
        "SELECT COALESCE(t.agent, ''), "
        "  replace(replace(COALESCE(t.user_turn, ''), E'\n', ' '), '|', '/'), "
        "  replace(replace(COALESCE(t.thinking, ''), E'\n', ' '), '|', '/'), "
        "  replace(replace(COALESCE(t.text, ''), E'\n', ' '), '|', '/'), "
        "  t.created_at::text "
        "FROM turns t ORDER BY t.created_at DESC LIMIT 8"
    )
    recent_turns = []
    for line in recent_turns_rows.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) >= 5:
            recent_turns.append({
                "agent": parts[0],
                "user_turn": parts[1][:300] if parts[1] else "",
                "thinking": parts[2][:300] if parts[2] else "",
                "text": parts[3][:300] if parts[3] else "",
                "created_at": parts[4][:19] if parts[4] else "",
            })

    return {
        "orphan_turns": orphans,
        "unlinked_worklogs": unlinked_worklogs,
        "sessions": sessions,
        "recent_activity": recent,
        "recent_turns": recent_turns,
        "total_turns": total_turns,
        "bad_agents": bad_agents,
    }


def call_qwen(system_prompt: str, user_prompt: str,
              max_tokens: int = 2048) -> Optional[Dict[str, Any]]:
    """Send context to Qwen and parse the JSON response."""
    api_key = _read_secret("LITELLM_MASTER_KEY") or "unused"

    payload = {
        "model": "qwen2.5-coder-7b",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }

    for attempt in range(2):
        try:
            r = requests.post(
                "http://127.0.0.1:4000/v1/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json=payload,
                timeout=600,
            )
            if r.status_code != 200:
                print(f"Qwen API returned {r.status_code} (attempt {attempt+1}/2)")
                time.sleep(2 ** attempt)
                continue

            content = r.json()["choices"][0]["message"]["content"]

            # Extract JSON from response (may be in markdown fence)
            start = content.find("{")
            end = content.rfind("}")
            if start == -1 or end == -1:
                print(f"Qwen response had no JSON (attempt {attempt+1}/2): {content[:200]}")
                time.sleep(2 ** attempt)
                continue

            try:
                return json.loads(content[start:end + 1])
            except json.JSONDecodeError:
                print(f"JSON parse failed (attempt {attempt+1}/2): {content[:200]}")
                time.sleep(2 ** attempt)
                continue

        except requests.ConnectionError:
            print(f"Qwen API unreachable (attempt {attempt+1}/2)")
            time.sleep(2 ** attempt)
        except Exception as e:
            print(f"Qwen call error: {e} (attempt {attempt+1}/2)")
            time.sleep(2 ** attempt)

    return None


def execute_linkages(actions: List[Dict[str, Any]]) -> int:
    """Execute linkage actions. Returns count of linked turns.

    Time windows are computed deterministically (worklog created_at ± 24h).
    Qwen's time_start/time_end are ignored — the 7B model hallucinates them.
    """
    linked = 0
    for a in actions:
        worklog_id = a["worklog_id"]
        agent = a.get("agent", "").strip()

        if agent not in CANONICAL_AGENTS:
            print(f"  Skipping linkage: non-canonical agent '{agent}' (Qwen hallucination)")
            continue

        # Derive time window from worklog's actual created_at (± 24 hours)
        wl = _psql(
            f"SELECT created_at FROM worklog_entries WHERE id = {worklog_id}"
        )
        if not wl.strip():
            print(f"  Link worklog #{worklog_id}: worklog not found")
            continue

        ts = wl.strip()

        sql = (
            f"SELECT t.id FROM turns t "
            f"WHERE t.agent = '{agent}' "
            f"  AND t.created_at >= '{ts}'::timestamptz - INTERVAL '24 hours' "
            f"  AND t.created_at <= '{ts}'::timestamptz + INTERVAL '24 hours' "
            f"  AND t.id NOT IN (SELECT unnest(COALESCE(w.turn_ids, '{{}}'::uuid[])) "
            f"                   FROM worklog_entries w WHERE w.turn_ids IS NOT NULL) "
            f"ORDER BY t.created_at"
        )
        result = _psql(sql)
        if not result:
            print(f"  Link worklog #{worklog_id}: no matching turns found "
                  f"(agent={agent}, window={ts} ±24h)")
            continue

        turn_ids = [line.strip() for line in result.split("\n") if line.strip()]
        if not turn_ids:
            continue

        ids_str = "{" + ",".join(turn_ids) + "}"
        update = (
            f"UPDATE worklog_entries "
            f"SET turn_ids = COALESCE(turn_ids, '{{}}'::uuid[]) || '{ids_str}'::uuid[] "
            f"WHERE id = {worklog_id}"
        )
        _psql(update)
        print(f"  Linked worklog #{worklog_id}: {len(turn_ids)} turns ({agent})")
        linked += len(turn_ids)

    return linked


def execute_deterministic_linkage() -> int:
    """Link agent-known worklogs to turns via SQL JOIN ±24h (no Qwen). Returns count of linked turns."""
    rows = _psql(
        "SELECT w.id, w.agent, w.created_at::text "
        "FROM worklog_entries w "
        "WHERE w.created_at >= NOW() - INTERVAL '30 days' "
        "  AND (w.turn_ids IS NULL OR array_length(w.turn_ids, 1) = 0 "
        "       OR array_length(w.turn_ids, 1) IS NULL) "
        "  AND w.agent IS NOT NULL AND w.agent != '' "
        "ORDER BY w.created_at DESC LIMIT 50"
    )
    if not rows:
        return 0

    linked = 0
    for line in rows.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        wid = parts[0]
        agent = parts[1]
        ts = parts[2][:19]

        if agent not in CANONICAL_AGENTS:
            continue

        result = _psql(
            f"SELECT t.id FROM turns t "
            f"WHERE t.agent = '{agent}' "
            f"  AND t.created_at >= '{ts}'::timestamptz - INTERVAL '24 hours' "
            f"  AND t.created_at <= '{ts}'::timestamptz + INTERVAL '24 hours' "
            f"  AND t.id NOT IN (SELECT unnest(COALESCE(w2.turn_ids, '{{}}'::uuid[])) "
            f"                   FROM worklog_entries w2 WHERE w2.turn_ids IS NOT NULL) "
            f"ORDER BY t.created_at"
        )
        if not result:
            continue

        turn_ids = [ln.strip() for ln in result.split("\n") if ln.strip()]
        if not turn_ids:
            continue

        ids_str = "{" + ",".join(turn_ids) + "}"
        _psql(
            f"UPDATE worklog_entries "
            f"SET turn_ids = COALESCE(turn_ids, '{{}}'::uuid[]) || '{ids_str}'::uuid[] "
            f"WHERE id = {wid}"
        )
        print(f"  Deterministic link worklog #{wid}: {len(turn_ids)} turns ({agent})")
        linked += len(turn_ids)

    return linked


def validate_linkages() -> int:
    """Check all worklogs for turn_id integrity. Returns count of broken linkages."""
    broken = 0
    rows = _psql(
        "SELECT w.id, w.agent, COALESCE(array_length(w.turn_ids, 1), 0) AS stored, "
        "(SELECT COUNT(*) FROM turns t "
        " WHERE t.id = ANY(w.turn_ids)) AS matched "
        "FROM worklog_entries w "
        "WHERE w.turn_ids IS NOT NULL "
        "  AND array_length(w.turn_ids, 1) > 0"
    )
    if not rows:
        return 0

    for line in rows.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) >= 3 and parts[1].strip().isdigit() and parts[2].strip().isdigit():
            wid = parts[0]
            stored = int(parts[1])
            matched = int(parts[2])
            if stored != matched:
                print(f"  INTEGRITY: worklog #{wid} has {stored} turn_ids but only {matched} exist in DB")
                broken += 1

    return broken


def execute_fixes(actions: List[Dict[str, Any]]) -> int:
    """Execute fix actions. Returns count of fixes applied."""
    fixed = 0

    for a in actions:
        ftype = a.get("type", "")

        if ftype == "agent_normalization":
            for bad, good in AGENT_MAP.items():
                if bad == good:
                    continue  # skip identity mappings (copilot→copilot, etc.)
                rows = _psql(f"SELECT COUNT(*) FROM turns WHERE agent = '{bad}'")
                count = int(rows.strip()) if rows.strip().isdigit() else 0
                if count > 0:
                    _psql(f"UPDATE turns SET agent = '{good}' WHERE agent = '{bad}'")
                    print(f"  Fixed agent: {bad} -> {good} ({count} turns)")
                    fixed += 1

    return fixed


def execute_observations(observations: list, category: str = "general",
                         context: dict = None) -> int:
    """Persist structured facts (or legacy free-text observations) to DB. Returns count saved."""
    if not observations:
        return 0

    _psql("""
        CREATE TABLE IF NOT EXISTS observations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            observation TEXT NOT NULL CHECK (length(trim(observation)) > 0),
            category TEXT NOT NULL DEFAULT 'general',
            source TEXT NOT NULL DEFAULT 'qwen_worker',
            context JSONB DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    run_ctx = context or {}
    count = 0
    for obs in observations:
        if isinstance(obs, dict):
            # Structured fact: evidence → observation column, full fact → context JSONB
            evidence = (obs.get("evidence") or "").strip()
            if not evidence:
                continue
            fact_type = (obs.get("fact_type") or "data_given").strip()
            # Merge run-level context into fact metadata
            fact_ctx = {k: v for k, v in obs.items() if k != "evidence"}
            fact_ctx.update(run_ctx)
            ctx_json = json.dumps(fact_ctx, ensure_ascii=False)
            safe_evidence = evidence.replace("'", "''")
            safe_ctx = ctx_json.replace("'", "''")
            safe_category = fact_type.replace("'", "''")
            _psql(
                f"INSERT INTO observations (observation, category, source, context) "
                f"VALUES ('{safe_evidence}', '{safe_category}', 'qwen_worker', '{safe_ctx}'::jsonb)"
            )
        elif isinstance(obs, str):
            # Legacy free-text observation
            safe = obs.replace("'", "''")
            ctx_json = json.dumps(run_ctx, ensure_ascii=False)
            safe_ctx = ctx_json.replace("'", "''")
            _psql(
                f"INSERT INTO observations (observation, category, source, context) "
                f"VALUES ('{safe}', '{category}', 'qwen_worker', '{safe_ctx}'::jsonb)"
            )
        count += 1

    print(f"  Observations saved: {count}")
    return count
