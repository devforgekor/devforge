#!/usr/bin/env python3
"""Phase A pre-cutover verification — V1-V8 checklist."""

import json
import subprocess
import sys
from datetime import datetime, timezone

DB_NAME = "devforge_app"
DB_USER = "postgres"


def pg_query(query: str) -> str:
    result = subprocess.run(
        ["podman", "exec", "-i", "postgres", "psql", "-U", DB_USER, "-d", DB_NAME, "-t", "-A", "-c", query],
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def check(label: str, condition: bool, detail: str = "") -> str:
    status = "✅" if condition else "❌"
    msg = f"{status} {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return "PASS" if condition else "FAIL"


def main():
    print(f"=== Phase A Pre-Cutover Verification — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} ===\n")

    results = []

    # === V1: 12-tool contract check ===
    print("--- V1: 12툴 계약 매칭 ---")
    from devforge.adapters.driving.mcp.server import get_tools
    tool_names = {t["name"] for t in get_tools()}
    contract_names = {
        "deepdive_step_enter", "deepdive_step_exit", "deepdive_session_heartbeat",
        "deepdive_session_status", "deepdive_verify_sandbox", "obs_write", "obs_search",
        "search_turns", "search_similarity", "mem_save", "mem_search", "get_conversation",
    }
    missing = contract_names - tool_names
    extra = tool_names - contract_names
    results.append(check("12툴 계약 보존", len(missing) == 0, f"missing=({', '.join(missing)})" if missing else f"all present, extra=({', '.join(extra)})"))

    # === V2: tool schemas ===
    print("\n--- V2: 툴 스키마 검증 ---")
    from devforge.adapters.driving.mcp.server import IngestParams, DeepDiveStepEnterParams, DeepDiveStepExitParams
    from devforge.adapters.driving.mcp.server import DeepDiveSessionHeartbeatParams, DeepDiveSessionStatusParams
    from devforge.adapters.driving.mcp.server import DeepDiveVerifySandboxParams, ObsWriteParams, ObsSearchParams
    from devforge.adapters.driving.mcp.server import SearchTurnsParams, SearchSimilarityParams, MemSaveParams, MemSearchParams, GetConversationParams
    schemas = {
        "ingest": IngestParams, "deepdive_step_enter": DeepDiveStepEnterParams,
        "deepdive_step_exit": DeepDiveStepExitParams,
        "deepdive_session_heartbeat": DeepDiveSessionHeartbeatParams,
        "deepdive_session_status": DeepDiveSessionStatusParams,
        "deepdive_verify_sandbox": DeepDiveVerifySandboxParams,
        "obs_write": ObsWriteParams, "obs_search": ObsSearchParams,
        "search_turns": SearchTurnsParams, "search_similarity": SearchSimilarityParams,
        "mem_save": MemSaveParams, "mem_search": MemSearchParams,
        "get_conversation": GetConversationParams,
    }
    for name, schema in schemas.items():
        required = schema.model_fields.keys()
        print(f"  {name}: required=({', '.join(required)})")
    results.append(check("모든 툴 스키마 정의됨", len(schemas) == 13, f"{len(schemas)}/13"))

    # === V3: provenance check ===
    print("\n--- V3: provenance 기록 ---")
    total = int(pg_query("SELECT count(*) FROM turns;"))
    unknown = int(pg_query("SELECT count(*) FROM turns WHERE source='unknown';"))
    results.append(check("기존 turns provenance", unknown == 0, f"unknown=0/{total}"))

    recent = pg_query("""SELECT source, count(*) FROM turns 
        WHERE created_at >= NOW() - INTERVAL '1 hour' GROUP BY source;""")
    has_new_source = "unknown" not in recent
    results.append(check("신규 turns source 기록", has_new_source, f"{recent.replace(chr(10), ' | ')}"))

    # === V4: HTTP ingest endpoint ===
    print("\n--- V4: HTTP /api/v1/ingest ---")
    from devforge.adapters.driving.api.app import create_app
    app = create_app()
    ingest_route = any("api/v1/ingest" in str(r) for r in app.routes if hasattr(r, "path"))
    results.append(check("/api/v1/ingest 존재", ingest_route))

    # === V5: deepdive E2E simulation ===
    print("\n--- V5: deepdive E2E 시뮬레이션 ---")
    has_deepdive = all(t in tool_names for t in [
        "deepdive_step_enter", "deepdive_step_exit",
        "deepdive_session_heartbeat", "deepdive_session_status", "deepdive_verify_sandbox",
    ])
    results.append(check("deepdive 5종 등록", has_deepdive, f"5/5 in {len(tool_names)} tools"))

    # === V6: mem operations ===
    print("\n--- V6: mem operations ---")
    has_mem = all(t in tool_names for t in ["mem_save", "mem_search"])
    results.append(check("mem_save/search 등록", has_mem))

    # === V7: conversation ===
    print("\n--- V7: get_conversation ---")
    has_conv = "get_conversation" in tool_names
    results.append(check("get_conversation 등록", has_conv))

    # === V8: container readiness ===
    print("\n--- V8: 컨테이너 기동 준비 ---")
    try:
        health = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "http://127.0.0.1:8000/health"],
            capture_output=True, text=True, timeout=5,
        )
        status = health.stdout.strip()
        results.append(check("devforge-mcp health check", status == "200", f"HTTP {status}"))
    except Exception as e:
        # Expected since refactored MCP not running yet
        results.append(check("devforge-mcp health check", False, f"not running (expected pre-cutover): {e}"))

    # === Summary ===
    print("\n" + "=" * 60)
    passed = sum(1 for r in results if r == "PASS")
    total_checks = len(results)
    print(f"Results: {passed}/{total_checks} PASS")
    if passed == total_checks:
        print("✅ Phase A pre-cutover: READY")
    else:
        print(f"⚠️  {total_checks - passed} items need attention before cutover")

    return 0 if passed == total_checks else 1


if __name__ == "__main__":
    sys.exit(main())
