#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.sh — Phase 1 (embed batch)
"""Embed Batch Pipeline — f16 embedding via Pod B llama-server.

Checkpoint-based: SELECT turns WHERE created_at > checkpoint AND embedding_f16 IS NULL.
Sends to Pod B /v1/embeddings (f16 mode), stores result in turns.embedding_f16.
Failed turns do NOT advance checkpoint — retried next cycle.

Usage:
  python3 scripts/pipelines/embed_batch.py              # batch from checkpoint
  python3 scripts/pipelines/embed_batch.py --limit 20   # batch cap
  python3 scripts/pipelines/embed_batch.py --dry-run    # simulate, no writes
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql, psql_ok, psql_json, esc_sql
from lib.infra.preflight import preflight_checks
from lib.common import log

EMBED_URL = "http://127.0.0.1:8081/v1/embeddings"
EMBED_TIMEOUT = 300   # per request
BATCH_LIMIT = 50
MAX_CYCLE = 600       # 10min max for embed phase




def get_unembedded_turns(limit: int):
    """Return turns created after checkpoint without f16 embedding, ordered by created_at."""
    ckpt = get_checkpoint("embed_batch")
    rows = psql_json(
        f"SELECT id, user_turn, text, created_at::text "
        f"FROM turns "
        f"WHERE created_at > '{esc_sql(ckpt)}'::timestamptz "
        f"  AND embedding_f16 IS NULL "
        f"ORDER BY created_at ASC "
        f"LIMIT {limit}"
    )
    return rows or []


def embed_turn(text: str) -> Optional[list]:
    """Send text to Pod B /v1/embeddings, return vector or None."""
    body = json.dumps({"input": text, "model": "default"}).encode()
    try:
        req = urllib.request.Request(
            EMBED_URL, data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
            data = json.loads(resp.read())
        vec = data["data"][0]["embedding"]
        if len(vec) != 4096:
            log(f"  [warn] unexpected dim {len(vec)} (expected 4096)")
        return vec
    except Exception as e:
        log(f"  [error] embed failed: {e}")
        return None


def store_embedding(turn_id: str, vector: list):
    """UPDATE turns SET embedding_f16 = vector WHERE id = turn_id."""
    vec_str = "[" + ",".join(f"{v:.8f}" for v in vector) + "]"
    sql = f"UPDATE turns SET embedding_f16 = '{esc_sql(vec_str)}'::vector WHERE id = '{esc_sql(turn_id)}'::uuid"
    psql_ok(sql)


def main():
    dry_run = "--dry-run" in sys.argv
    limit = BATCH_LIMIT
    for i, a in enumerate(sys.argv):
        if a == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    log("=" * 60)
    log("Embed Batch — f16 (Qwen3-Embedding-8B)")
    log("=" * 60)

    preflight_checks("embed_batch.py", required_ports={8081})

    t_start = time.monotonic()
    turns = get_unembedded_turns(limit)
    if not turns:
        log("  [ok] No new turns to embed")
        log(f"  checkpoint={get_checkpoint("embed_batch")[:19]}")
        return

    log(f"  Found {len(turns)} unembedded turns (limit={limit})")
    n_ok = 0
    n_fail = 0
    last_created = None

    for i, turn in enumerate(turns, 1):
        if time.monotonic() - t_start > MAX_CYCLE:
            log(f"  [timeout] cycle limit ({MAX_CYCLE}s) — {i-1} done, checkpoint not advanced past {i-1}")
            break

        # Combine user_turn + text for embedding
        embed_text = f"{turn['user_turn']} {turn['text']}"
        if dry_run:
            log(f"  [{i}/{len(turns)}] DRY-RUN: {turn['created_at'][:19]} ({len(embed_text)} chars)")
            n_ok += 1
            last_created = turn['created_at']
            continue

        t0 = time.monotonic()
        vec = embed_turn(embed_text)
        elapsed = time.monotonic() - t0

        if vec is not None:
            store_embedding(turn['id'], vec)
            n_ok += 1
            last_created = turn['created_at']
            log(f"  [{i}/{len(turns)}] {elapsed:.1f}s ✓ {turn['created_at'][:19]}")
        else:
            n_fail += 1
            log(f"  [{i}/{len(turns)}] {elapsed:.1f}s ✗ {turn['created_at'][:19]} — RETRY NEXT CYCLE")

        # Checkpoint advance after each successful turn
        if last_created and not dry_run:
            advance_checkpoint("embed_batch", last_created)

    elapsed = time.monotonic() - t_start
    log(f"Embed batch done: {n_ok} ok, {n_fail} failed, {elapsed:.0f}s")
    if n_fail > 0:
        log(f"  {n_fail} failed — checkpoint NOT advanced past last success")


if __name__ == "__main__":
    main()
