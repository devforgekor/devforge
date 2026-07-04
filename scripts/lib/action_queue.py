#!/usr/bin/env python3
# Status: production
# Path: imported by — mcp_server.py (action_write, action_poll), watchdog/__init__.py (_consume_actions)
"""Action Queue — MCP tool → background daemon bridge.

Pattern: MCP tools write actions to watchdog_pulses (category='action').
Watchdog consumes them asynchronously, executes safely, records results.

Usage:
    # MCP tool side (write-only, returns immediately)
    pid = action_write("Restart inference", action_type="podman",
                       action_params={"container": "devforge-inference"})

    # Watchdog side (consume + execute)
    for a in action_claim_pending():
        ok, msg = execute_action(a)
        action_complete(a["pulse_id"], msg) if ok else action_fail(a["pulse_id"], msg)
"""

import json
import subprocess
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from lib.db import esc_sql, psql_json, psql_ok


def _make_action_id() -> str:
    """Generate unique action pulse_id: action_{date}_{random_hex}."""
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    rand = uuid.uuid4().hex[:12]
    return f"action_{date_str}_{rand}"


def action_write(
    instruction: str,
    priority: str = "P1_CONTEXT",
    action_type: str = "systemctl",
    action_params: Optional[Dict[str, Any]] = None,
    max_retries: int = 3,
) -> Optional[str]:
    """Write an action pulse to watchdog_pulses.

    MCP tool calls this. Returns pulse_id or None on failure.
    The watchdog consumes this asynchronously on its next cycle.
    """
    pulse_id = _make_action_id()
    params_json = json.dumps(action_params or {}, ensure_ascii=False)
    ok = psql_ok(
        "INSERT INTO watchdog_pulses "
        "(pulse_id, priority, category, target_file, target_test, instruction, max_retries) "
        f"VALUES ('{esc_sql(pulse_id)}', '{esc_sql(priority)}', "
        f"'action', '{esc_sql(action_type)}', "
        f"'{esc_sql(params_json)}', '{esc_sql(instruction)}', {max_retries})"
    )
    return pulse_id if ok else None


def action_claim_pending(max_count: int = 10) -> List[Dict[str, Any]]:
    """Atomic PENDING -> IN_PROGRESS for action pulses.

    Returns list of claimed action dicts with keys:
        pulse_id, priority, action_type, action_params (parsed), instruction,
        retry_count, max_retries, created_at
    """
    try:
        rows = psql_json(
            "SELECT pulse_id, priority, instruction, target_file, "
            "target_test, retry_count, max_retries, created_at "
            "FROM watchdog_pulses "
            "WHERE status = 'PENDING' AND category = 'action' "
            "ORDER BY "
            "  CASE priority "
            "    WHEN 'P0_HOT_FIX' THEN 1 "
            "    WHEN 'P1_CONTEXT' THEN 2 "
            "    WHEN 'P2_LOW' THEN 3 "
            "    ELSE 4 END, "
            "  created_at ASC "
            f"LIMIT {max_count}"
        )
    except Exception:
        return []
    if not rows:
        return []

    pulse_ids = [r["pulse_id"] for r in rows]
    id_list = ", ".join(f"'{esc_sql(pid)}'" for pid in pulse_ids)
    psql_ok(
        "UPDATE watchdog_pulses SET status = 'IN_PROGRESS' "
        f"WHERE pulse_id IN ({id_list}) AND status = 'PENDING'"
    )

    actions = []
    for r in rows:
        params = {}
        try:
            params = json.loads(r.get("target_test") or "{}")
        except (json.JSONDecodeError, TypeError):
            params = {}
        actions.append(
            {
                "pulse_id": r["pulse_id"],
                "priority": r.get("priority", "P1_CONTEXT"),
                "instruction": r.get("instruction", ""),
                "action_type": r.get("target_file", "systemctl"),
                "action_params": params,
                "retry_count": r.get("retry_count", 0),
                "max_retries": r.get("max_retries", 3),
                "created_at": r.get("created_at", ""),
            }
        )
    return actions


def action_complete(pulse_id: str, result_summary: str = "") -> bool:
    """Mark action pulse as RESOLVED and record result observation."""
    from lib.observation import observe

    ok = psql_ok(
        "UPDATE watchdog_pulses SET status = 'RESOLVED', resolved_at = now() "
        f"WHERE pulse_id = '{esc_sql(pulse_id)}'"
    )
    if ok:
        observe(
            f"[action] {result_summary}",
            category="config",
            source="action_queue:completed",
            context={"pulse_id": pulse_id},
            tags={"domain": ["action_queue"], "action": ["completed"]},
        )
    return ok


