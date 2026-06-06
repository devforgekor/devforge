#!/usr/bin/env python3
# Status: experimental
# Path: none — library
"""Azure Spot VM lifecycle management for cooperative LLM debate.

Creates spot VMs from Compute Gallery golden images, waits for SSH + llama-server
health, then deletes after debate rounds complete.

Three-subscription model:
  - Account 1 (a942e898-...): Qwen3-30B-A3B, gallery img-qwen3-30b-a3b-v3 (centralindia)
  - Account 2 (e71711e2-...): Nemotron-3-Nano-30B-A3B, gallery img-nemotron3-nano-30b-spot (indonesiacentral)
  - Account 3 (d0a7db48-...): Gemma-4-26B-A4B, gallery img-gemma-4-26b-spot (centralindia)
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Tuple

# ── Configuration ───────────────────────────────────────────────────────────

# Azure account/subscription map
SUBSCRIPTIONS: Dict[str, str] = {
    "account1": "a942e898-e1ee-47f4-b9b3-d9475672ff4e",  # Qwen gallery
    "account2": "e71711e2-5df5-4259-bd0d-4bd58fd1ca67",  # Nemotron VM (SP)
    "account3": "d0a7db48-d9a5-4e71-8425-90e90f541520",  # Gemma Judge VM
}

# SSH key pairs (public keys extracted from PEM files)
# Unified SSH key for all Azure LLM VMs
_UNIFIED_SSH_KEY = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQDOonBd5j4ZrVrxg30AjgFBAhi08BKkcH8IEhdUMeKCi6xyJmhWK96LgzlCIQTyjLVheo+qxq0A6mIafwE6zGqDVFuIYsjLPelaSHd3VtCPaLPsaZUp+FMg1qTGc6OXO4koB4b80jpsdb3ZGKHoceRCjMPDUSoXtzBaJwcveF5ENoHV9SwKovILXlqmPdFpGfNVZhpxjbhszdbx5ixABpOUj5uCRKEhWFF0N1+L4Ep22P5iZWIb0R5JQw2xV71xAiE5NCngFea3KD7vK6SJuXwcmOpoYvidS1QSp8k0zdDHfJ4YCNeI7ajQoLLUMYMQf4As4X1JArnoyh14eChnepnJbwUuyEkpF71psE7zAbBysl64G6WyhagDCvmCFsQGujymddOUQhB0T5K9Gdp7vxhN71xTQhtkSOzexFvneavGXFHMDgH9vXGMs9th6DminKza5Gi6uvSS++ZRUt0i6JngdHCzU5sy51jBaMWktM5OjwyHlohGc3k4ronBZIhQN/5cy8QfpSlGz4jfTdmXOr0EEOrCUlh9iBs835YnDiLDVFLViAaHXuF9lKEryLSmq+/P020x40Q7hn8fLCrk7+kjphOohbIRQh5H8gUcrWv1KCRIxIlS8NqQII3w5PgG6o3YcaAZIlVpm6RKSIipXojt00XO2p/PQJRhs5NAPqeo4Q== azure-llm-unified"

SSH_PUBLIC_KEYS: Dict[str, str] = {
    "qwen": _UNIFIED_SSH_KEY,
    "nemotron": _UNIFIED_SSH_KEY,
    "gemma": _UNIFIED_SSH_KEY,
}


@dataclass
class SpotVMConfig:
    """Configuration for a spot VM type deployed from a gallery image."""

    # Human label
    label: str
    # Azure subscription to deploy into
    subscription_id: str
    resource_group: str
    location: str
    # Compute Gallery image reference
    gallery_name: str
    gallery_rg: str
    image_definition: str
    # VM spec
    vm_size: str = "Standard_FX2ms_v2"
    admin_username: str = "azureuser"
    ssh_pubkey: str = ""
    # llama-server on VM
    llama_port: int = 400
    os_disk_size: int = 30
    # Networking — filled after create
    vm_name: str = ""
    public_ip: str = ""
    # Specialized image (no OS profile) vs Generalized (requires OS profile)
    is_specialized: bool = True
    # Security type for TrustedLaunch images (None = default)
    security_type: str = ""
    # Shell command to run on VM after SSH is up (e.g., fix systemd config)
    post_create_cmd: str = ""


# ── Pre-defined spot VM configs ─────────────────────────────────────────────

QWEN_SPOT_CONFIG = SpotVMConfig(
    label="qwen3-30b-a3b",
    subscription_id=SUBSCRIPTIONS["account1"],
    resource_group="RG-DEVFORGE-PROD-CIN",
    location="centralindia",
    gallery_name="gallery_devforge_prod_cin",
    gallery_rg="RG-DEVFORGE-PROD-CIN",
    image_definition="img-qwen3-30b-a3b-v3",
    vm_size="Standard_FX2ms_v2",
    admin_username="azureuser",
    ssh_pubkey=SSH_PUBLIC_KEYS["qwen"],
    llama_port=400,
    os_disk_size=30,
)

NEMOTRON_SPOT_CONFIG = SpotVMConfig(
    label="nemotron3-nano-30b",
    subscription_id=SUBSCRIPTIONS["account2"],
    resource_group="RG-DEVFORGE-LLM-PROD-CIN",
    location="indonesiacentral",
    gallery_name="gallery_devforge_llm_prod_cin",
    gallery_rg="RG-DEVFORGE-LLM-PROD-CIN",
    image_definition="img-nemotron3-nano-30b-spot",
    vm_size="Standard_FX2ms_v2",
    admin_username="azureuser",
    ssh_pubkey=SSH_PUBLIC_KEYS["nemotron"],
    llama_port=400,
    os_disk_size=30,
)

GEMMA_SPOT_CONFIG = SpotVMConfig(
    label="gemma-4-26b",
    subscription_id=SUBSCRIPTIONS["account3"],
    resource_group="RG-DEVFORGE-LLM-JUDGE-CIN",
    location="centralindia",
    gallery_name="gallery_devforge_llm_judge_cin",
    gallery_rg="RG-DEVFORGE-LLM-JUDGE-CIN",
    image_definition="img-gemma-4-26b-spot",
    vm_size="Standard_E2as_v4",
    admin_username="azureuser",
    ssh_pubkey=SSH_PUBLIC_KEYS["gemma"],
    llama_port=8080,
    os_disk_size=30,
    is_specialized=False,
    security_type="TrustedLaunch",
    # E2as_v4 has 2 vCPUs — fix golden image's 4-thread default
    post_create_cmd=(
        "sudo sed -i 's/--threads [0-9]*/--threads 2/; s/--threads-batch [0-9]*/--threads-batch 2/'"
        " /etc/systemd/system/llama-server.service"
        " && sudo systemctl daemon-reload"
        " && sudo systemctl restart llama-server"
    ),
)


# ═══════════════════════════════════════════════════════════════════════════════
# Spot VM Manager
# ═══════════════════════════════════════════════════════════════════════════════

class SpotVMManager:
    """Manage a single Azure spot VM created from a Compute Gallery image."""

    def __init__(self, config: SpotVMConfig, session_id: str):
        self.cfg = config
        self.session_id = session_id
        # Unique VM name: spot-{label}-{session_short} (lowercase for DNS compliance)
        short_id = session_id.replace(":", "").replace("-", "")[-8:].lower()
        self.cfg.vm_name = f"spot-{config.label}-{short_id}"
        self._nic_name: str = ""
        self._pip_name: str = ""

    # ── Azure CLI helpers ────────────────────────────────────────────────

    def _az_with_sub(self, args: list, timeout: int = 120) -> subprocess.CompletedProcess:
        """Run az CLI with explicit subscription set. Uses shlex.join for safety."""
        quoted = shlex.join(["az"] + args)
        cmd_str = f"az account set -s {shlex.quote(self.cfg.subscription_id)} && {quoted}"
        return subprocess.run(
            cmd_str, shell=True, capture_output=True, text=True, timeout=timeout
        )

    @property
    def image_resource_id(self) -> str:
        """Full ARM resource ID for the gallery image (latest version)."""
        return (
            f"/subscriptions/{self.cfg.subscription_id}"
            f"/resourceGroups/{self.cfg.gallery_rg}"
            f"/providers/Microsoft.Compute/galleries/{self.cfg.gallery_name}"
            f"/images/{self.cfg.image_definition}/versions/latest"
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def create(self) -> bool:
        """Create a spot VM from the gallery image. Returns True when VM is running."""
        print(f"\n  [spot:{self.cfg.label}] Creating spot VM '{self.cfg.vm_name}'...")
        print(f"    Location: {self.cfg.location} | Size: {self.cfg.vm_size}")
        print(f"    Image: {self.cfg.image_definition}")

        # Build args — conditionally add --specialized for specialized images
        vm_args = [
            "vm", "create",
            "--name", self.cfg.vm_name,
            "--resource-group", self.cfg.resource_group,
            "--location", self.cfg.location,
            "--image", self.image_resource_id,
            "--size", self.cfg.vm_size,
            "--priority", "Spot",
            "--max-price", "-1",
            "--eviction-policy", "Deallocate",
            "--os-disk-size-gb", str(self.cfg.os_disk_size),
            "--generate-ssh-keys",
            "--public-ip-address-dns-name", self.cfg.vm_name,
        ]
        if self.cfg.is_specialized:
            vm_args.insert(-2, "--specialized")  # before --public-ip-address-dns-name
        if not self.cfg.is_specialized:
            vm_args.insert(-2, "--admin-username")
            vm_args.insert(-2, self.cfg.admin_username)
            vm_args.insert(-2, "--ssh-key-values")
            vm_args.insert(-2, self.cfg.ssh_pubkey)
        if self.cfg.security_type:
            # insert before --public-ip-address-dns-name, value then flag
            vm_args.insert(-2, self.cfg.security_type)
            vm_args.insert(-3, "--security-type")

        result = self._az_with_sub(vm_args + ["--output", "none"], timeout=300)

        if result.returncode != 0:
            print(f"  [spot:{self.cfg.label}] ERROR creating VM:")
            stderr = result.stderr
            # Filter out known non-error warnings
            for line in stderr.splitlines():
                if "consumed" not in line.lower() and line.strip():
                    print(f"    {line[:200]}")
            if "ResourceNotFound" in stderr and "galleries" in stderr:
                print(f"  [spot:{self.cfg.label}] Gallery image not found — image may not exist yet")
            return False

        self._nic_name = f"{self.cfg.vm_name}VMNic"  # Azure's auto-generated NIC name
        self._pip_name = f"{self.cfg.vm_name}PublicIP"

        # az vm create --output none gives no JSON; query IP separately
        self.cfg.public_ip = self._get_public_ip()
        if not self.cfg.public_ip:
            print(f"  [spot:{self.cfg.label}] VM created but could not resolve public IP")
            return False

        print(f"  [spot:{self.cfg.label}] VM created: {self.cfg.public_ip}")
        return True

    def _get_public_ip(self) -> str:
        """Query the public IP of this VM."""
        try:
            result = self._az_with_sub([
                "network", "public-ip", "show",
                "--name", self._pip_name,
                "--resource-group", self.cfg.resource_group,
                "--query", "ipAddress",
                "-o", "tsv",
            ], timeout=30)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass

        # Fallback: search by VM name pattern in NIC
        try:
            result = self._az_with_sub([
                "vm", "show",
                "-n", self.cfg.vm_name,
                "-g", self.cfg.resource_group,
                "--query", "networkProfile.networkInterfaces[0].id",
                "-o", "tsv",
            ], timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                nic_id = result.stdout.strip()
                pip_result = self._az_with_sub([
                    "network", "nic", "show",
                    "--id", nic_id,
                    "--query", "ipConfigurations[0].publicIpAddress.ipAddress",
                    "-o", "tsv",
                ], timeout=30)
                if pip_result.returncode == 0:
                    return pip_result.stdout.strip()
        except Exception:
            pass

        return ""

    @property
    def _ssh_key_path(self) -> str:
        """SSH private key path for this VM type."""
        key_path = os.path.expanduser("~/.ssh/azurellm.pem")
        if os.path.exists(key_path):
            return key_path
        return os.path.expanduser("~/.ssh/id_rsa")

    def _ssh_base_args(self) -> list:
        """Base SSH arguments including identity file."""
        return [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes",
            "-i", self._ssh_key_path,
        ]

    def wait_ready(self, timeout: int = 300) -> bool:
        """Wait for VM SSH + llama-server health check.

        First waits for SSH (port 22), then polls llama-server :400/health.
        """
        ip = self.cfg.public_ip
        if not ip:
            print(f"  [spot:{self.cfg.label}] No public IP — cannot check health")
            return False

        print(f"  [spot:{self.cfg.label}] Waiting for health (timeout={timeout}s)...")

        # Phase 1: Wait for SSH (port 22)
        ssh_ok = self._wait_ssh(ip, timeout=min(timeout, 180))
        if not ssh_ok:
            print(f"  [spot:{self.cfg.label}] SSH did not come up")
            return False

        # Phase 1.5: Run post-create command (e.g., fix systemd service for vCPU count)
        if self.cfg.post_create_cmd:
            print(f"  [spot:{self.cfg.label}] Running post-create setup...")
            try:
                result = subprocess.run(
                    self._ssh_base_args() + [
                        f"{self.cfg.admin_username}@{ip}",
                        self.cfg.post_create_cmd],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    print(f"  [spot:{self.cfg.label}] Post-create WARNING: {result.stderr[:200]}")
            except Exception as e:
                print(f"  [spot:{self.cfg.label}] Post-create ERROR: {e}")

        # Phase 2: Wait for llama-server health
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            try:
                result = subprocess.run(
                    self._ssh_base_args() + [
                        f"{self.cfg.admin_username}@{ip}",
                        f"curl -s http://localhost:{self.cfg.llama_port}/health"],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0:
                    try:
                        health = json.loads(result.stdout)
                        if health.get("status") == "ok":
                            elapsed = time.monotonic() - start
                            print(f"  [spot:{self.cfg.label}] Healthy after {elapsed:.0f}s")
                            return True
                    except json.JSONDecodeError:
                        pass
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                pass
            time.sleep(5)

        print(f"  [spot:{self.cfg.label}] Health TIMEOUT after {timeout}s")
        return False

    def _wait_ssh(self, ip: str, timeout: int = 180) -> bool:
        """Wait for SSH to become available on the VM."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            try:
                result = subprocess.run(
                    self._ssh_base_args() + [f"{self.cfg.admin_username}@{ip}", "echo ok"],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0 and "ok" in result.stdout:
                    elapsed = time.monotonic() - start
                    print(f"  [spot:{self.cfg.label}] SSH ready after {elapsed:.0f}s")
                    return True
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                pass
            time.sleep(10)

        return False

    def deallocate(self) -> bool:
        """Deallocate (stop) the VM — preserves OS disk for reuse."""
        print(f"  [spot:{self.cfg.label}] Deallocating {self.cfg.vm_name}...")
        result = self._az_with_sub([
            "vm", "deallocate",
            "--name", self.cfg.vm_name,
            "--resource-group", self.cfg.resource_group,
        ], timeout=120)
        if result.returncode == 0:
            print(f"  [spot:{self.cfg.label}] Deallocated")
            return True
        print(f"  [spot:{self.cfg.label}] Deallocate failed: {result.stderr[:200]}")
        return False

    def delete(self) -> bool:
        """Delete VM and all associated resources (NIC, NSG, public IP, disk)."""
        print(f"  [spot:{self.cfg.label}] Deleting {self.cfg.vm_name} and resources...")

        # Delete VM
        result = self._az_with_sub([
            "vm", "delete",
            "--name", self.cfg.vm_name,
            "--resource-group", self.cfg.resource_group,
            "--yes",
            "--force-deletion", "false",
        ], timeout=120)
        if result.returncode != 0:
            print(f"  [spot:{self.cfg.label}] VM delete warning: {result.stderr[:200]}")

        # Delete associated resources (best-effort)
        for cmd_type, name, res_name in [
            ("network", "nic", self._nic_name),
            ("network", "nsg", f"{self.cfg.vm_name}NSG"),
            ("network", "public-ip", self._pip_name),
            ("disk", "disk", f"{self.cfg.vm_name}OSDisk"),
        ]:
            if not res_name:  # skip if name wasn't populated
                continue
            try:
                if cmd_type == "network":
                    self._az_with_sub([
                        "network", name, "delete",
                        "--name", res_name,
                        "--resource-group", self.cfg.resource_group,
                    ], timeout=30)
                else:
                    self._az_with_sub([
                        "disk", "delete",
                        "--name", res_name,
                        "--resource-group", self.cfg.resource_group,
                        "--yes",
                    ], timeout=30)
            except Exception:
                pass  # best-effort cleanup

        print(f"  [spot:{self.cfg.label}] Deleted")
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════════════════

class SpotOrchestrator:
    """Orchestrate multiple spot VMs for a cooperative debate session."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.managers: Dict[str, SpotVMManager] = {}

    def add(self, label: str, config: SpotVMConfig) -> SpotVMManager:
        """Register a VM type for this session."""
        mgr = SpotVMManager(config, self.session_id)
        self.managers[label] = mgr
        return mgr

    def provision_all(self) -> bool:
        """Create all registered spot VMs. Returns True if all succeeded."""
        ok = True
        for label, mgr in self.managers.items():
            if not mgr.create():
                print(f"  [orch] Failed to create {label} — continuing with remaining...")
                ok = False
            else:
                time.sleep(5)  # Stagger VM creation slightly

        # Wait for all successfully-created VMs to be healthy
        for label, mgr in self.managers.items():
            if mgr.cfg.public_ip:
                if not mgr.wait_ready(timeout=300):
                    print(f"  [orch] {label} did not become ready")
                    ok = False

        return ok

    def terminate_all(self) -> None:
        """Delete all spot VMs. Best-effort — never raises."""
        for label, mgr in self.managers.items():
            try:
                mgr.delete()
            except Exception as e:
                print(f"  [orch] Error deleting {label}: {e}")

    def get_connection_info(self) -> Dict[str, Tuple[str, int]]:
        """Return {(label, ssh_alias): (public_ip, port)} for ready VMs."""
        info: Dict[str, Tuple[str, int]] = {}
        for label, mgr in self.managers.items():
            if mgr.cfg.public_ip:
                info[label] = (mgr.cfg.public_ip, mgr.cfg.llama_port)
        return info


# ═══════════════════════════════════════════════════════════════════════════════
# SSH tunnel management for spot VMs
# ═══════════════════════════════════════════════════════════════════════════════

def open_spot_tunnel(label: str, ip: str, remote_port: int, local_port: int,
                     ssh_key_path: str) -> bool:
    """Open SSH tunnel to a spot VM's llama-server. Returns True on success."""
    print(f"  [tunnel:spot] Opening :{local_port} -> {ip}:{remote_port} ({label})")

    # Kill any existing tunnel on this port
    try:
        result = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            if f":{local_port}" in line and "ssh" in line.lower():
                m = re.search(r"pid=(\d+)", line)
                if m:
                    try:
                        os.kill(int(m.group(1)), 15)
                    except Exception:
                        pass
    except Exception:
        pass

    try:
        tunnel_result = subprocess.run(
            [
                "ssh", "-f", "-N",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=60",
                "-o", "ServerAliveCountMax=3",
                "-i", ssh_key_path,
                "-L", f"{local_port}:localhost:{remote_port}",
                f"azureuser@{ip}",
            ],
            capture_output=True, timeout=15,
        )
        if tunnel_result.returncode != 0:
            print(f"  [tunnel:spot] ERROR: {tunnel_result.stderr.decode()}")
            return False
    except subprocess.TimeoutExpired:
        print(f"  [tunnel:spot] ERROR: SSH tunnel timeout to {ip}")
        return False
    except Exception as e:
        print(f"  [tunnel:spot] ERROR: {e}")
        return False

    time.sleep(1)
    return True


def close_spot_tunnel(local_port: int) -> None:
    """Kill SSH tunnel on the given local port."""
    try:
        result = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            if f":{local_port}" in line and "ssh" in line.lower():
                m = re.search(r"pid=(\d+)", line)
                if m:
                    try:
                        os.kill(int(m.group(1)), 15)
                        print(f"  [tunnel:spot] Closed :{local_port}")
                    except Exception:
                        pass
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Azure Spot VM lifecycle for debate")
    sub = ap.add_subparsers(dest="command")

    # create
    p_create = sub.add_parser("create", help="Create a spot VM")
    p_create.add_argument("--label", required=True, choices=["qwen", "nemotron", "gemma"])
    p_create.add_argument("--session-id", required=True)

    # wait
    p_wait = sub.add_parser("wait", help="Wait for VM health")
    p_wait.add_argument("--label", required=True, choices=["qwen", "nemotron", "gemma"])
    p_wait.add_argument("--ip", required=True)
    p_wait.add_argument("--timeout", type=int, default=300)

    # delete
    p_del = sub.add_parser("delete", help="Delete spot VM and resources")
    p_del.add_argument("--label", required=True, choices=["qwen", "nemotron", "gemma"])
    p_del.add_argument("--vm-name", required=True)
    p_del.add_argument("--pip-name", required=True)
    p_del.add_argument("--nic-name", required=True)

    args = ap.parse_args()

    if args.command == "create":
        if args.label == "qwen":
            cfg = QWEN_SPOT_CONFIG
        elif args.label == "nemotron":
            cfg = NEMOTRON_SPOT_CONFIG
        else:
            cfg = GEMMA_SPOT_CONFIG
        mgr = SpotVMManager(cfg, args.session_id)
        if mgr.create():
            print(json.dumps({
                "vm_name": mgr.cfg.vm_name,
                "public_ip": mgr.cfg.public_ip,
                "nic_name": mgr._nic_name,
                "pip_name": mgr._pip_name,
            }))
        else:
            import sys
            sys.exit(1)

    elif args.command == "wait":
        if args.label == "qwen":
            cfg = QWEN_SPOT_CONFIG
        elif args.label == "nemotron":
            cfg = NEMOTRON_SPOT_CONFIG
        else:
            cfg = GEMMA_SPOT_CONFIG
        mgr = SpotVMManager(cfg, "cli-wait")
        mgr.cfg.public_ip = args.ip
        if not mgr.wait_ready(timeout=args.timeout):
            import sys
            sys.exit(1)

    elif args.command == "delete":
        if args.label == "qwen":
            cfg = QWEN_SPOT_CONFIG
        elif args.label == "nemotron":
            cfg = NEMOTRON_SPOT_CONFIG
        else:
            cfg = GEMMA_SPOT_CONFIG
        mgr = SpotVMManager(cfg, "cli-delete")
        mgr.cfg.vm_name = args.vm_name
        mgr._pip_name = args.pip_name
        mgr._nic_name = args.nic_name
        mgr.delete()


if __name__ == "__main__":
    main()
