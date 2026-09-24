#!/usr/bin/env python3
# Status: production
# Path: manual; systemd/user/devforge-mcp-inventory.service (later)
"""MCP server inventory drift check (OWASP MCP09 shadow MCP).

Compare the approved inventory (specs/mcp-inventory.yaml) against the live
opencode config. Exit 1 on unlisted servers or enabled-but-unapproved ones.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

REPO = Path(__file__).resolve().parents[1]
INVENTORY = REPO / "specs" / "mcp-inventory.yaml"


def load_inventory(path: str) -> Tuple[Dict[str, dict], List[str]]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    servers = {s["name"]: s for s in data.get("servers", [])}
    return servers, data.get("config_paths", [])


def load_live(config_paths: List[str]) -> Dict[str, dict]:
    live: Dict[str, dict] = {}
    for raw in config_paths:
        p = Path(os.path.expanduser(raw))
        if not p.is_file():
            continue
        data = json.loads(p.read_text())
        for name, cfg in (data.get("mcp") or {}).items():
            enabled = cfg.get("enabled", True) if isinstance(cfg, dict) else True
            live[name] = {"enabled": bool(enabled), "path": str(p)}
    return live


def drift(inventory: Dict[str, dict], live: Dict[str, dict]) -> Dict[str, List[str]]:
    return {
        "unlisted": sorted(set(live) - set(inventory)),
        "enabled_not_approved": sorted(
            n for n, v in live.items()
            if v["enabled"] and n in inventory and not inventory[n].get("approved")
        ),
        "listed_missing": sorted(set(inventory) - set(live)),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="MCP inventory drift check")
    parser.add_argument("--inventory", default=str(INVENTORY))
    args = parser.parse_args(argv)

    inventory, paths = load_inventory(args.inventory)
    live = load_live(paths)
    result = drift(inventory, live)

    if result["unlisted"]:
        print("UNLISTED (shadow MCP):", result["unlisted"])
    if result["enabled_not_approved"]:
        print("ENABLED but NOT APPROVED:", result["enabled_not_approved"])
    if result["listed_missing"]:
        print("INFO listed but absent from live:", result["listed_missing"])

    if result["unlisted"] or result["enabled_not_approved"]:
        return 1
    enabled = sum(1 for v in live.values() if v["enabled"])
    print(f"mcp inventory: OK ({len(inventory)} listed, {enabled} enabled)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
