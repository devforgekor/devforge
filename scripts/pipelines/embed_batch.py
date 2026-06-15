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
import signal
import sys
import threading
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
from lib.watchdog.messenger import heartbeat

EMBED_URL = "http://127.0.0.1:8081/v1/embeddings"
MAX_BATCH_SIZE = 6    # max texts per request (safety cap)
BATCH_LIMIT = 200     # max turns per run
BATCH_TIMEOUT = 1800  # per batch request (30min safety — model cold load ~3.5min + processing)
MAX_CYCLE = 86400     # 24hr max for full 8.6K turn embed
SLOT_CTX = 5000       # token budget per slot (--ctx-size 12288 / --parallel 2 * 0.8 margin)
PID_FILE = "/tmp/embed_batch.pid"

# ── PID lock — prevent concurrent runs ──────────────────────────────────


def _check_pid_lock() -> bool:
    """Return True if another embed_batch is running (exit caller should skip)."""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            if old_pid == os.getpid():
                return False  # ourselves, unlikely
            # Check if process is alive
            os.kill(old_pid, 0)
            log(f"  [lock] embed_batch PID {old_pid} already running — skipping")
            return True
        except (ValueError, OSError, ProcessLookupError):
            pass  # stale lock, overwrite
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    return False


def _remove_pid_lock():
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE) as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(PID_FILE)
    except Exception:
        pass


# ── Background liveness heartbeat ────────────────────────────────────


def _liveness_heartbeat():
    """Daemon thread: fires liveness heartbeat every 60s.
    Watchdog checks this to distinguish "still alive but slow" from "dead".
    """
    while True:
        try:
            heartbeat("liveness_embed_batch")
        except Exception:
            pass
        time.sleep(60)


def _post_embed(texts: list[str], timeout: int) -> Optional[dict]:
    """POST to /v1/embeddings with a fresh TCP connection (no reuse — avoids server-side hang)."""
    import json as _json
    body = _json.dumps({"input": texts, "model": "default"}).encode()
    req = urllib.request.Request(
        EMBED_URL, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _json.loads(resp.read().decode())
    except Exception as e:
        log(f"  [error] batch embed failed ({len(texts)} texts): {e}")
        return None


def embed_batch(texts: list[str], timeout: int = 600) -> Optional[list[Optional[list]]]:
    """Send multiple texts to /v1/embeddings, return list of vectors (None for failed)."""
    data = _post_embed(texts, timeout)
    if data is None:
        return None
    try:
        vecs: dict[int, list] = {}
        for item in data["data"]:
            vecs[item["index"]] = item["embedding"]
        return [vecs.get(i) for i in range(len(texts))]
    except (KeyError, TypeError) as e:
        log(f"  [error] parse failed: {e}")
        return None


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
    import atexit

    dry_run = "--dry-run" in sys.argv
    limit = BATCH_LIMIT
    for i, a in enumerate(sys.argv):
        if a == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    # PID lock — skip if another instance is running
    if _check_pid_lock():
        return
    atexit.register(_remove_pid_lock)

    # Background liveness heartbeat (daemon thread, stops when main exits)
    threading.Thread(target=_liveness_heartbeat, daemon=True).start()

    log("=" * 60)
    log("Embed Batch — f16 (Qwen3-Embedding-8B, max_batch={})".format(MAX_BATCH_SIZE))
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

    # Build batches dynamically by estimated token budget
    batches = []
    cur_batch: list = []
    cur_tok = 0
    for item in emb_turns:
        text = item[1]
        est = max(len(text) * 2 // 3, 1)  # ~1.5 chars/token for Korean, safer estimate
        if est > SLOT_CTX:
            # Super-long text: flush cur, send solo
            if cur_batch:
                batches.append(cur_batch)
                cur_batch, cur_tok = [], 0
            batches.append([item])
            log(f"  [solo] text with ~{est} est tok — sending individually")
        elif cur_tok + est > SLOT_CTX:
            batches.append(cur_batch)
            cur_batch, cur_tok = [item], est
        elif len(cur_batch) >= MAX_BATCH_SIZE:
            batches.append(cur_batch)
            cur_batch, cur_tok = [item], est
        else:
            cur_batch.append(item)
            cur_tok += est
    if cur_batch:
        batches.append(cur_batch)

    log(f"  Built {len(batches)} batches (max {MAX_BATCH_SIZE} texts / ~{SLOT_CTX} tok per batch)")

    # Batch embedding
    for bi, batch in enumerate(batches, 1):
        if time.monotonic() - t_start > MAX_CYCLE:
            log(f"  [timeout] cycle limit ({MAX_CYCLE}s) at batch {bi}")
            break

        heartbeat("embed_batch")

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
            log(f"  batch {bi}/{len(batches)} — ALL FAILED ({elapsed:.1f}s)")
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

        log(f"  batch {bi}/{len(batches)} ({len(batch)} texts, ~{sum(len(p) for _, p in batch)//2} est tok) {elapsed:.1f}s")

    elapsed = time.monotonic() - t_start
    log(f"Embed batch done: {n_ok} ok, {n_fail} failed, {elapsed:.0f}s")
    if n_fail > 0:
        log(f"  {n_fail} failed — checkpoint NOT advanced past last success")


if __name__ == "__main__":
    main()
