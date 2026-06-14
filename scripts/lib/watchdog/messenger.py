#!/usr/bin/env python3
# Status: production
# Path: imported by — watchdog.py, day_pipeline.py, pipelines/*
"""Watchdog Messenger — 정보 중개 시스템.

PostgreSQL-backed: watchman_pulses table replaces file-based queue.
Idempotent pulse IDs (date + file_hash) prevent duplicate insertion.
"""

import hashlib
from datetime import datetime, timezone
from typing import Optional

from lib.db import psql_ok, psql_json, esc_sql


def _make_pulse_id(instruction: str, target_file: str = "", date_str: str = "") -> str:
    """Generate deterministic pulse_id: pulse_{date}_{content_hash[:12]}."""
    if not date_str:
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    payload = f"{date_str}:{target_file}:{instruction[:100]}"
    content_hash = hashlib.sha256(payload.encode()).hexdigest()[:12]
    return f"pulse_{date_str}_{content_hash}"


def log_message(source: str, target: str, type: str, content: str, detail: str = "",
                priority: str = "P1_CONTEXT", category: str = "",
                target_file: str = "", target_test: str = "",
                max_retries: int = 3) -> Optional[str]:
    """메시지 기록 → watchman_pulses table.

    Returns pulse_id if created, None if duplicate (idempotent).
    Signature backward-compatible with old file-based log_message().
    """
    pulse_id = _make_pulse_id(content, target_file)

    ct = esc_sql(content)
    dt = esc_sql(detail)
    cat = esc_sql(category)
    tf = esc_sql(target_file)
    tt = esc_sql(target_test)

    ok = psql_ok(
        f"INSERT INTO watchman_pulses (pulse_id, priority, category, target_file, "
        f"target_test, instruction, max_retries) "
        f"VALUES ('{pulse_id}', '{esc_sql(priority)}', "
        f"NULLIF('{cat}', ''), NULLIF('{tf}', ''), NULLIF('{tt}', ''), "
        f"'{ct}', {max_retries}) "
        f"ON CONFLICT (pulse_id) DO UPDATE SET "
        f"last_failure = NULLIF('{dt}', ''), "
        f"retry_count = watchman_pulses.retry_count + 1 "
        f"WHERE watchman_pulses.status = 'PENDING'"
    )
    return pulse_id if ok else None


def get_undelivered(target: Optional[str] = None) -> list:  # Python 3.9: list[dict] not supported
    """미전달 pulse 조회 및 IN_PROGRESS 마킹 (atomic via transaction).

    Returns list of dicts with keys matching old file format:
        type, content, detail, target_file, target_test, retry_count, max_retries
    """
    target_clause = f"AND target_file = '{esc_sql(target)}'" if target else ""
    pulse_ids = []

    # 1. PENDING → IN_PROGRESS (mark-and-fetch pattern)
    try:
        rows = psql_json(
            f"SELECT pulse_id, priority, category, instruction, target_file, "
            f"target_test, retry_count, max_retries "
            f"FROM watchman_pulses "
            f"WHERE status = 'PENDING' {target_clause} "
            f"ORDER BY "
            f"  CASE priority "
            f"    WHEN 'P0_HOT_FIX' THEN 1 "
            f"    WHEN 'P1_CONTEXT' THEN 2 "
            f"    WHEN 'HUMAN_REQUIRED' THEN 3 "
            f"    ELSE 4 END, "
            f"  created_at ASC "
            f"LIMIT 20"
        )
    except Exception:
        return []

    if not rows:
        return []

    for r in rows:
        pulse_ids.append(r["pulse_id"])

    # 2. Mark IN_PROGRESS
    id_list = ", ".join(f"'{esc_sql(pid)}'" for pid in pulse_ids)
    psql_ok(
        f"UPDATE watchman_pulses SET status = 'IN_PROGRESS' "
        f"WHERE pulse_id IN ({id_list}) AND status = 'PENDING'"
    )

    # 3. Return in old format
    messages = []
    for r in rows:
        messages.append({
            "type": "alert",
            "content": f"[{r['priority']}] {r['instruction']}",
            "detail": f"pulse_id={r['pulse_id']}",
            "target_file": r.get("target_file", ""),
            "target_test": r.get("target_test", ""),
            "retry_count": r.get("retry_count", 0),
            "max_retries": r.get("max_retries", 3),
        })
    return messages


def resolve_pulse(pulse_id: str, status: str = "RESOLVED") -> bool:
    """Mark a pulse as RESOLVED or IGNORED."""
    return psql_ok(
        f"UPDATE watchman_pulses SET status = '{esc_sql(status)}', "
        f"resolved_at = now() "
        f"WHERE pulse_id = '{esc_sql(pulse_id)}'"
    )


def escalate_pulse(pulse_id: str, reason: str = "") -> bool:
    """Escalate pulse to HUMAN_REQUIRED (retry_count >= max_retries)."""
    r = esc_sql(reason)
    return psql_ok(
        f"UPDATE watchman_pulses "
        f"SET status = 'HUMAN_REQUIRED', last_failure = NULLIF('{r}', '') "
        f"WHERE pulse_id = '{esc_sql(pulse_id)}'"
    )


def list_pulses(status: str = "PENDING", limit: int = 20) -> list:  # Python 3.9: list[dict] not supported
    """List pulses by status."""
    return psql_json(
        f"SELECT pulse_id, priority, category, instruction, target_file, "
        f"retry_count, max_retries, status, created_at "
        f"FROM watchman_pulses "
        f"WHERE status = '{esc_sql(status)}' "
        f"ORDER BY created_at DESC LIMIT {limit}"
    )


def get_pulse(pulse_id: str) -> Optional[dict]:
    """Get single pulse by ID."""
    rows = psql_json(
        f"SELECT * FROM watchman_pulses WHERE pulse_id = '{esc_sql(pulse_id)}'"
    )
    return rows[0] if rows else None
