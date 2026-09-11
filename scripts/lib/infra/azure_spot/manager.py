#!/usr/bin/env python3
# Status: experimental
# Path: imported by — lib.infra.azure_spot.__init__, orchestrator, cli
"""Azure Spot VM manager — create, poll, delete spot VMs."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any, Dict, List

from lib.infra.azure_spot.config import (
    LLAMA_SERVER_PORT,
    PUBLIC_IP_SKU,
    SSH_KEY_PATH,
    SSH_USER,
    SpotVMConfig,
)


def _az(*args: str, subscription: str = "", timeout: int = 120) -> subprocess.CompletedProcess:
    cmd = ["az"] + list(args)
    if subscription:
        cmd += ["--subscription", subscription]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _vm_name(label: str) -> str:
    return f"spot-{label}-{int(time.time())}"


_NAME_TS_RE = re.compile(r"-(\d{9,})$")


def _vm_age_sec(name: str, now: float | None = None) -> int | None:
    """Age in seconds from the epoch suffix encoded in a spot VM name, or None."""
    m = _NAME_TS_RE.search(name or "")
    if not m:
        return None
    return int((now if now is not None else time.time()) - int(m.group(1)))


def _wait_for_ssh(ip: str, timeout: int = 180, interval: int = 10) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ConnectTimeout=5",
                f"{SSH_USER}@{ip}",
                "echo ssh_ok",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0 and "ssh_ok" in r.stdout:
            return True
        time.sleep(interval)
    return False


def _wait_for_inference_server(
    ip: str, port: int = 8081, timeout: int = 300, interval: int = 15
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                f"{SSH_USER}@{ip}",
                f"curl -s http://localhost:{port}/health | head -c 200",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if "ok" in r.stdout.lower() or "healthy" in r.stdout.lower():
            return True
        time.sleep(interval)
    return False


class SpotVMManager:
    def __init__(self, config: SpotVMConfig):
        self.config = config
        self.cfg = config  # consumer compatibility alias (cooperative_debate uses mgr.cfg)
        self._nic_name = ""
        self._pip_name = ""

    def create_vm(self, vm_name: str | None = None) -> Dict[str, Any]:
        cfg = self.config
        name = vm_name or _vm_name(cfg.label)

        print(f"  Creating spot VM '{name}' in {cfg.location}...")
        r = _az(
            "vm",
            "create",
            "--resource-group",
            self.config.resource_group,
            "--name",
            name,
            "--image",
            cfg.image_id(),
            "--size",
            cfg.vm_size,
            "--location",
            cfg.location,
            "--vnet-name",
            cfg.vnet_name,
            "--subnet",
            cfg.subnet_name,
            "--public-ip-sku",
            PUBLIC_IP_SKU,
            "--security-type",
            "Standard",
            "--priority",
            "Spot",
            "--eviction-policy",
            "Delete",
            "--max-price",
            str(cfg.max_price),
            "--admin-username",
            SSH_USER,
            "--ssh-key-values",
            os.path.expanduser(SSH_KEY_PATH),
            "--nic-delete-option",
            "Delete",
            "--os-disk-delete-option",
            "Delete",
            "--data-disk-delete-option",
            "Delete",
            "--storage-sku",
            "StandardSSD_LRS",
            "--os-disk-size-gb",
            "64",
            subscription=cfg.subscription_id,
        )
        if r.returncode != 0:
            raise RuntimeError(f"Failed to create VM '{name}': {r.stderr}")

        vm_info = json.loads(r.stdout)
        ip = vm_info.get("publicIpAddress") or self._get_ip(name)
        self.config.vm_name = name
        self.config.public_ip = ip
        print(f"  VM '{name}' created, IP={ip}")
        return {"name": name, "ip": ip}

    def _az_with_sub(self, args: list, timeout: int = 120) -> subprocess.CompletedProcess:
        """Run `az` with this config's subscription (consumer compatibility API)."""
        return _az(*args, subscription=self.config.subscription_id, timeout=timeout)

    def _get_public_ip(self) -> str:
        """Return the public IP of this config's vm_name (consumer compatibility API)."""
        ip = self._get_ip(self.config.vm_name)
        self.config.public_ip = ip
        return ip

    def _get_ip(self, vm_name: str) -> str:
        r = _az(
            "vm",
            "show",
            "--resource-group",
            self.config.resource_group,
            "--name",
            vm_name,
            "--query",
            "publicIpAddress",
            "--output",
            "tsv",
            subscription=self.config.subscription_id,
        )
        return r.stdout.strip()

    def poll_until_ready(self, ip: str, ssh_timeout: int = 180, llm_timeout: int = 300) -> bool:
        print(f"  Waiting for SSH on {ip} (timeout={ssh_timeout}s)...")
        if not _wait_for_ssh(ip, timeout=ssh_timeout):
            print(f"  SSH not available on {ip} within {ssh_timeout}s")
            return False

        print(f"  Waiting for llama-server on {ip}:{LLAMA_SERVER_PORT} (timeout={llm_timeout}s)...")
        if not _wait_for_inference_server(ip, timeout=llm_timeout):
            print(f"  llama-server not ready on {ip} within {llm_timeout}s")
            return False

        print(f"  VM {ip} ready for inference")
        return True

    def delete_vm(self, vm_name: str) -> bool:
        print(f"  Deleting spot VM '{vm_name}'...")
        r = _az(
            "vm",
            "delete",
            "--resource-group",
            self.config.resource_group,
            "--name",
            vm_name,
            "--yes",
            subscription=self.config.subscription_id,
        )
        if r.returncode != 0:
            print(f"  Failed to delete VM '{vm_name}': {r.stderr}")
            return False
        print(f"  VM '{vm_name}' deleted")
        return True

    def list_spot_vms(self) -> List[Dict[str, str]]:
        r = _az(
            "vm",
            "list",
            "-d",
            "--resource-group",
            self.config.resource_group,
            "--query",
            "[?priority=='Spot'].{name:name, vmId:id, powerState:powerState}",
            "--output",
            "json",
            subscription=self.config.subscription_id,
        )
        return json.loads(r.stdout) if r.stdout.strip() else []

    def wait_until_deleted(self, vm_name: str, timeout: int = 180, interval: int = 10) -> bool:
        """Poll until the VM no longer exists. True when removal is confirmed (quota freed)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = _az(
                "vm", "show", "--resource-group", self.config.resource_group,
                "--name", vm_name, "--query", "id", "--output", "tsv",
                subscription=self.config.subscription_id,
            )
            err = (r.stderr or "").lower()
            if r.returncode != 0 and ("notfound" in err or "not found" in err):
                return True
            if r.returncode == 0 and not r.stdout.strip():
                return True
            time.sleep(interval)
        return False

    def delete_vm_verified(self, vm_name: str, timeout: int = 180) -> bool:
        """Delete the VM and confirm it is gone (so the next spot create can proceed)."""
        self.delete_vm(vm_name)
        return self.wait_until_deleted(vm_name, timeout=timeout)

    def sweep_orphans(self, ttl_sec: int = 7200, dry_run: bool = False) -> List[str]:
        """Delete spot VMs older than ttl_sec (age from name epoch). Returns names acted on."""
        acted: List[str] = []
        for vm in self.list_spot_vms():
            name = vm.get("name", "")
            age = _vm_age_sec(name)
            if age is None or age < ttl_sec:
                continue
            if dry_run:
                print(f"  [dry-run] would delete {name} (age={age}s > ttl={ttl_sec}s)")
            else:
                print(f"  orphan {name} (age={age}s > ttl={ttl_sec}s) — deleting")
                self.delete_vm(name)
            acted.append(name)
        return acted
