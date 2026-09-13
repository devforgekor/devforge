#!/usr/bin/env python3
# Status: production
# Path: systemd:golden-image-deploy-check.timer
"""15분 주기 — 배포 동기화 + 헬스체크 + 타임아웃(10분) + 잔존 VM 강제 종료."""

import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

from golden_image import models
from golden_image import azure_client

TIMEOUT_MIN = 10
HEALTH_PATH = "/health"
HEALTH_PORT = 443  # Caddy TLS, 문서 §6


def _health_check(ip: str, api_key: str, timeout: int = 5) -> tuple[bool, int]:
    start = time.time()
    try:
        req = urllib.request.Request(f"https://{ip}{HEALTH_PATH}")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = resp.status == 200
            latency = int((time.time() - start) * 1000)
            return ok, latency
    except Exception:
        latency = int((time.time() - start) * 1000)
        return False, latency


def _load_api_key() -> str:
    api_key = os.environ.get("LLAMA_API_KEY", "")
    if not api_key:
        try:
            with open(os.path.expanduser("~/.config/devforge/secrets.env")) as f:
                for line in f:
                    if line.startswith("LLAMA_API_KEY="):
                        api_key = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return api_key


def _check_deployments(api_key: str) -> None:
    """기존 배포 추적/헬스체크/타임아웃 로직."""
    pendings = models.list_pending_deployments()
    now = datetime.now(timezone.utc)
    for dep in pendings:
        dep_id = dep["id"]
        vm_name = dep["vm_name"]
        public_ip = dep.get("public_ip")
        created = dep.get("created_at")
        try:
            if isinstance(created, str):
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            else:
                created_dt = created
            if created_dt and (now - created_dt) > timedelta(minutes=TIMEOUT_MIN):
                models.update_deployment(dep_id, status="failed", error="timeout 10min")
                print(f"[timeout] {vm_name} -> failed")
                continue
        except Exception:
            pass

        if not public_ip:
            print(f"[pending] {vm_name} no IP yet, retry next cycle")
            continue

        success = False
        latency = None
        for _ in range(3):
            ok, lat = _health_check(public_ip, api_key)
            models.record_health(dep_id, ok, lat)
            if ok:
                success = True
                latency = lat
                break
            time.sleep(2)

        if success:
            models.update_deployment(dep_id, status="success")
            print(f"[healthy] {vm_name} {latency}ms")
        else:
            print(f"[unhealthy] {vm_name} 3x fail — will retry next cycle")


def _check_orphan_vms() -> None:
    """Azure에 llm-qwen* VM이 살아있으면 강제 종료 — 안전장치."""
    vms = azure_client.list_vms_by_prefix("llm-qwen")
    if not vms:
        return

    for vm in vms:
        vm_name = vm.get("name", "unknown")
        ip = vm.get("publicIps", "N/A")
        power = vm.get("powerState", "unknown")
        print(f"[orphan] detected live VM: {vm_name} (ip={ip}, power={power}) — forcing delete")
        ok = azure_client.delete_vm(vm_name)
        if ok:
            print(f"[orphan] {vm_name} deleted successfully")
            models.log_deployment(
                vm_name=vm_name,
                status="evicted",
                public_ip=ip if ip != "N/A" else None,
                error="orphan VM force-deleted by 15min safety timer",
            )
        else:
            print(f"[orphan] {vm_name} delete FAILED — will retry next cycle")


def main() -> None:
    api_key = _load_api_key()
    _check_deployments(api_key)
    _check_orphan_vms()


if __name__ == "__main__":
    main()
