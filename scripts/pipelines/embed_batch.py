#!/usr/bin/env python3
# Status: production
# Path: day_cycle.sh
"""Embed Batch Pipeline — text_clean_polished 기준 embedding 단 1회.

State-based: LEFT JOIN embeddings WHERE NULL → embed → INSERT INTO embeddings.
Failed turns do NOT advance — retried next cycle via retry_count.

Usage:
  python3 scripts/pipelines/embed_batch.py              # batch from checkpoint
  python3 scripts/pipelines/embed_batch.py --limit 20   # batch cap
  python3 scripts/pipelines/embed_batch.py --dry-run    # simulate, no writes
  python3 scripts/pipelines/embed_batch.py --facts      # embed review_facts instead of turns
  python3 scripts/pipelines/embed_batch.py --feedback   # embed feedback_examples instead of turns
"""

import atexit
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

from lib.db import psql, psql_ok, psql_json, esc_sql
from lib.infra.preflight import preflight_checks
from lib.common import log
from lib.watchdog.messenger import heartbeat, resolve_pulse
from lib.pod_manager import ensure_model

from lib.llm_client import MODEL_REGISTRY
EMBED_URL = f"http://127.0.0.1:{MODEL_REGISTRY['embeder']['port']}/v1/embeddings"
MAX_BATCH_SIZE = 10   # max texts per request (safety cap)
BATCH_LIMIT = 50     # max turns per run (sliced via MAX_BATCH_SIZE)
BATCH_TIMEOUT = 1800  # per batch request (30min safety — model cold load ~3.5min + processing)
MAX_CYCLE = 86400     # 24hr max for full 8.6K turn embed
SLOT_CTX = 5000       # token budget per slot (--ctx-size 12288 / --parallel 2 * 0.8 margin)
CHUNK_MAX_TOKENS = 512
SHORT_TURN_CHARS = 200
EMBED_DIMS = 2048

import re

def truncate_to_mrl(vector: list[float], target_dims: int = EMBED_DIMS) -> list[float]:
    truncated = vector[:target_dims]
    norm = sum(x * x for x in truncated) ** 0.5
    if norm > 0:
        truncated = [x / norm for x in truncated]
    return truncated

def split_sentences(text: str) -> list[str]:
    text = text.replace('\r\n', '\n')
    sents = re.split(r'(?<=[.!?])\s+', text)
    result = []
    for s in sents:
        paragraphs = re.split(r'\n\n+', s.strip())
        result.extend(p for p in paragraphs if p.strip())
    return result

