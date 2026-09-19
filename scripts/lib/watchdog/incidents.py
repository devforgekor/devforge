#!/usr/bin/env python3
# Status: production
# Path: imported by — lib/watchdog/orchestrator.py, lib/cli_watch.py
"""Incident recorder — detect → capture → remediate → record (audit).

`watchdog_incidents` = one row per (dedup_key) incident with lifecycle:
  open → resolved (reopen within REOPEN_WINDOW_SEC if it recurs).

Context (bound journal/container logs) is captured BEFORE remediation so
forensic state survives the restart. Secrets are masked; size is bounded.

Industry mapping: SRE incident timeline + ITIL problem management + audit trail.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Optional

from lib.db import esc_sql, psql_json, psql_ok

REOPEN_WINDOW_SEC = 3600  # recur within 1h of resolution → reopen same incident
TASK_THRESHOLD = 3  # incidents for same dedup_key within 7d → create fix task
TASK_WINDOW = "7 days"
CONTEXT_MAX = 8000
CAPTURE_TIMEOUT = 5
EVENTS_KEEP_DAYS = 90
INCIDENTS_KEEP_DAYS = 180

_SECRET_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"), r"\1***"),
    (re.compile(r"sk-[A-Za-z0-9]{10,}"), "sk-***"),
    (re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key|authorization)\s*[:=]\s*\S+"), r"\1=***"),
    (re.compile(r"(?i)(-----BEGIN [A-Z ]+PRIVATE KEY-----)[\s\S]*?(-----END [A-Z ]+PRIVATE KEY-----)"), r"\1...\2"),
]

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS watchdog_incidents (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dedup_key     text NOT NULL,
    component     text NOT NULL,
    status        text NOT NULL DEFAULT 'open',
    symptom       text,
    context       text,
    detected_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    action        text,
    action_result text,
    action_at     timestamptz,
    resolved_at   timestamptz,
    fail_count    integer NOT NULL DEFAULT 1,
    reopen_count  integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_watchdog_incidents_open ON watchdog_incidents (status, dedup_key);
CREATE INDEX IF NOT EXISTS idx_watchdog_incidents_created ON watchdog_incidents (detected_at DESC);
"""

_schema_ready = False


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    psql_ok(CREATE_SQL, timeout=10)
    _schema_ready = True


def mask_secrets(text: str) -> str:
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def _run(cmd: list[str], timeout: int = CAPTURE_TIMEOUT) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return f"(capture failed: {e})"


def capture_context(unit: Optional[str]) -> str:
    """Grab bounded, masked diagnostic context for a component's unit.

    unit may be 'system:<name>' to target a rootful system service.
    """
    if not unit:
        return ""
    system_scope = unit.startswith("system:")
    real = unit.split(":", 1)[1] if system_scope else unit
    scope = [] if system_scope else ["--user"]
    parts: list[str] = []
    if real.startswith("container-"):
        cname = real.removeprefix("container-")
        parts.append("$ podman logs --tail 40 " + cname)
        parts.append(_run(["podman", "logs", "--tail", "40", cname]))
    label = " ".join(["systemctl", *scope, "show", real, "-p", "Result,ExecMainStatus,NRestarts"])
    parts.append("$ " + label)
    parts.append(_run(["systemctl", *scope, "show", real, "-p", "Result,ExecMainStatus,NRestarts"]))
    if not real.startswith("container-"):
        parts.append("$ " + " ".join(["journalctl", *scope, "-u", real, "-n", "40"]))
        parts.append(_run(["journalctl", *scope, "-u", real, "-n", "40", "--no-pager"]))
    ctx = mask_secrets("\n".join(parts))
    return ctx[:CONTEXT_MAX]


def _latest_open(dedup: str) -> Optional[int]:
    rows = psql_json(
        f"SELECT id FROM watchdog_incidents WHERE dedup_key='{esc_sql(dedup)}' "
        f"AND status='open' ORDER BY id DESC LIMIT 1"
    )
    return rows[0]["id"] if rows else None


