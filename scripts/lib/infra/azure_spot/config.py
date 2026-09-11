#!/usr/bin/env python3
# Status: experimental
# Path: imported by — lib.infra.azure_spot.__init__, manager, orchestrator, cli
"""Azure Spot VM configuration (aligned to real Azure infra, 2026-09-11)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

SUBSCRIPTIONS: Dict[str, str] = {
    "account1": "a942e898-e1ee-47f4-b9b3-d9475672ff4e",
    "account2": "e71711e2-5df5-4259-bd0d-4bd58fd1ca67",
    "account3": "d0a7db48-d9a5-4e71-8425-90e90f541520",
}

LOCATION = "centralindia"
PUBLIC_IP_SKU = "Standard"
VM_SIZE = "Standard_FX2mds_v2"  # 2 vCPU / 42 GiB — fits LowPriorityCores (3)
SSH_USER = "azureuser"
LLAMA_SERVER_PORT = 8081
TUNNEL_PORT_BASE = 8085  # local tunnel ports start here (avoid 8081 embedder collision)
SSH_KEY_PATH = "~/.ssh/id_rsa.pub"


@dataclass
class SpotVMConfig:
    label: str
    subscription_id: str
    image: str
    gallery: str
    resource_group: str
    vnet_name: str
    subnet_name: str = "default"
    location: str = LOCATION
    vm_size: str = VM_SIZE
    account: str = ""
    vm_name: str = ""
    public_ip: str = ""
    max_price: float = -1.0  # Azure Spot max price; -1 = up to on-demand price (avoid no-alloc)

    def image_id(self) -> str:
        return (
            f"/subscriptions/{self.subscription_id}"
            f"/resourceGroups/{self.resource_group}/providers"
            f"/Microsoft.Compute/galleries/{self.gallery}/images/{self.image}/versions/latest"
        )


QWEN_SPOT_CONFIG = SpotVMConfig(
    label="qwen3-30b",
    subscription_id=SUBSCRIPTIONS["account1"],
    resource_group="rg-devforge-prod-cin",
    gallery="gallery_devforge_prod_cin",
    image="llm-qwen-27b",
    vnet_name="vm-devforge-prod-cin-vnet",
    account="account1",
)

# ── DEPRECATED (폐기 대상; 계정/SP 정보는 보존) ──────────────────────
NEMOTRON_SPOT_CONFIG = SpotVMConfig(
    label="nemotron3-nano",
    subscription_id=SUBSCRIPTIONS["account2"],
    resource_group="rg-devforge-llm-prod-cin",
    gallery="gallery_devforge_llm_prod_cin",
    image="",
    vnet_name="vm-devforge-llm-prod-cin-vnet",
    account="account2",
)

GEMMA_SPOT_CONFIG = SpotVMConfig(
    label="gemma-4-26b",
    subscription_id=SUBSCRIPTIONS["account3"],
    resource_group="rg-devforge-llm-judge-cin",
    gallery="gallery_devforge_llm_judge_cin",
    image="",
    vnet_name="vm-gemma-4-26b-spotVNET",
    account="account3",
)

# 활성: 단일 계정(account1). Spot LowPriorityCores 한도(3core)로 1대만 운용.
SPOT_CONFIGS: Dict[str, SpotVMConfig] = {
    "qwen3-30b": QWEN_SPOT_CONFIG,
}

DEPRECATED_CONFIGS: Dict[str, SpotVMConfig] = {
    "nemotron3-nano": NEMOTRON_SPOT_CONFIG,
    "gemma-4-26b": GEMMA_SPOT_CONFIG,
}
