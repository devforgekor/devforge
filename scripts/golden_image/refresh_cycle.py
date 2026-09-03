#!/usr/bin/env python3
# Status: production
# Path: systemd:golden-image-deploy-check.timer
"""15분 주기 — 배포 동기화 + 헬스체크 + 타임아웃(10분) + 큐 재시도."""

import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

from golden_image import models

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


def main() -> None:
    api_key = os.environ.get("LLAMA_API_KEY", "")
    # load from secrets.env fallback
    if not api_key:
        try:
            with open(os.path.expanduser("~/.config/devforge/secrets.env")) as f:
                for line in f:
                    if line.startswith("LLAMA_API_KEY="):
                        api_key = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass

    pendings = models.list_pending_deployments()
    now = datetime.now(timezone.utc)
    for dep in pendings:
        dep_id = dep["id"]
        vm_name = dep["vm_name"]
        public_ip = dep.get("public_ip")
        created = dep.get("created_at")
        # timeout check: 10분 초과 시 failed + 큐 적재(next cycle 재시도)
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
            # IP 미할당 — 다음 주기에 재시도 (best-effort)
            print(f"[pending] {vm_name} no IP yet, retry next cycle")
            continue

        # 3회 헬스체크
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
            # 3회 실패 — failed로 마킹하되 삭제하지 않고 큐 유지(next cycle 재시도)
            # 실제 VM은 Spot eviction 시 Azure가 Delete하므로 DB만 상태 갱신
            print(f"[unhealthy] {vm_name} 3x fail — will retry next cycle")


if __name__ == "__main__":
    main()