def _recent_resolved(dedup: str) -> Optional[int]:
    rows = psql_json(
        f"SELECT id FROM watchdog_incidents WHERE dedup_key='{esc_sql(dedup)}' "
        f"AND status='resolved' AND resolved_at > now() - interval '{REOPEN_WINDOW_SEC} seconds' "
        f"ORDER BY id DESC LIMIT 1"
    )
    return rows[0]["id"] if rows else None


def _maybe_create_task(dedup: str) -> None:
    rows = psql_json(
        f"SELECT count(*) AS n FROM watchdog_incidents "
        f"WHERE dedup_key='{esc_sql(dedup)}' AND detected_at > now() - interval '{TASK_WINDOW}'"
    )
    if not rows or int(rows[0].get("n", 0)) < TASK_THRESHOLD:
        return
    title = esc_sql(f"[watchdog] 반복 실패 조사: {dedup}")
    psql_ok(
        f"INSERT INTO tasks (title, status, priority, description, tags) VALUES "
        f"('{title}', 'pending', 'medium', "
        f"'watchdog 반복 incident (최근 {TASK_WINDOW} 내 {TASK_THRESHOLD}회+). "
        f"watchdog_incidents 참고해 근본원인 수정.', "
        f"'{{watchdog,incident}}') ON CONFLICT (title) DO NOTHING"
    )
    _maybe_create_github_issue(dedup)


# ── GitHub Issue → (dev-poll auto-safe claim → dev_pipeline PR) ──────
GH_REPO = os.environ.get("WATCHDOG_GH_REPO", "devforgekor/devforge")
GH_LABELS = os.environ.get("WATCHDOG_GH_LABELS", "watchdog,auto-safe")
def _gh_env() -> dict:
    env = {**os.environ}
    token = os.environ.get("MY_GITHUB_TOKEN_KEY", "")
    if token:
        env["GH_TOKEN"] = token
    return env


def _gh(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout, env=_gh_env())


def _maybe_create_github_issue(dedup: str) -> None:
    """반복 incident를 GitHub 이슈로 생성(멱등). auto-safe 라벨로 dev-poll이 자동 claim."""
    if os.environ.get("WATCHDOG_GH_ISSUES", "1") != "1":
        return
    title = f"[watchdog] 반복 실패: {dedup}"
    try:
        r = _gh(["issue", "list", "--repo", GH_REPO, "--state", "open",
                 "--json", "number,title", "--limit", "200"])
        if r.returncode == 0:
            for it in json.loads(r.stdout or "[]"):
                if it.get("title") == title:
                    return  # already open
        for lb in [x.strip() for x in GH_LABELS.split(",") if x.strip()]:
            _gh(["label", "create", lb, "--repo", GH_REPO, "--force"])
        inc = psql_json(
            f"SELECT symptom, fail_count, action, action_result, context "
            f"FROM watchdog_incidents WHERE dedup_key='{esc_sql(dedup)}' ORDER BY id DESC LIMIT 1"
        )
        row = inc[0] if inc else {}
        ctx = (row.get("context") or "").replace("```", "'''")[:2000]
        body = (
            "## watchdog 반복 incident (자동 생성)\n\n"
            f"- dedup: `{dedup}`\n"
            f"- symptom: {row.get('symptom') or '-'}\n"
            f"- fail_count: {row.get('fail_count') or '-'}\n"
            f"- recent action: {row.get('action') or '-'} ({row.get('action_result') or '-'})\n\n"
            "근본원인을 수정하고 테스트 후 PR 하세요. (watchdog_incidents 테이블 참고)\n\n"
            "### context (masked)\n```\n" + ctx + "\n```\n"
        )
        r = _gh(["issue", "create", "--repo", GH_REPO, "--title", title, "--body", body,
                 "--label", GH_LABELS])
        print(f"[incidents] github issue: {r.stdout.strip()[:120] or r.stderr.strip()[:120]}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[incidents] github issue create failed: {e}", flush=True)


