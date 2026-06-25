#!/usr/bin/env python3
# Status: production
# Path: day_cycle.sh — Text Preprocessing step
"""Text Preprocessing — text_clean 생성 (NULL인 턴만 처리).

day_cycle.sh 첫 번째 스테이지. 모든 턴에 대해 텍스트 전처리를 1회만 수행.
turn_watcher.py의 중복 처리 대신 day_cycle.sh에서 통합 관리.

처리 내용 (lib/text_cleaner.TextCleaner.clean):
  - NFKC 유니코드 정규화
  - 공백 정리 (연속 공백 → 단일 공백)
  - 이모지/반복문자 제거
  - 코드블록/인라인코드 보존

Usage:
  python3 scripts/pipelines/text_clean.py
"""
import sys, os, time
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql_json, psql_ok, esc_sql
from lib.text_cleaner import get_cleaner

BATCH_LIMIT = 50


def main():
    # Advance turns already clean from batching → cleaned
    psql_ok(
        "UPDATE turns SET pipeline_state = 'cleaned' "
        "WHERE pipeline_state = 'batching' AND text_clean IS NOT NULL AND text_clean != ''"
    )

    turns = psql_json(
        "SELECT id, user_turn, text, thinking FROM turns "
        "WHERE (text_clean IS NULL OR text_clean = '') AND pipeline_state = 'batching' "
        f"ORDER BY created_at ASC LIMIT {BATCH_LIMIT}"
    )
    if not turns:
        print("  [text_clean] 0 turns need preprocessing")
        return True

    cl = get_cleaner()
    ok = 0
    t0 = time.monotonic()
    n = len(turns)

    for i, t in enumerate(turns, 1):
        tid = t["id"]
        try:
            user_clean = cl.clean((t.get("user_turn") or "")[:2000])
            text_clean = cl.clean((t.get("text") or "")[:8000])
            think_clean = cl.clean((t.get("thinking") or "")[:4000])
            sql = (
                f"UPDATE turns SET "
                f"user_turn_clean = '{esc_sql(user_clean)}', "
                f"text_clean = '{esc_sql(text_clean)}', "
                f"thinking_clean = '{esc_sql(think_clean)}' "
                f"WHERE id = '{tid}'"
            )
            if psql_ok(sql):
                ok += 1
                psql_ok(f"UPDATE turns SET pipeline_state = 'cleaned' WHERE id = '{tid}'")
        except Exception as e:
            print(f"  [text_clean] ERROR {tid[:8]}: {e}", flush=True)

        if i % 200 == 0:
            elapsed = time.monotonic() - t0
            print(f"  [text_clean] ... {ok}/{n} done ({elapsed:.0f}s)", flush=True)

    elapsed = time.monotonic() - t0
    print(f"  [text_clean] {ok}/{n} turns cleaned in {elapsed:.0f}s", flush=True)
    return ok == n


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
