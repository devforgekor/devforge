#!/usr/bin/env python3
# Status: experimental
# Path: azure_spot CLI entry (lib.infra.azure_spot.cli:main)
"""CLI entry point for Azure Spot VM lifecycle management."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from lib.infra.azure_spot.config import SPOT_CONFIGS, TUNNEL_PORT_BASE, SpotVMConfig
from lib.infra.azure_spot.manager import SpotVMManager
from lib.infra.azure_spot.orchestrator import SpotOrchestrator
from lib.infra.azure_spot.tunnel import close_spot_tunnel


def main():
    parser = argparse.ArgumentParser(description="Azure Spot VM lifecycle management")
    sub = parser.add_subparsers(dest="command")

    launch_p = sub.add_parser("launch", help="Launch spot VMs")
    launch_p.add_argument("--label", choices=list(SPOT_CONFIGS.keys()), help="Specific VM label")
    launch_p.add_argument("--ssh-timeout", type=int, default=180)
    launch_p.add_argument("--llm-timeout", type=int, default=300)

    status_p = sub.add_parser("status", help="List running VMs")
    status_p.add_argument("--label", choices=list(SPOT_CONFIGS.keys()), default="all")

    delete_p = sub.add_parser("delete", help="Delete a specific VM")
    delete_p.add_argument("label", choices=list(SPOT_CONFIGS.keys()))
    delete_p.add_argument("name", help="VM name to delete")

    sweep_p = sub.add_parser("sweep", help="Delete orphan spot VMs older than TTL")
    sweep_p.add_argument("--ttl", type=int, default=7200, help="Max age in seconds (default 7200)")
    sweep_p.add_argument("--dry-run", action="store_true", help="Only report, do not delete")

    destroy_p = sub.add_parser("destroy", help="Delete spot VMs and verify removal")
    destroy_p.add_argument("--label", choices=list(SPOT_CONFIGS.keys()), help="Limit to one label")

    sub.add_parser("verify", help="Verify no spot VMs remain (clean)")

    run_p = sub.add_parser("run", help="Provision, run command, then ALWAYS destroy+verify")
    run_p.add_argument("--label", choices=list(SPOT_CONFIGS.keys()), default="qwen3-30b")
    run_p.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run (use after --)")

    sub.add_parser("list-tunnels", help="List active tunnels")

    args = parser.parse_args()

    if args.command == "launch":
        configs = [SPOT_CONFIGS[args.label]] if args.label else None
        orch = SpotOrchestrator(configs)
        removed = orch.preflight_clean()
        if removed:
            print(f"  preflight removed leftovers: {removed}")
        results = orch.launch_all(ssh_timeout=args.ssh_timeout, llm_timeout=args.llm_timeout)
        for label, status in results.items():
            print(f"  {label}: {status}")
        return 0

    elif args.command == "status":
        if args.label != "all":
            configs = [SPOT_CONFIGS[args.label]]
        else:
            configs = SPOT_CONFIGS.values()
        for cfg in configs:
            mgr = SpotVMManager(cfg)
            vms = mgr.list_spot_vms()
            for vm in vms:
                print(f"  {cfg.label}: {vm['name']} ({vm.get('powerState', 'unknown')})")
        return 0

    elif args.command == "delete":
        cfg = SPOT_CONFIGS[args.label]
        SpotVMManager(cfg).delete_vm(args.name)
        return 0

    elif args.command == "sweep":
        orch = SpotOrchestrator()
        result = orch.sweep_orphans(ttl_sec=args.ttl, dry_run=args.dry_run)
        for label, acted in result.items():
            print(f"  {label}: {len(acted)} swept {acted}")
        return 0

    elif args.command == "destroy":
        cfg = [SPOT_CONFIGS[args.label]] if args.label else None
        orch = SpotOrchestrator(cfg)
        res = orch.teardown()
        for label, info in res.items():
            print(f"  {label}: deleted={info['deleted']} verified={info['verified']} remaining={info['remaining']}")
        clean = all(n == 0 for n in orch.verify_clean().values())
        print("  CLEAN — next create OK" if clean else "  WARN: VMs remain")
        return 0 if clean else 1

    elif args.command == "verify":
        orch = SpotOrchestrator()
        counts = orch.verify_clean()
        for label, n in counts.items():
            print(f"  {label}: {n} remaining")
        clean = all(n == 0 for n in counts.values())
        print("  CLEAN" if clean else "  WARN: VMs remain")
        return 0 if clean else 1

    elif args.command == "run":
        cfg = SPOT_CONFIGS[args.label]
        orch = SpotOrchestrator([cfg])
        ok = True
        try:
            orch.preflight_clean()
            results = orch.launch_all()
            print(f"  results: {results}")
            if results.get(cfg.label) in ("failed", "unhealthy", None):
                ok = False
            elif args.cmd:
                env = dict(os.environ)
                env["AZURE_SPOT_ENDPOINT"] = f"http://127.0.0.1:{TUNNEL_PORT_BASE}/v1"
                rc = subprocess.run(args.cmd, env=env).returncode
                ok = rc == 0
        finally:
            res = orch.teardown()
            clean = all(n == 0 for n in orch.verify_clean().values())
            print(f"  teardown: {res} clean={clean}")
            if not clean:
                ok = False
        return 0 if ok else 1

    elif args.command == "list-tunnels":
        import subprocess
        try:
            r = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=5)
            lines = [ln for ln in r.stdout.splitlines() if "ssh" in ln]
            print("\n".join(lines) if lines else "  (no active ssh tunnels)")
        except Exception as e:
            print(f"  list-tunnels error: {e}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