def record_detect(component: str, event_type: str, detail: str, unit: Optional[str] = None) -> Optional[int]:
    """Create/update/reopen an incident for a detected failure. Returns incident id."""
    ensure_schema()
    dedup = f"{component}:{event_type}"
    sym = esc_sql(detail or "")[:500]
    inc = _latest_open(dedup)
    if inc is not None:
        psql_ok(
            f"UPDATE watchdog_incidents SET fail_count=fail_count+1, "
            f"symptom='{sym}', last_seen_at=now() WHERE id={inc}"
        )
        return inc
    ctx = esc_sql(capture_context(unit))
    inc = _recent_resolved(dedup)
    if inc is not None:  # reopen recent
        psql_ok(
            f"UPDATE watchdog_incidents SET status='open', reopen_count=reopen_count+1, "
            f"fail_count=fail_count+1, resolved_at=NULL, detected_at=now(), last_seen_at=now(), "
            f"symptom='{sym}', context='{ctx}' WHERE id={inc}"
        )
        _maybe_create_task(dedup)
        return inc
    rows = psql_json(
        f"INSERT INTO watchdog_incidents (dedup_key, component, status, symptom, context) "
        f"VALUES ('{esc_sql(dedup)}', '{esc_sql(component)}', 'open', '{sym}', '{ctx}') RETURNING id"
    )
    if rows:
        _maybe_create_task(dedup)
        return rows[0]["id"]
    return None


def record_action(incident_id: Optional[int], action: str, ok: bool) -> None:
    """Record the remediation action and its result; resolve on success."""
    if incident_id is None:
        return
    a = esc_sql(action)
    if ok:
        psql_ok(
            f"UPDATE watchdog_incidents SET action='{a}', action_result='success', "
            f"action_at=now(), status='resolved', resolved_at=now() WHERE id={incident_id}"
        )
    else:
        psql_ok(
            f"UPDATE watchdog_incidents SET action='{a}', action_result='fail', "
            f"action_at=now() WHERE id={incident_id}"
        )


def resolve_if_open(component: str) -> None:
    """Close any open incident for a component that is healthy again."""
    ensure_schema()
    psql_ok(
        f"UPDATE watchdog_incidents SET status='resolved', resolved_at=now(), "
        f"action=COALESCE(action, 'auto-recovered'), action_result='success', action_at=now() "
        f"WHERE dedup_key LIKE '{esc_sql(component)}:%' AND status='open'"
    )


def list_incidents(open_only: bool = False, since: Optional[str] = None, limit: int = 20) -> list[dict]:
    ensure_schema()
    where = []
    if open_only:
        where.append("status='open'")
    if since:
        where.append(f"detected_at > now() - interval '{esc_sql(since)}'")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return psql_json(
        f"SELECT id, status, component, symptom, action, action_result, fail_count, "
        f"reopen_count, detected_at::text, resolved_at::text "
        f"FROM watchdog_incidents {clause} ORDER BY detected_at DESC LIMIT {int(limit)}"
    )


def get_incident(inc_id: int) -> Optional[dict]:
    ensure_schema()
    rows = psql_json(f"SELECT * FROM watchdog_incidents WHERE id={int(inc_id)}")
    return rows[0] if rows else None


def prune() -> None:
    """Retention: raw events 90d, resolved incidents 180d."""
    psql_ok(f"DELETE FROM catchdog_events WHERE created_at < now() - interval '{EVENTS_KEEP_DAYS} days'")
    psql_ok(
        f"DELETE FROM watchdog_incidents WHERE status='resolved' "
        f"AND resolved_at < now() - interval '{INCIDENTS_KEEP_DAYS} days'"
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