def estimate_tokens(text: str) -> int:
    return max(1, len(text) * 2 // 5)

def chunk_text(text: str) -> list[tuple[str, int]]:
    if len(text) < SHORT_TURN_CHARS:
        return [(text, 0)]
    sentences = split_sentences(text)
    chunks: list[tuple[str, int]] = []
    cur: list[str] = []
    cur_tok = 0
    for sent in sentences:
        sent_tok = estimate_tokens(sent)
        if sent_tok > CHUNK_MAX_TOKENS:
            if cur:
                chunks.append((' '.join(cur), len(chunks)))
                cur, cur_tok = [], 0
            max_chars = CHUNK_MAX_TOKENS * 5 // 2
            chunks.append((sent[:max_chars].rstrip(), len(chunks)))
        elif cur_tok + sent_tok > CHUNK_MAX_TOKENS:
            if cur:
                chunks.append((' '.join(cur), len(chunks)))
            cur, cur_tok = [sent], sent_tok
        else:
            cur.append(sent)
            cur_tok += sent_tok
    if cur:
        chunks.append((' '.join(cur), len(chunks)))
    if not chunks:
        chunks = [(text[:CHUNK_MAX_TOKENS * 5 // 2], 0)]
    return chunks


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
            vecs[item["index"]] = truncate_to_mrl(item["embedding"])
        return [vecs.get(i) for i in range(len(texts))]
    except (KeyError, TypeError) as e:
        log(f"  [error] parse failed: {e}")
        return None


def get_unembedded_turns(limit: int):
    """Return turns without a complete set of embedding chunks."""
    rows = psql_json(
        f"SELECT t.id, t.user_turn_clean_polished, t.text_clean_polished, "
        f"  t.user_turn, t.text, t.created_at::text, t.est_chars "
        f"FROM turns t "
        f"WHERE NOT EXISTS ("
        f"  SELECT 1 FROM embeddings e "
        f"  WHERE e.source_type = 'turn' AND e.source_id = t.id "
        f"    AND e.model_name = 'qwen3-embedding-8b-v1'"
        f") "
        f"  AND t.pipeline_state = 'polished' "
        f"  AND t.text_clean_polished IS NOT NULL "
        f"  AND (t.retry_count IS NULL OR t.retry_count < 3) "
        f"ORDER BY t.est_chars ASC NULLS LAST, t.created_at DESC "
        f"LIMIT {limit}"
    )
    return rows or []


def get_unembedded_facts(limit: int):
    """Return review_facts rows without embedding record."""
    rows = psql_json(
        f"SELECT rf.id, rf.turn_id, rf.fact_type, rf.evidence, rf.created_at::text "
        f"FROM review_facts rf "
        f"LEFT JOIN embeddings e ON e.source_type = 'review_fact' AND e.source_id = rf.id "
        f"  AND e.model_name = 'qwen3-embedding-8b-v1' "
        f"WHERE e.id IS NULL "
        f"  AND rf.evidence IS NOT NULL "
        f"ORDER BY rf.created_at DESC "
        f"LIMIT {limit}"
    )
    return rows or []


def store_embedding(turn_id: str, vector: list, embed_text: str, chunk_index: int = 0):
    """INSERT INTO embeddings for a turn. UPSERT on conflict."""
    vec_str = "[" + ",".join(f"{v:.8f}" for v in vector) + "]"
    safe_text = embed_text[:8000].replace("'", "''")
    psql_ok(
        f"INSERT INTO embeddings (source_type, source_id, embed_text, embedding, model_name, chunk_index) "
        f"VALUES ('turn', '{esc_sql(turn_id)}'::uuid, '{safe_text}', "
        f"  '{esc_sql(vec_str)}'::vector, 'qwen3-embedding-8b-v1', {chunk_index}) "
        f"ON CONFLICT (source_type, source_id, model_name, chunk_index) DO UPDATE SET "
        f"  embedding = EXCLUDED.embedding, embed_text = EXCLUDED.embed_text, "
        f"  created_at = now()"
    )


def store_fact_embedding(fact_id: str, vector: list, embed_text: str):
    """INSERT INTO embeddings for a review_fact."""
    vec_str = "[" + ",".join(f"{v:.8f}" for v in vector) + "]"
    safe_text = embed_text[:8000].replace("'", "''")
    psql_ok(
        f"INSERT INTO embeddings (source_type, source_id, embed_text, embedding, model_name) "
        f"VALUES ('review_fact', '{esc_sql(fact_id)}'::uuid, '{safe_text}', "
        f"  '{esc_sql(vec_str)}'::vector, 'qwen3-embedding-8b-v1') "
        f"ON CONFLICT (source_type, source_id, model_name) DO UPDATE SET "
        f"  embedding = EXCLUDED.embedding, embed_text = EXCLUDED.embed_text, "
        f"  created_at = now()"
    )


def get_unembedded_feedback(limit: int):
    """Return feedback_examples rows without embedding record."""
    rows = psql_json(
        f"SELECT fe.id, fe.evidence_text, fe.source_text, fe.verdict, fe.created_at::text "
        f"FROM feedback_examples fe "
        f"LEFT JOIN embeddings e ON e.source_type = 'feedback_example' AND e.source_id = fe.id "
        f"  AND e.model_name = 'qwen3-embedding-8b-v1' "
        f"WHERE e.id IS NULL "
        f"  AND fe.evidence_text IS NOT NULL "
        f"ORDER BY fe.created_at DESC "
        f"LIMIT {limit}"
    )
    return rows or []


def store_feedback_embedding(feedback_id: str, vector: list, embed_text: str):
    """INSERT INTO embeddings for a feedback_example."""
    vec_str = "[" + ",".join(f"{v:.8f}" for v in vector) + "]"
    safe_text = embed_text[:8000].replace("'", "''")
    psql_ok(
        f"INSERT INTO embeddings (source_type, source_id, embed_text, embedding, model_name) "
        f"VALUES ('feedback_example', '{esc_sql(feedback_id)}'::uuid, '{safe_text}', "
        f"  '{esc_sql(vec_str)}'::vector, 'qwen3-embedding-8b-v1') "
        f"ON CONFLICT (source_type, source_id, model_name) DO UPDATE SET "
        f"  embedding = EXCLUDED.embedding, embed_text = EXCLUDED.embed_text, "
        f"  created_at = now()"
    )


def main():
    dry_run = "--dry-run" in sys.argv
    facts_mode = "--facts" in sys.argv
    feedback_mode = "--feedback" in sys.argv
    limit = BATCH_LIMIT
    for i, a in enumerate(sys.argv):
        if a == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    # Register heartbeat pulse + SIGTERM cleanup
    heartbeat("embed_batch", detail="embedding batch")
    _cleanup_embed = lambda: resolve_pulse("heartbeat_embed_batch")
    signal.signal(signal.SIGTERM, lambda s, f: (_cleanup_embed(), os._exit(1)))
    atexit.register(_cleanup_embed)

    threading.Thread(target=_liveness_heartbeat, daemon=True).start()

    if feedback_mode:
        mode = "Feedback"
    elif facts_mode:
        mode = "Facts"
    else:
        mode = "Turns"
    log("=" * 60)
    log(f"Embed Batch — {mode} (max_batch={MAX_BATCH_SIZE})")
    log("=" * 60)

    if not feedback_mode and not facts_mode:
        orphaned = psql_json(
            "SELECT count(*) as cnt FROM embeddings e "
            "WHERE e.source_type = 'turn' AND e.model_name = 'qwen3-embedding-8b-v1' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM turns t "
            "  WHERE t.id = e.source_id AND t.pipeline_state = 'embedded'"
            ")"
        )
        if orphaned and orphaned[0].get("cnt", 0) > 0:
            log(f"  [cleanup] removing {orphaned[0]['cnt']} orphaned embedding chunks")
            psql_ok(
                "DELETE FROM embeddings e "
                "WHERE e.source_type = 'turn' AND e.model_name = 'qwen3-embedding-8b-v1' "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM turns t "
                "  WHERE t.id = e.source_id AND t.pipeline_state = 'embedded'"
                ")"
            )

    preflight_checks("embed_batch.py", required_ports={8081})
    # Ensure embedding model is running on 8081 (model identity check)
    if not ensure_model('embeder', skip_if_healthy=True):
        log("  FATAL: cannot start embedding model on 8081")
        return

    t_start = time.monotonic()

    if feedback_mode:
        rows = get_unembedded_feedback(limit)
    elif facts_mode:
        rows = get_unembedded_facts(limit)
    else:
        rows = get_unembedded_turns(limit)

    if not rows:
        log(f"  [ok] No unembedded {mode.lower()}")
        return

    # Pre-process: prepare text with sentence-level chunking
    prepared = []  # (action, row, text, chunk_index)
    for row in rows:
        if feedback_mode:
            text = row.get("evidence_text", "") or ""
        elif facts_mode:
            text = row.get("evidence", "") or ""
        else:
            user = (row.get("user_turn_clean_polished") or row.get("user_turn_clean") or "")
            resp = (row.get("text_clean_polished") or row.get("text_clean") or "")
            text = f"{user} {resp}".strip()
        if not text:
            prepared.append(("skip", row, None, None))
        else:
            if feedback_mode or facts_mode:
                prepared.append(("embed", row, text[:8192], 0))
            else:
                chunks = chunk_text(text)
                for ct, ci in chunks:
                    prepared.append(("embed", row, ct[:8192], ci))

    emb_rows = [(t, p, c) for (s, t, p, c) in prepared if s == "embed"]
    skip_count = sum(1 for s, _, _, _ in prepared if s == "skip")

    log(f"  Found {len(rows)} {mode.lower()}: {len(emb_rows)} to embed, {skip_count} empty/skip")

    ok_count = 0
    fail_count = 0

    for row, _ in [(t, p) for s, t, p, c in prepared if s == "skip"]:
        log(f"  SKIP (empty) {row.get('created_at','')[:19]}")

    # Build batches dynamically by estimated token budget
    batches = []
    cur_batch: list = []
    cur_tok = 0
    for item in emb_rows:
        text = item[1]
        row = item[0]
        est = max(1, len(text) * 2 // 3)
        if est > SLOT_CTX:
            if cur_batch:
                batches.append(cur_batch)
                cur_batch, cur_tok = [], 0
            batches.append([item])
            log(f"  [solo] text with ~{est} est tok")
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

    turn_chunk_progress: dict[str, dict] = {}

    for bi, batch in enumerate(batches, 1):
        heartbeat("embed_batch")

        batch_rows = [t for t, _, _ in batch]
        batch_texts = [p for _, p, _ in batch]

        if dry_run:
            for row in batch_rows:
                log(f"  DRY-RUN: {row.get('created_at','')[:19]}")
            ok_count += len(batch_rows)
            continue

        t0 = time.monotonic()
        vectors = embed_batch(batch_texts, timeout=BATCH_TIMEOUT)
        elapsed = time.monotonic() - t0

        if vectors is None:
            log(f"  batch {bi}/{len(batches)} — ALL FAILED ({elapsed:.1f}s)")
            fail_count += len(batch_rows)
            continue

        for (row, text, ci), vec in zip(batch, vectors):
            if vec is not None:
                if feedback_mode:
                    store_feedback_embedding(row['id'], vec, text)
                elif facts_mode:
                    store_fact_embedding(row['id'], vec, text)
                else:
                    store_embedding(row['id'], vec, text, ci)
                    tid = row['id']
                    if tid not in turn_chunk_progress:
                        total = sum(1 for _, r, _, _ in prepared if r['id'] == tid)
                        turn_chunk_progress[tid] = {'total': total, 'ok': 0}
                    turn_chunk_progress[tid]['ok'] += 1
                    if turn_chunk_progress[tid]['ok'] >= turn_chunk_progress[tid]['total']:
                        psql_ok(f"UPDATE turns SET pipeline_state = 'embedded' "
                                f"WHERE id = '{esc_sql(tid)}'::uuid")
                ok_count += 1
            else:
                fail_count += 1
                log(f"  [warn] null vector for {row.get('created_at','')[:19]} chunk {ci}")
                if not facts_mode and not feedback_mode:
                    r = psql_json(f"""
                        UPDATE turns SET retry_count = COALESCE(retry_count, 0) + 1
                        WHERE id = '{esc_sql(row['id'])}'::uuid
                        RETURNING retry_count
                    """)
                    if r and r[0].get('retry_count', 0) >= 3:
                        store_embedding(row['id'], [0.0] * 4096, text or "(sentinel)", 0)
                        psql_ok(f"UPDATE turns SET pipeline_state = 'embedded' "
                                f"WHERE id = '{esc_sql(row['id'])}'::uuid")
                        log(f"  [skip] {row.get('created_at','')[:19]} — 3 failures, sentinel stored")

        log(f"  batch {bi}/{len(batches)} ({len(batch)} texts, ~{sum(len(p) for _, p, _ in batch)//2} est tok) {elapsed:.1f}s")

    elapsed = time.monotonic() - t_start
    log(f"Embed batch done: {ok_count} ok, {fail_count} failed, {elapsed:.0f}s")
    if ok_count == 0 and fail_count > 0:
        sys.exit(1)  # ALL FAILED — signal upstream (day_cycle.sh) to retry


if __name__ == "__main__":
    main()

