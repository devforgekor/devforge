#!/usr/bin/env python3.12
# Status: production
# Path: tests/fitness/test_mcp_inventory.py — specs/mcp-inventory.yaml
"""MCP server inventory validity + live drift (OWASP MCP09 shadow MCP) + M0 recheck."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from mcp_inventory_check import (  # noqa: E402
    INVENTORY,
    OPENCODE_CONFIG,
    TOOL_PREFIX,
    WILDCARD,
    check_allowlist,
    drift,
    load_contract,
    load_inventory,
    load_live,
    load_tool_allowlist,
    m0_check,
    scan_m0_log,
)

M0_SINCE = "2026-09-26T00:00:00+00:00"


def _inventory():
    return load_inventory(str(INVENTORY))


def test_inventory_should_have_required_fields():
    servers, _ = _inventory()
    for name, entry in servers.items():
        for field in ("name", "type", "enabled", "approved", "owner", "purpose"):
            assert field in entry, f"{name} missing {field}"


def test_inventory_should_not_enable_unapproved():
    servers, _ = _inventory()
    bad = sorted(n for n, s in servers.items() if s["enabled"] and not s["approved"])
    assert not bad, f"enabled but not approved: {bad}"


def test_live_config_should_match_inventory():
    servers, paths = _inventory()
    live = load_live(paths)
    if not live:
        pytest.skip("no live opencode config on this host")
    result = drift(servers, live)
    assert not (result["unlisted"] or result["enabled_not_approved"]), result


def test_contract_should_expose_12_distinct_tool_names():
    contract = load_contract()
    assert len(contract) == 12, contract
    assert len(set(contract)) == len(contract)
    assert "obs_write" in contract


def test_check_allowlist_should_pass_when_wildcard_false_and_contract_true():
    contract = load_contract()
    allow = {WILDCARD: False, **{TOOL_PREFIX + n: True for n in contract}}
    res = check_allowlist(allow, contract)
    assert res["wildcard_ok"]
    assert not res["noncontract_enabled"]
    assert not res["contract_missing"]


def test_check_allowlist_should_flag_noncontract_enabled_when_contract_missing():
    contract = load_contract()
    allow = {WILDCARD: False, TOOL_PREFIX + "flaresolverr_bypass": True}
    res = check_allowlist(allow, contract)
    assert res["noncontract_enabled"] == ["devforge-mcp_flaresolverr_bypass"]
    assert set(res["contract_missing"]) == set(contract)


def test_check_allowlist_should_flag_wildcard_enabled():
    contract = load_contract()
    allow = {WILDCARD: True, **{TOOL_PREFIX + n: True for n in contract}}
    res = check_allowlist(allow, contract)
    assert not res["wildcard_ok"]


def test_scan_m0_log_should_count_only_noncontract_calls_in_window(tmp_path):
    contract = load_contract()
    log = tmp_path / "opencode.log"
    log.write_text(
        "timestamp=2026-09-10T00:00:00.000Z level=INFO message=evaluated"
        " permission=devforge-mcp_flaresolverr_bypass pattern=* action.action=allow\n"
        "timestamp=2026-09-27T01:02:03.004Z level=INFO message=evaluated"
        " permission=devforge-mcp_obs_write pattern=* action.action=allow\n"
        "timestamp=2026-09-27T01:02:03.005Z level=INFO message=evaluated"
        " permission=devforge-mcp_flaresolverr_bypass pattern=* action.action=allow\n"
        "timestamp=2026-09-27T01:03:03.005Z level=INFO message=evaluated"
        " permission=devforge-mcp_flaresolverr_bypass pattern=* action.action=deny\n"
        "timestamp=2026-09-27T01:04:03.005Z level=INFO message=evaluated"
        " permission=search_turns pattern=* action.action=allow\n"
        "noise line without eval marker\n"
    )
    scan = scan_m0_log(log, M0_SINCE, contract)
    assert scan["present"]
    assert scan["evaluations"] == 3, scan
    assert scan["noncontract"] == [
        {
            "tool": "devforge-mcp_flaresolverr_bypass",
            "first_ts": "2026-09-27T01:02:03.005Z",
            "last_ts": "2026-09-27T01:03:03.005Z",
            "count": 2,
        }
    ]


def test_scan_m0_log_should_report_absent_log(tmp_path):
    scan = scan_m0_log(tmp_path / "missing.log", M0_SINCE, load_contract())
    assert scan == {"present": False, "evaluations": 0, "noncontract": []}


def _write_config(tmp_path: Path, tools: dict) -> Path:
    config = tmp_path / "opencode.json"
    config.write_text(json.dumps({"tools": tools}))
    return config


def test_m0_check_should_fail_and_append_evidence_when_violated(tmp_path):
    config = _write_config(tmp_path, {WILDCARD: False, TOOL_PREFIX + "obs_write": True})
    log = tmp_path / "opencode.log"
    log.write_text(
        "timestamp=2026-09-27T01:02:03.004Z level=INFO message=evaluated"
        " permission=devforge-mcp_flaresolverr_bypass pattern=* action.action=allow\n"
    )
    evidence = tmp_path / "m0_recheck.jsonl"
    rc = m0_check(config_path=config, log_path=log, evidence_path=evidence, since=M0_SINCE)
    assert rc == 1
    record = json.loads(evidence.read_text().splitlines()[-1])
    assert record["check"] == "m0"
    assert record["status"] == "violated"
    assert record["log_noncontract"][0]["tool"] == "devforge-mcp_flaresolverr_bypass"


def test_m0_check_should_pass_with_contract_only_allowlist(tmp_path):
    contract = load_contract()
    config = _write_config(tmp_path, {WILDCARD: False, **{TOOL_PREFIX + n: True for n in contract}})
    log = tmp_path / "opencode.log"
    log.write_text(
        "timestamp=2026-09-27T01:02:03.004Z level=INFO message=evaluated"
        " permission=devforge-mcp_obs_write pattern=* action.action=allow\n"
    )
    evidence = tmp_path / "m0_recheck.jsonl"
    rc = m0_check(config_path=config, log_path=log, evidence_path=evidence, since=M0_SINCE)
    assert rc == 0
    record = json.loads(evidence.read_text().splitlines()[-1])
    assert record["status"] == "ok"
    assert record["window_evaluations"] == 1


def test_m0_check_should_fail_when_evidence_unwritable(tmp_path):
    contract = load_contract()
    config = _write_config(tmp_path, {WILDCARD: False, **{TOOL_PREFIX + n: True for n in contract}})
    log = tmp_path / "opencode.log"
    log.write_text("")
    evidence = tmp_path / "no_such_dir" / "m0_recheck.jsonl"
    evidence.parent.mkdir()
    evidence.parent.chmod(0o500)
    try:
        rc = m0_check(config_path=config, log_path=log, evidence_path=evidence, since=M0_SINCE)
    finally:
        evidence.parent.chmod(0o700)
    assert rc == 1


def test_live_allowlist_should_match_contract_when_host_config_present():
    if not OPENCODE_CONFIG.is_file():
        pytest.skip("no live opencode config on this host")
    contract = load_contract()
    res = check_allowlist(load_tool_allowlist(OPENCODE_CONFIG), contract)
    assert res["wildcard_ok"], res
    assert not res["noncontract_enabled"], res
    assert not res["contract_missing"], res
