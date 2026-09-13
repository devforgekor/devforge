#!/usr/bin/env python3
# Status: production
# Path: scripts/golden_image/schema.sql, scripts/golden_image/refresh_cycle.py, scripts/golden_image/yearly_check.py
"""Golden Image DB wrapper — lib/db.py psql pattern reuse, no ORM."""

from typing import Optional
from lib.db import psql, psql_json, psql_ok, escape_sql_string as esc


def create_version(version: str, image_id: Optional[str] = None, memo: Optional[str] = None) -> bool:
    vq = f"'{esc(version)}'"
    imgq = f"'{esc(image_id)}'" if image_id else "NULL"
    mq = f"'{esc(memo)}'" if memo else "NULL"
    sql = f"INSERT INTO golden_image_versions(version, image_id, memo) VALUES ({vq}, {imgq}, {mq}) ON CONFLICT (version) DO NOTHING"
    return psql_ok(sql)


def get_active_version() -> Optional[str]:
    rows = psql_json("SELECT version FROM golden_image_versions WHERE status='active' ORDER BY created_at DESC LIMIT 1")
    return rows[0]["version"] if rows else None


def update_version_status(version: str, status: str) -> bool:
    assert status in ("active", "deprecated")
    vq = f"'{esc(version)}'"
    sq = f"'{esc(status)}'"
    return psql_ok(f"UPDATE golden_image_versions SET status={sq} WHERE version={vq}")


def log_deployment(vm_name: str, status: str, public_ip: Optional[str] = None, error: Optional[str] = None, version: Optional[str] = None) -> Optional[int]:
    assert status in ("pending", "running", "success", "failed", "evicted")
    vq = f"'{esc(vm_name)}'"
    sq = f"'{esc(status)}'"
    ipq = f"'{esc(public_ip)}'" if public_ip else "NULL"
    errq = f"'{esc(error)}'" if error else "NULL"
    verq = f"'{esc(version)}'" if version else "NULL"
    out = psql(f"INSERT INTO deployment_logs(vm_name, status, public_ip, error, version) VALUES ({vq}, {sq}, {ipq}, {errq}, {verq}) RETURNING id")
    try:
        return int(out.strip().split("\n")[-1])
    except Exception:
        return None


def update_deployment(deployment_id: int, status: Optional[str] = None, public_ip: Optional[str] = None, error: Optional[str] = None) -> bool:
    sets = []
    if status:
        sets.append(f"status='{esc(status)}'")
    if public_ip is not None:
        sets.append(f"public_ip='{esc(public_ip)}'")
    if error is not None:
        sets.append(f"error='{esc(error)}'")
    if not sets:
        return False
    sets.append("updated_at=NOW()")
    return psql_ok(f"UPDATE deployment_logs SET {', '.join(sets)} WHERE id={int(deployment_id)}")


def record_health(deployment_id: int, success: bool, latency_ms: Optional[int] = None) -> bool:
    lat = str(int(latency_ms)) if latency_ms is not None else "NULL"
    succ = "true" if success else "false"
    return psql_ok(f"INSERT INTO health_checks(deployment_id, success, latency_ms) VALUES ({int(deployment_id)}, {succ}, {lat})")


def list_pending_deployments() -> list:
    return psql_json("SELECT id, vm_name, version, status, public_ip FROM deployment_logs WHERE status IN ('pending','running') ORDER BY created_at DESC LIMIT 20")