def action_fail(pulse_id: str, error: str = "") -> bool:
    """Increment retry or escalate to HUMAN_REQUIRED if max_retries exceeded."""
    from lib.observation import observe

    rows = psql_json(
        f"SELECT retry_count, max_retries FROM watchdog_pulses "
        f"WHERE pulse_id = '{esc_sql(pulse_id)}'"
    )
    if not rows:
        return False

    r = rows[0]
    retry_count = r.get("retry_count", 0) + 1
    max_retries = r.get("max_retries", 3)

    if retry_count >= max_retries:
        ok = psql_ok(
            "UPDATE watchdog_pulses SET status = 'HUMAN_REQUIRED', "
            f"retry_count = {retry_count}, last_failure = '{esc_sql(error[:500])}' "
            f"WHERE pulse_id = '{esc_sql(pulse_id)}'"
        )
        observe(
            f"[action:escalated] {error}",
            category="config",
            source="action_queue:escalated",
            context={"pulse_id": pulse_id, "retry_count": retry_count},
            tags={"domain": ["action_queue"], "action": ["escalated"]},
        )
    else:
        ok = psql_ok(
            "UPDATE watchdog_pulses SET status = 'PENDING', "
            f"retry_count = {retry_count}, last_failure = '{esc_sql(error[:500])}' "
            f"WHERE pulse_id = '{esc_sql(pulse_id)}'"
        )
        observe(
            f"[action:retry] {error} (retry {retry_count}/{max_retries})",
            category="config",
            source="action_queue:retry",
            context={"pulse_id": pulse_id, "retry_count": retry_count},
            tags={"domain": ["action_queue"], "action": ["retry"]},
        )
    return ok


def action_poll_results(pulse_id: str) -> List[Dict[str, Any]]:
    """Poll result observations for a specific action pulse.

    Returns list of observation dicts.
    """
    try:
        rows = psql_json(
            "SELECT id, observation, category, source, context, tags, created_at "
            "FROM observations "
            "WHERE source LIKE 'action_queue:%' "
            f"AND context->>'pulse_id' = '{esc_sql(pulse_id)}' "
            "ORDER BY created_at ASC"
        )
        return rows or []
    except Exception:
        return []


# ── Safe execution dispatch ────────────────────────────────────
# Each executor receives action_params dict, returns (success: bool, message: str).
# No shell=True anywhere — all subprocess calls use explicit argument lists.


def _exec_systemctl(params: Dict[str, Any]) -> tuple:
    """Execute a safe systemctl action."""
    service = params.get("service", "")
    if not service or any(c in service for c in (";", "|", "&", "$", "`")):
        return False, f"Invalid service name: {service}"
    cmd = params.get("command", "restart")
    subcmd = ["systemctl", "--user", cmd, service]
    try:
        r = subprocess.run(subcmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True, f"systemctl {cmd} {service}: OK"
        return False, f"systemctl {cmd} {service}: {r.stderr.strip()[:200]}"
    except subprocess.TimeoutExpired:
        return False, f"systemctl {cmd} {service}: timed out"
    except Exception as e:
        return False, f"systemctl {cmd} {service}: {e}"


def _exec_podman(params: Dict[str, Any]) -> tuple:
    """Execute a safe podman action."""
    container = params.get("container", "")
    if not container or any(c in container for c in (";", "|", "&", "$", "`")):
        return False, f"Invalid container name: {container}"
    cmd = params.get("command", "restart")
    subcmd = ["podman", cmd, container]
    try:
        r = subprocess.run(subcmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return True, f"podman {cmd} {container}: OK"
        return False, f"podman {cmd} {container}: {r.stderr.strip()[:200]}"
    except subprocess.TimeoutExpired:
        return False, f"podman {cmd} {container}: timed out"
    except Exception as e:
        return False, f"podman {cmd} {container}: {e}"


def _exec_cli(params: Dict[str, Any]) -> tuple:
    """Execute a safe CLI action (python3 scripts/<script> <args>)."""
    script = params.get("script", "")
    args_list = params.get("args", [])
    if not script:
        return False, "No script specified"
    if any(c in script for c in (";", "|", "&", "$", "`")):
        return False, f"Invalid script name: {script}"

    cmd = ["python3", f"/opt/projects/server/scripts/{script}"] + list(args_list)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            return True, f"cli {script}: {r.stdout.strip()[:200]}"
        return False, f"cli {script}: {r.stderr.strip()[:200]}"
    except subprocess.TimeoutExpired:
        return False, f"cli {script}: timed out"
    except Exception as e:
        return False, f"cli {script}: {e}"


ACTION_EXECUTORS = {
    "systemctl": _exec_systemctl,
    "podman": _exec_podman,
    "cli": _exec_cli,
}


def execute_action(action: Dict[str, Any]) -> tuple:
    """Execute a claimed action safely.

    Args:
        action: Action dict from action_claim_pending()

    Returns:
        (success: bool, message: str)
    """
    action_type = action.get("action_type", "")
    params = action.get("action_params", {})
    executor = ACTION_EXECUTORS.get(action_type)
    if not executor:
        return False, f"Unknown action type: {action_type}"
    return executor(params)
