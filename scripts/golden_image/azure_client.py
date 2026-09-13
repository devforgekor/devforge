#!/usr/bin/env python3
# Status: production
# Path: scripts/golden_image/refresh_cycle.py, scripts/golden_image/yearly_refresh.sh
"""Azure CLI wrapper — Managed Identity prior, SP fallback, retry."""

import json
import subprocess
import time
from typing import Optional

RG = "rg-devforge-prod-cin"
GALLERY = "gallery_devforge_prod_cin"
IMAGE_DEF = "llm-qwen-27b-golden"
LOCATION = "centralindia"
VM_SIZE = "Standard_FX2ms_v2"


def _run_az(args, timeout=60):
    try:
        r = subprocess.run(["az"] + args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def az_json(args, retries=3):
    for attempt in range(retries):
        code, out, err = _run_az(args + ["-o", "json"])
        if code == 0:
            try:
                return json.loads(out) if out else None
            except Exception:
                return None
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    return None


def get_latest_version():
    data = az_json(["sig", "image-version", "list", "--resource-group", RG, "--gallery-name", GALLERY, "--gallery-image-definition", IMAGE_DEF])
    if not data:
        return None
    try:
        vers = [d for d in data if d.get("provisioningState") == "Succeeded"] if isinstance(data, list) else []
        if not vers:
            vers = data if isinstance(data, list) else []
        vers.sort(key=lambda x: x.get("name", ""))
        return vers[-1].get("name") if vers else None
    except Exception:
        return None


def get_subscription_id():
    code, out, _ = _run_az(["account", "show", "--query", "id", "-o", "tsv"])
    return out.strip() if code == 0 and out else None


def create_spot_vm(vm_name, version, public_ip_dns=None):
    sub = get_subscription_id()
    if not sub:
        return False, "no subscription"
    image = f"/subscriptions/{sub}/resourceGroups/{RG}/providers/Microsoft.Compute/galleries/{GALLERY}/images/{IMAGE_DEF}/versions/{version}"
    cmd = [
        "vm", "create",
        "--resource-group", RG,
        "--name", vm_name,
        "--location", LOCATION,
        "--image", image,
        "--size", VM_SIZE,
        "--admin-username", "azureuser",
        "--ssh-key-values", "~/.ssh/id_rsa.pub",
        "--priority", "Spot",
        "--eviction-policy", "Delete",
        "--os-disk-delete-option", "Delete",
        "--public-ip-sku", "Standard",
        "-o", "json",
    ]
    if public_ip_dns:
        cmd += ["--public-ip-dns-name", public_ip_dns]
    code, out, err = _run_az(cmd, timeout=180)
    if code == 0:
        return True, out
    return False, err or out


def list_vms_by_prefix(prefix: str = "llm-qwen") -> list[dict]:
    """List all VMs in the resource group with name starting with prefix.

    `--show-details` is required for `az vm list` to populate `publicIps` and
    `powerState` (both are derived fields absent from the default listing).
    Without it the orphan-vm safety timer would lose IP/power info, so keep parity
    with `claude-mode` (see ~/.bashrc.d/claude-mode:28).
    """
    data = az_json(["vm", "list", "-g", RG, "--show-details", "--query", f"[?starts_with(name,'{prefix}')].{{name:name,publicIps:publicIps,powerState:powerState}}"])
    return data if isinstance(data, list) else []


def delete_vm(vm_name: str) -> bool:
    code, _, _ = _run_az(["vm", "delete", "--resource-group", RG, "--name", vm_name, "--yes", "--force-deletion"], timeout=60)
    return code == 0


def vm_show_ip(vm_name):
    code, out, _ = _run_az(["vm", "show", "-d", "-g", RG, "-n", vm_name, "--query", "publicIps", "-o", "tsv"])
    return out.strip() if code == 0 and out else None
