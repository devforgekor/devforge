#!/usr/bin/env python3
# Status: experimental
# Path: imported by — lib.infra.azure_spot.__init__, cli, lib.debate.cooperative_debate
"""Azure Spot VM orchestrator — coordinates VM lifecycle (endpoint provider)."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from lib.infra.azure_spot.config import (
    LLAMA_SERVER_PORT,
    SPOT_CONFIGS,
    TUNNEL_PORT_BASE,
    SpotVMConfig,
)
from lib.infra.azure_spot.manager import SpotVMManager
from lib.infra.azure_spot.tunnel import open_spot_tunnel


class SpotOrchestrator:
    def __init__(self, configs: Optional[List[SpotVMConfig]] = None):
        self.configs = configs or list(SPOT_CONFIGS.values())
        self.vms: Dict[str, Dict[str, Any]] = {}
        self.tunnels: Dict[str, Any] = {}
        self.managers: Dict[str, SpotVMManager] = {c.label: SpotVMManager(c) for c in self.configs}

    def add(self, label: str, cfg: SpotVMConfig) -> None:
        """Register an additional VM config under a logical label (consumer API)."""
        self.configs.append(cfg)
        self.managers[label] = SpotVMManager(cfg)

    def provision_all(self, ssh_timeout: int = 180, llm_timeout: int = 300) -> bool:
        """Consumer API: provision all and return True iff none failed/unhealthy."""
        results = self.launch_all(ssh_timeout=ssh_timeout, llm_timeout=llm_timeout)
        return bool(results) and all(v not in ("failed", "unhealthy") for v in results.values())

    def launch_all(self, ssh_timeout: int = 180, llm_timeout: int = 300) -> Dict[str, str]:
        results: Dict[str, str] = {}
        for cfg in self.configs:
            mgr = self.managers.get(cfg.label) or SpotVMManager(cfg)
            try:
                vm = mgr.create_vm()
            except RuntimeError as e:
                print(f"  Failed to create VM for {cfg.label}: {e}")
                results[cfg.label] = "failed"
                continue
            self.vms[cfg.label] = vm

            if not mgr.poll_until_ready(vm["ip"], ssh_timeout, llm_timeout):
                results[cfg.label] = "unhealthy"
                continue

            local_port = TUNNEL_PORT_BASE + len(self.tunnels)
            proc = open_spot_tunnel(cfg.label, vm["ip"], LLAMA_SERVER_PORT, local_port)
            if proc:
                self.tunnels[cfg.label] = {"proc": proc, "port": local_port, "ip": vm["ip"]}
            results[cfg.label] = vm["ip"]
            time.sleep(5)
        return results

    def close_all_tunnels(self):
        from lib.infra.azure_spot.tunnel import close_spot_tunnel, close_spot_tunnels
        for label, info in self.tunnels.items():
            print(f"  Closing tunnel for {label}")
            close_spot_tunnel(info["port"])
        self.tunnels.clear()
        close_spot_tunnels(TUNNEL_PORT_BASE, count=8)  # sweep tunnels from other processes

    def delete_all_vms(self):
        for label, vm in self.vms.items():
            mgr = self.managers.get(label)
            if mgr:
                mgr.delete_vm(vm["name"])
        self.vms.clear()

    def terminate_all(self):
        """Consumer API: close tunnels + delete VMs."""
        self.cleanup_all()

    def sweep_orphans(self, ttl_sec: int = 7200, dry_run: bool = False) -> Dict[str, list]:
        """Delete orphan spot VMs older than ttl_sec across all configs."""
        return {label: mgr.sweep_orphans(ttl_sec=ttl_sec, dry_run=dry_run)
                for label, mgr in self.managers.items()}

    def teardown(self, verify_timeout: int = 180) -> Dict[str, Any]:
        """Discover existing spot VMs, delete them, and VERIFY removal (frees quota)."""
        self.close_all_tunnels()
        out: Dict[str, Any] = {}
        for label, mgr in self.managers.items():
            names = [v["name"] for v in mgr.list_spot_vms()]
            verified = True
            for n in names:
                verified = mgr.delete_vm_verified(n, timeout=verify_timeout) and verified
            out[label] = {"deleted": names, "verified": verified,
                          "remaining": len(mgr.list_spot_vms())}
        self.vms.clear()
        return out

    def verify_clean(self) -> Dict[str, int]:
        """Remaining spot VM count per config (0 = clean, next create will succeed)."""
        return {label: len(mgr.list_spot_vms()) for label, mgr in self.managers.items()}

    def preflight_clean(self, verify_timeout: int = 180) -> list:
        """Before launch: delete leftover spot VMs so Spot quota is free."""
        removed: list = []
        for _label, mgr in self.managers.items():
            for v in mgr.list_spot_vms():
                if mgr.delete_vm_verified(v["name"], timeout=verify_timeout):
                    removed.append(v["name"])
        return removed

    def cleanup_all(self):
        self.close_all_tunnels()
        self.delete_all_vms()
