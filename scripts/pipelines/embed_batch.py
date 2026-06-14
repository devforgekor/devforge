#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.sh — Phase 1 (embed batch)
"""Embed Batch Pipeline — f16 embedding via Pod B llama-server.

Checkpoint-based: SELECT turns WHERE created_at > checkpoint AND embedding_f16 IS NULL.
Sends to Pod B /v1/embeddings (f16 mode) in batches, stores result in turns.embedding_f16.
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

from lib.db import psql, psql_ok, psql_json, esc_sql, get_checkpoint, advance_checkpoint
from lib.infra.preflight import preflight_checks
from lib.common import log

EMBED_URL = "http://127.0.0.1:8081/v1/embeddings"
BATCH_SIZE = 4        # 4 texts per request (--parallel 2 = 4096 ctx/slot, 4 texts ~1200-2400 tokens)
BATCH_LIMIT = 200     # max turns per run (200 / 4 = 50 API calls)
BATCH_TIMEOUT = 1800  # per batch request (30min safety — model cold load ~3.5min + processing)
MAX_CYCLE = 86400     # 24hr max for full 8.6K turn embed


def get_unembedded_turns(limit: int):
    """Return turns created after checkpoint without f16 embedding, ordered by created_at."""
    ckpt = get_checkpoint("embed_batch")
    rows = psql_json(
        f"SELECT id, user_turn_clean, text_clean, created_at::text "
        f"FROM turns "
        f"WHERE created_at > '{esc_sql(ckpt)}'::timestamptz "
        f"  AND embedding_f16 IS NULL "
        f"ORDER BY created_at ASC "
        f"LIMIT {limit}"
    )
    return rows or []


def embed_batch(texts: list[str], timeout: int = 600) -> Optional[list[Optional[list]]]:
    """Send multiple texts to /v1/embeddings, return list of vectors (None for failed)."""
    body = json.dumps({"input": texts, "model": "default"}).encode()
    try:
        req = urllib.request.Request(
            EMBED_URL, data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())

        # Build index -> vector map
        vecs: dict[int, list] = {}
        for item in data["data"]:
            idx = item["index"]
            vec = item["embedding"]
            if len(vec) != 4096:
                log(f"  [warn] index {idx}: unexpected dim {len(vec)} (expected 4096)")
            vecs[idx] = vec

        # Return in original order, None if response missing index
        return [vecs.get(i) for i in range(len(texts))]
    except Exception as e:
        log(f"  [error] batch embed failed ({len(texts)} texts): {e}")
        return None


def _prepare_text(turn: dict) -> Optional[str]:
    """Combine and truncate turn text for embedding. Returns None to skip."""
    embed_text = f"{turn['user_turn_clean']} {turn['text_clean']}".strip()
    if not embed_text:
        return None
    if len(embed_text) > 8192:
        embed_text = embed_text[:8192]
    return embed_text


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
    log("Embed Batch — f16 (Qwen3-Embedding-8B, batch={})".format(BATCH_SIZE))
    log("=" * 60)

    preflight_checks("embed_batch.py", required_ports={8081})

    t_start = time.monotonic()
    turns = get_unembedded_turns(limit)
    if not turns:
        ckpt_val = get_checkpoint("embed_batch")
        log(f"  [ok] No new turns to embed")
        log(f"  checkpoint={ckpt_val[:19] if ckpt_val else 'N/A'}")
        return

    # Pre-process: prepare text, identify skips
    prepared = []
    for turn in turns:
        text = _prepare_text(turn)
        if text is None:
            prepared.append(("skip", turn, None))
        else:
            prepared.append(("embed", turn, text))

    emb_turns = [(t, p) for (s, t, p) in prepared if s == "embed"]
    skip_count = sum(1 for s, _, _ in prepared if s == "skip")

    log(f"  Found {len(turns)} turns: {len(emb_turns)} to embed, {skip_count} empty/skip")

    n_ok = 0
    n_fail = 0

    # Log skips but do NOT advance checkpoint — only advance after success
    for turn, _ in [(t, p) for s, t, p in prepared if s == "skip"]:
        log(f"  SKIP (empty) {turn['created_at'][:19]}")
        n_ok += 1

    # Batch embedding
    for batch_start in range(0, len(emb_turns), BATCH_SIZE):
        if time.monotonic() - t_start > MAX_CYCLE:
            log(f"  [timeout] cycle limit ({MAX_CYCLE}s) at batch {batch_start}")
            break

        batch = emb_turns[batch_start:batch_start + BATCH_SIZE]
        batch_turns = [t for t, _ in batch]
        batch_texts = [p for _, p in batch]

        if dry_run:
            for turn in batch_turns:
                log(f"  DRY-RUN: {turn['created_at'][:19]}")
            n_ok += len(batch_turns)
            continue

        t0 = time.monotonic()
        vectors = embed_batch(batch_texts, timeout=BATCH_TIMEOUT)
        elapsed = time.monotonic() - t0

        if vectors is None:
            log(f"  batch {batch_start//BATCH_SIZE+1} — ALL FAILED ({elapsed:.1f}s)")
            n_fail += len(batch_turns)
            continue

        # Per-turn storage
        batch_ok = True
        for turn, vec in zip(batch_turns, vectors):
            if vec is not None:
                store_embedding(turn['id'], vec)
                n_ok += 1
            else:
                batch_ok = False
                n_fail += 1
                log(f"  [warn] null vector for {turn['created_at'][:19]}")

        # Only advance checkpoint if ALL turns in batch succeeded
        if batch_ok and not dry_run:
            advance_checkpoint("embed_batch", batch_turns[-1]['created_at'])

        log(f"  batch {batch_start//BATCH_SIZE+1}/{(len(emb_turns)+BATCH_SIZE-1)//BATCH_SIZE} "
            f"({batch_start+1}-{min(batch_start+BATCH_SIZE, len(emb_turns))}/{len(emb_turns)}) "
            f"{elapsed:.1f}s")

    elapsed = time.monotonic() - t_start
    log(f"Embed batch done: {n_ok} ok, {n_fail} failed, {elapsed:.0f}s")
    if n_fail > 0:
        log(f"  {n_fail} failed — checkpoint NOT advanced past last success")


if __name__ == "__main__":
    main()
