#!/usr/bin/env python3.12
# Status: production
# Path: systemd/user/devforge-mcp-inventory.service
"""MCP server inventory drift check (OWASP MCP09 shadow MCP).

--inventory mode: approved inventory (specs/mcp-inventory.yaml) vs live opencode
config; exit 1 on unlisted servers or enabled-but-unapproved ones.
--m0 mode: M0 recheck — tool allowlist must equal the 12-tool contract with a
deny-all wildcard, and opencode.log must show zero non-contract devforge-mcp
tool evaluations since the M0 cutoff; every run appends evidence to
logs/m0_recheck.jsonl (daily timer).
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

REPO = Path(__file__).resolve().parents[1]
INVENTORY = REPO / "specs" / "mcp-inventory.yaml"
CONTRACT = REPO / "specs" / "mcp-contract.json"
TOOL_PREFIX = "devforge-mcp_"
WILDCARD = TOOL_PREFIX + "*"
M0_SINCE = "2026-09-26T00:00:00+00:00"
M0_EVIDENCE = REPO / "logs" / "m0_recheck.jsonl"
OPENCODE_CONFIG = Path.home() / ".config" / "opencode" / "opencode.json"
OPENCODE_LOG = Path.home() / ".local" / "share" / "opencode" / "log" / "opencode.log"
EVAL_RE = re.compile(r"^timestamp=(\S+)\s.*?message=evaluated permission=(\S+) pattern=")


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
            n
            for n, v in live.items()
            if v["enabled"] and n in inventory and not inventory[n].get("approved")
        ),
        "listed_missing": sorted(set(inventory) - set(live)),
    }


def load_contract(path: Path = CONTRACT) -> List[str]:
    data = json.loads(Path(path).read_text())
    return [t["name"] for t in data.get("tools", [])]


def load_tool_allowlist(config_path: Path) -> Dict[str, bool]:
    data = json.loads(Path(config_path).read_text())
    tools = data.get("tools") or {}
    return {
        k: bool(v) for k, v in tools.items() if isinstance(k, str) and k.startswith(TOOL_PREFIX)
    }


def check_allowlist(allow: Dict[str, bool], contract: List[str]) -> Dict[str, object]:
    contract_set = set(contract)
    return {
        "wildcard_ok": allow.get(WILDCARD) is False,
        "wildcard": allow.get(WILDCARD),
        "noncontract_enabled": sorted(
            k
            for k, v in allow.items()
            if k != WILDCARD and v and k[len(TOOL_PREFIX) :] not in contract_set
        ),
        "contract_missing": sorted(
            n for n in contract_set if not allow.get(TOOL_PREFIX + n, False)
        ),
    }


def scan_m0_log(log_path: Path, since: str, contract: List[str]) -> Dict[str, object]:
    contract_set = set(contract)
    since_dt = datetime.fromisoformat(since)
    path = Path(log_path)
    if not path.is_file():
        return {"present": False, "evaluations": 0, "noncontract": []}
    evaluations = 0
    offenders: Dict[str, dict] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = EVAL_RE.match(line)
            if not m or not m.group(2).startswith(TOOL_PREFIX):
                continue
            ts_s = m.group(1)
            try:
                ts = datetime.fromisoformat(ts_s)
            except ValueError:
                continue
            if ts < since_dt:
                continue
            evaluations += 1
            name = m.group(2)[len(TOOL_PREFIX) :]
            if name in contract_set:
                continue
            rec = offenders.setdefault(
                name, {"tool": TOOL_PREFIX + name, "first_ts": ts_s, "last_ts": ts_s, "count": 0}
            )
            rec["last_ts"] = ts_s
            rec["count"] += 1
    return {"present": True, "evaluations": evaluations, "noncontract": list(offenders.values())}


def m0_check(
    config_path: Path = OPENCODE_CONFIG,
    log_path: Path = OPENCODE_LOG,
    evidence_path: Path = M0_EVIDENCE,
    since: str = M0_SINCE,
) -> int:
    violations: List[str] = []
    try:
        contract = load_contract()
    except (OSError, json.JSONDecodeError) as exc:
        violations.append(f"contract unreadable: {exc}")
        contract = []
    allow: Dict[str, bool] = {}
    try:
        allow = load_tool_allowlist(config_path)
    except (OSError, json.JSONDecodeError) as exc:
        violations.append(f"allowlist unreadable: {exc}")
    al = check_allowlist(allow, contract) if contract else {}
    scan = scan_m0_log(log_path, since, contract)
    if not al.get("wildcard_ok", False):
        violations.append(f"{WILDCARD}={al.get('wildcard')!r} (must be False)")
    if al.get("noncontract_enabled"):
        violations.append(f"non-contract tool enabled: {al['noncontract_enabled']}")
    if al.get("contract_missing"):
        violations.append(f"contract tool missing from allowlist: {al['contract_missing']}")
    if not scan["present"]:
        violations.append(f"log missing: {log_path}")
    if scan["noncontract"]:
        violations.append(f"non-contract calls since {since}: {scan['noncontract']}")
    record = {
        "check": "m0",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since,
        "status": "ok" if not violations else "violated",
        "contract_count": len(contract),
        "window_evaluations": scan["evaluations"],
        "allowlist": {
            "wildcard_ok": bool(al.get("wildcard_ok")),
            "noncontract_enabled": al.get("noncontract_enabled", []),
            "contract_missing": al.get("contract_missing", []),
        },
        "log_noncontract": scan["noncontract"],
        "violations": violations,
    }
    try:
        evidence_path = Path(evidence_path)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        with open(evidence_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"M0 evidence write failed: {exc}", file=sys.stderr)
        return 1
    if violations:
        for v in violations:
            print(f"M0 VIOLATION: {v}")
        print(f"m0 recheck: VIOLATED ({len(violations)} issue(s), since={since})")
        return 1
    print(
        f"m0 recheck: OK (contract={len(contract)} tools, "
        f"window evals={scan['evaluations']}, non-contract=0, since={since})"
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="MCP inventory drift check")
    parser.add_argument("--inventory", default=str(INVENTORY))
    parser.add_argument(
        "--m0",
        action="store_true",
        help="M0 recheck: contract-only tool allowlist + zero non-contract calls",
    )
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

    rc = 1 if (result["unlisted"] or result["enabled_not_approved"]) else 0
    if not rc:
        enabled = sum(1 for v in live.values() if v["enabled"])
        print(f"mcp inventory: OK ({len(inventory)} listed, {enabled} enabled)")
    if args.m0:
        rc = max(rc, m0_check())
    return rc


if __name__ == "__main__":
    sys.exit(main())
