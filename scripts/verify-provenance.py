#!/usr/bin/env python3
# Status: experimental
# Path: none — verification script
"""provenance 검증 — turns.source가 정상 기록되는지 확인"""

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


def main():
    print(f"=== Provenance Verification — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} ===\n")

    # 1. 전체 turns source 분포
    print("--- 1. turns.source 분포 ---")
    dist = pg_query("SELECT source, count(*) FROM turns GROUP BY source ORDER BY count(*) DESC;")
    print(dist)

    # 2. unknown 비율
    total_row = pg_query("SELECT count(*) FROM turns;")
    unknown_row = pg_query("SELECT count(*) FROM turns WHERE source='unknown';")
    print(f"\n--- 2. unknown 비율 ---")
    try:
        total = int(total_row)
        unknown = int(unknown_row)
        pct = (unknown / total * 100) if total > 0 else 0
        print(f"Total: {total}, Unknown: {unknown}, Ratio: {pct:.2f}%")
        if pct == 0:
            print("✅ PASS: No turns with source='unknown'")
        else:
            print(f"⚠️  WARN: {unknown} turns still have source='unknown'")
    except ValueError:
        print("ERROR: Could not parse counts")

    # 3. legacy 마커 존재 확인
    print(f"\n--- 3. legacy 마커 확인 ---")
    legacy = pg_query("SELECT count(*) FROM turns WHERE source='legacy:pre-2026-09';")
    print(f"legacy:pre-2026-09 turns: {legacy}")

    # 4. 백업 테이블 존재 확인
    print(f"\n--- 4. 백업 테이블 확인 ---")
    backup = pg_query("SELECT count(*) FROM turns_source_backup_20260914;")
    print(f"turns_source_backup_20260914 rows: {backup}")

    # 5. 신규 turns 확인 (최근 1시간)
    print(f"\n--- 5. 최근 신규 turns (최근 1시간) ---")
    recent = pg_query("""
        SELECT source, count(*) FROM turns 
        WHERE created_at >= NOW() - INTERVAL '1 hour'
        GROUP BY source;
    """)
    if recent:
        print(recent)
    else:
        print("(No turns in the last hour)")

    # 6. 코드 경로 확인
    print(f"\n--- 6. INSERT 문 code 확인 (호스트) ---")
    import os
    files = {
        "scripts/turn_watcher.py": "source",
        "scripts/mcp_server.py": "source",
        "scripts/mcp_server_sse.py": "source",
    }
    for filepath, expected in files.items():
        fullpath = os.path.join("/opt/projects/server", filepath)
        if os.path.exists(fullpath):
            content = open(fullpath).read()
            has_source_col = "source" in content and "INSERT INTO turns" in content
            print(f"{filepath}: {'✅ source 컬럼 포함' if has_source_col else '❌ source 컬럼 누락'}")
        else:
            print(f"{filepath}: ❌ 파일 없음")

    print(f"\n=== Verification Complete ===")


if __name__ == "__main__":
    main()
