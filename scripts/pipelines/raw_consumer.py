#!/usr/bin/env python3
# Status: experimental
# Path: devforge-worker — Pass 2: raw → pending (text_clean)
"""Consume raw turns from DB — apply text_clean, advance to pending.

Pass 2 (near-real-time): polls for pipeline_state='raw', newest first.
Pass 3 (backfill): processes remaining raw turns DESC (oldest last).

Flow:
  turn_watcher (jsonl -> DB, raw insert)
  -> raw_consumer (raw -> clean -> pending)
  -> day_cycle (pending -> batching -> cleaned -> scanned -> extracted -> verified -> enriched -> embedded)
"""

import json
import time

from lib.db import esc_sql, psql_json, psql_ok
from lib.text_cleaner import estimate_tokens, get_cleaner

BATCH_LIMIT = 50
POLL_INTERVAL = 30


def process_raw_turns(backfill: bool = False) -> int:
    """Claim raw turns with SKIP LOCKED, clean, advance to pending.

    Args:
        backfill: True = oldest-first (Pass 3), False = newest-first (Pass 2).

    Returns:
        Number of turns processed.
    """
    order = "DESC" if backfill else "ASC"

    rows = psql_json(f"""
        WITH batch AS (
            SELECT id
            FROM turns
            WHERE pipeline_state = 'raw'
            ORDER BY created_at {order}
            LIMIT {BATCH_LIMIT}
            FOR UPDATE SKIP LOCKED
        )
        SELECT t.id, t.user_turn, t.text, t.thinking
        FROM batch b
        JOIN turns t ON t.id = b.id
        ORDER BY t.created_at ASC
    """)
    if not rows:
        return 0

    cl = get_cleaner()
    ok_count = 0

    for row in rows:
        tid = row["id"]
        raw_ut = row.get("user_turn", "") or ""
        raw_tx = row.get("text", "") or ""
        raw_th = row.get("thinking", "") or ""

        lang = cl.detect_language(raw_ut)[0]

        user_clean = cl.clean(raw_ut[:8000], lang=lang)
        text_clean_val = cl.clean((raw_tx or "")[:8000], lang=lang)
        think_clean = cl.clean((raw_th or "")[:4000], lang=lang)
        full_len_input = " ".join(filter(None, [user_clean, text_clean_val, think_clean]))
        est = estimate_tokens(full_len_input)

        # FTS5 terms + tokens from concatenated clean text
        full_clean = " ".join(filter(None, [user_clean, text_clean_val, think_clean]))
        tokens_json: str = "NULL"
        if full_clean.strip():
            doc = cl.process_document(full_clean)
            tokens_json = f"'{esc_sql(json.dumps({'terms': doc['terms'], 'tokens': doc['tokens']}, ensure_ascii=False))}'::jsonb"

        def _sv(v: str) -> str:
            return "NULL" if not v.strip() else f"'{esc_sql(v)}'"

        psql_ok(f"""
            UPDATE turns SET
                user_turn_clean = {_sv(user_clean)},
                text_clean = {_sv(text_clean_val)},
                thinking_clean = {_sv(think_clean)},
                tokens = {tokens_json},
                est_chars = {est},
                detected_lang = '{esc_sql(lang)}',
                pipeline_state = 'pending'
            WHERE id = '{esc_sql(tid)}'::uuid
        """)
        ok_count += 1

    return ok_count


def main():
    import argparse

    ap = argparse.ArgumentParser(description="Consume raw turns — clean + advance to pending")
    ap.add_argument("--backfill", action="store_true", help="Pass 3: process oldest raw turns")
    ap.add_argument("--once", action="store_true", help="Run one batch and exit")
    ap.add_argument("--limit", type=int, default=50, help="Batch size (default: 50)")
    args = ap.parse_args()

    if args.backfill:
        n = process_raw_turns(backfill=True)
        print(f"raw_consumer backfill: {n} turns processed")
        return 0

    if args.once:
        n = process_raw_turns(backfill=False)
        print(f"raw_consumer: {n} turns processed")
        return 0

    # Pass 2 — continuous loop
    print("raw_consumer Pass 2 starting (interval=%ds)" % POLL_INTERVAL)
    while True:
        try:
            n = process_raw_turns(backfill=False)
            if n > 0:
                print(f"  raw_consumer: {n} turns cleaned")
        except Exception as e:
            print(f"  raw_consumer error: {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
