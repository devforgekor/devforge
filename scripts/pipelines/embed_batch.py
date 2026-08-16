#!/usr/bin/env python3
# Status: production
# Path: day_cycle.sh
"""Embed Batch Pipeline — text_clean 기준 embedding (unified preprocessing).

State-based: LEFT JOIN embeddings WHERE NULL → embed → INSERT INTO embeddings.
Failed turns do NOT advance — retried next cycle via retry_count.

Uses text_clean (SSOT after text_clean+polish merge).
Backward compat: COALESCE(text_clean, text_clean_polished) for pre-merge data.

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
from typing import Optional
import re
import unicodedata

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.common import log
from lib.db import esc_sql, psql, psql_json, psql_ok
from lib.infra.preflight import preflight_checks
from lib.pod_manager import ensure_model
from lib.text_cleaner import get_cleaner
from lib.watchdog.messenger import heartbeat, resolve_pulse
from lib.work_steal import WorkStealer

EMBED_PORTS = [8080, 8081]  # inference :8080 + inference :8081
MAX_BATCH_SIZE = 10  # max texts per request (safety cap)
BATCH_LIMIT = 50  # max turns per run (sliced via MAX_BATCH_SIZE)
BATCH_TIMEOUT = 600  # per batch request (10min — ~2-4min typical, safety margin for retry)
MAX_CYCLE = 86400  # 24hr max for full 8.6K turn embed
SLOT_CTX = 1600  # token budget per slot (--ctx-size 2048 / --parallel 1 * 0.8 margin)
CHUNK_MAX_TOKENS = 512
SHORT_TURN_CHARS = 200
EMBED_DIMS = 2048


def truncate_to_mrl(vector: list[float], target_dims: int = EMBED_DIMS) -> list[float]:
    truncated = vector[:target_dims]
    norm = sum(x * x for x in truncated) ** 0.5
    if norm > 0:
        truncated = [x / norm for x in truncated]
    return truncated


def split_sentences(text: str) -> list[str]:
    """Language-aware sentence splitting via text_cleaner."""
    cleaner = get_cleaner()
    lang, _ = cleaner.detect_language(text)
    return cleaner.split_sentences(text, lang=lang)


def estimate_tokens(text: str) -> int:
    """Token estimation via tiktoken (o200k_base) from text_cleaner."""
    return get_cleaner().estimate_tokens(text)


def preprocess_for_embed(text: str) -> str:
    """NFKC normalize + collapse whitespace before embedding."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _get_tail_sentences(sentences: list[str], overlap_tokens: int) -> list[str]:
    """Return trailing sentences fitting within overlap_tokens budget (for chunk overlap)."""
    if overlap_tokens <= 0:
        return []
    tail: list[str] = []
    tok = 0
    for sent in reversed(sentences):
        st = estimate_tokens(sent)
        if tok + st > overlap_tokens:
            break
        tail.insert(0, sent)
        tok += st
    return tail


def chunk_text(text: str, overlap: int = 64) -> list[tuple[str, int]]:
    if len(text) < SHORT_TURN_CHARS:
        return [(text, 0)]
    sentences = split_sentences(text)
    chunks: list[tuple[str, int]] = []
    cur: list[str] = []
    cur_tok = 0
    overlap_sentences: list[str] = []
    for sent in sentences:
        sent_tok = estimate_tokens(sent)
        if sent_tok > CHUNK_MAX_TOKENS:
            if cur:
                chunks.append((" ".join(overlap_sentences + cur), len(chunks)))
                overlap_sentences = _get_tail_sentences(cur, overlap)
                cur, cur_tok = [], 0
            max_chars = CHUNK_MAX_TOKENS * 5 // 2
            chunks.append((sent[:max_chars].rstrip(), len(chunks)))
            overlap_sentences = []
        elif cur_tok + sent_tok > CHUNK_MAX_TOKENS:
            if cur:
                chunks.append((" ".join(overlap_sentences + cur), len(chunks)))
            overlap_sentences = _get_tail_sentences(cur, overlap)
            cur, cur_tok = [sent], sent_tok
        else:
            cur.append(sent)
            cur_tok += sent_tok
    if cur:
        chunks.append((" ".join(overlap_sentences + cur), len(chunks)))
    if not chunks:
        chunks = [(text[: CHUNK_MAX_TOKENS * 5 // 2], 0)]
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


def _post_embed(texts: list[str], timeout: int, port: int) -> Optional[dict]:
    """POST to /v1/embeddings with a fresh TCP connection (no reuse — avoids server-side hang)."""
    import json as _json

    body = _json.dumps({"input": texts, "model": "default"}).encode()
    url = f"http://127.0.0.1:{port}/v1/embeddings"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _json.loads(resp.read().decode())
    except Exception as e:
        log(f"  [error] batch embed failed (:{port}, {len(texts)} texts): {e}")
        return None


def embed_batch(
    texts: list[str], timeout: int = 600, port: int = 8081
) -> Optional[list[Optional[list]]]:
    """Send multiple texts to /v1/embeddings, return list of vectors (None for failed)."""
    data = _post_embed(texts, timeout, port)
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


def _skip_unembeddable_turns() -> int:
    """Dead-letter: turns stuck at 'enriched' whose combined content is too
    short to ever pass the embed length gate get moved to 'embed_skipped'
    immediately, at the point this pipeline determines they're unprocessable
    — instead of silently starving forever and blocking day_cycle's
    in-flight gate for every future cycle.

    Note: uses plain psql() (top-level UPDATE ... RETURNING), not psql_json(),
    since Postgres rejects a data-modifying WITH nested inside psql_json's
    "SELECT row_to_json(r) FROM (...) r" wrapper ("WITH clause containing a
    data-modifying statement must be at the top level").
    """
    raw = psql(
        "UPDATE turns SET pipeline_state = 'embed_skipped', "
        "  pipeline_state_reason = 'combined content < 15 chars after cleaning' "
        "WHERE pipeline_state = 'enriched' "
        "  AND LENGTH(TRIM("
        "    COALESCE(user_turn_clean, user_turn_clean_polished, '') || ' ' ||"
        "    COALESCE(text_clean, text_clean_polished, text, '')"
        "  )) < 15 "
        "RETURNING id"
    )
    if not raw:
        return 0
    return len([line for line in raw.splitlines() if line.strip()])


def get_unembedded_turns(limit: int):
    """Return turns without a complete set of embedding chunks."""
    rows = psql_json(
        f"SELECT t.id, "
        f"  COALESCE(t.user_turn_clean, t.user_turn_clean_polished) AS user_turn_clean, "
        f"  COALESCE(t.text_clean, t.text_clean_polished, t.text) AS text_clean, "
        f"  t.user_turn, t.text, t.created_at::text, t.est_chars, "
        f"  t.agent, t.meta->>'model' AS model "
        f"FROM turns t "
        f"WHERE NOT EXISTS ("
        f"  SELECT 1 FROM embeddings e "
        f"  WHERE e.source_type = 'turn' AND e.source_id = t.id "
        f"    AND e.model_name = 'qwen3-embedding-8b-v1'"
        f") "
        f"  AND t.pipeline_state = 'enriched'"
        # Length check must mirror the actual embed text built below
        # (user_turn_clean + text_clean) — checking text_clean alone
        # permanently starves turns with a short assistant reply but
        # substantial user content.
        f"  AND LENGTH(TRIM("
        f"    COALESCE(t.user_turn_clean, t.user_turn_clean_polished, '') || ' ' ||"
        f"    COALESCE(t.text_clean, t.text_clean_polished, t.text, '')"
        f"  )) >= 15"
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


def store_embedding(
    turn_id: str,
    vector: list,
    embed_text: str,
    chunk_index: int = 0,
    metadata: Optional[dict] = None,
):
    """INSERT INTO embeddings for a turn. UPSERT on conflict."""
    vec_str = "[" + ",".join(f"{v:.8f}" for v in vector) + "]"
    safe_text = embed_text[:8000].replace("'", "''")
    meta_json = json.dumps(metadata or {})
    meta_esc = meta_json.replace("'", "''")
    psql_ok(
        f"INSERT INTO embeddings (source_type, source_id, embed_text, embedding, model_name, chunk_index, metadata) "
        f"VALUES ('turn', '{esc_sql(turn_id)}'::uuid, '{safe_text}', "
        f"  '{esc_sql(vec_str)}'::vector, 'qwen3-embedding-8b-v1', {chunk_index}, "
        f"  '{meta_esc}'::jsonb) "
        f"ON CONFLICT (source_type, source_id, model_name, chunk_index) DO UPDATE SET "
        f"  embedding = EXCLUDED.embedding, embed_text = EXCLUDED.embed_text, "
        f"  metadata = EXCLUDED.metadata, created_at = now()"
    )


def store_fact_embedding(fact_id: str, vector: list, embed_text: str):
    """INSERT INTO embeddings for a review_fact."""
    vec_str = "[" + ",".join(f"{v:.8f}" for v in vector) + "]"
    safe_text = embed_text[:8000].replace("'", "''")
    psql_ok(
        f"INSERT INTO embeddings (source_type, source_id, embed_text, embedding, model_name, chunk_index) "
        f"VALUES ('review_fact', '{esc_sql(fact_id)}'::uuid, '{safe_text}', "
        f"  '{esc_sql(vec_str)}'::vector, 'qwen3-embedding-8b-v1', 0) "
        f"ON CONFLICT (source_type, source_id, model_name, chunk_index) DO UPDATE SET "
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
        f"INSERT INTO embeddings (source_type, source_id, embed_text, embedding, model_name, chunk_index) "
        f"VALUES ('feedback_example', '{esc_sql(feedback_id)}'::uuid, '{safe_text}', "
        f"  '{esc_sql(vec_str)}'::vector, 'qwen3-embedding-8b-v1', 0) "
        f"ON CONFLICT (source_type, source_id, model_name, chunk_index) DO UPDATE SET "
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

    # Check inference embed health, fall back to inference only if inference unavailable
    pod_a_healthy = False
    try:
        req = urllib.request.Request("http://127.0.0.1:8080/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            pod_a_healthy = True
    except Exception:
        log("  inference :8080 unavailable — using inference :8081 only")

    ports = EMBED_PORTS if pod_a_healthy else [8081]
    log(f"  Embed ports: {ports}")

    preflight_checks("embed_batch.py", required_ports=set(ports))
    if not ensure_model("embeder", skip_if_healthy=True):
        log("  FATAL: cannot start embedding model on 8081")
        return

    t_start = time.monotonic()

    if feedback_mode:
        rows = get_unembedded_feedback(limit)
    elif facts_mode:
        rows = get_unembedded_facts(limit)
    else:
        skipped = _skip_unembeddable_turns()
        if skipped:
            log(f"  [dead-letter] {skipped} turn(s) too short to embed — marked embed_skipped")
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
            user = row.get("user_turn_clean") or ""
            resp = row.get("text_clean") or ""
            text = f"{user} {resp}".strip()
        if not text:
            prepared.append(("skip", row, None, None))
        else:
            text = preprocess_for_embed(text)
            if feedback_mode or facts_mode:
                text = text[:8192]
                if len(text) < 15:
                    prepared.append(("skip", row, None, None))
                else:
                    prepared.append(("embed", row, text, 0))
            else:
                chunks = chunk_text(text, overlap=64)
                for ct, ci in chunks:
                    if len(ct) < 15:
                        continue
                    prepared.append(("embed", row, ct[:8192], ci))

    emb_rows = [(t, p, c) for (s, t, p, c) in prepared if s == "embed"]
    skip_count = sum(1 for s, _, _, _ in prepared if s == "skip")

    log(f"  Found {len(rows)} {mode.lower()}: {len(emb_rows)} to embed, {skip_count} empty/skip")

    for row, _ in [(t, p) for s, t, p, c in prepared if s == "skip"]:
        log(f"  SKIP (empty) {row.get('created_at', '')[:19]}")

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
        elif cur_tok + est > SLOT_CTX or len(cur_batch) >= MAX_BATCH_SIZE:
            batches.append(cur_batch)
            cur_batch, cur_tok = [item], est
        else:
            cur_batch.append(item)
            cur_tok += est
    if cur_batch:
        batches.append(cur_batch)

    log(f"  Built {len(batches)} batches (max {MAX_BATCH_SIZE} texts / ~{SLOT_CTX} tok per batch)")

    turn_chunk_progress: dict[str, dict] = {}
    _progress_lock = threading.Lock()

    def process_embed_batch(port: int, item: list, item_timeout: int) -> dict:
        """WorkStealer process_fn: embed a single batch on given port, store results."""
        batch = item
        batch_texts = [p for _, p, _ in batch]
        vectors = embed_batch(batch_texts, timeout=item_timeout, port=port)

        if vectors is None:
            return {"ok": False, "error": "embed_batch returned None", "n_items": len(batch)}

        ok = 0
        fail = 0
        for (row, text, ci), vec in zip(batch, vectors):
            if vec is not None:
                if feedback_mode:
                    store_feedback_embedding(row["id"], vec, text)
                elif facts_mode:
                    store_fact_embedding(row["id"], vec, text)
                else:
                    meta = {}
                    if row.get("agent"):
                        meta["agent"] = row["agent"]
                    if row.get("model"):
                        meta["model"] = row["model"]
                    store_embedding(row["id"], vec, text, ci, metadata=meta or None)
                    tid = row["id"]
                    with _progress_lock:
                        if tid not in turn_chunk_progress:
                            total = sum(1 for _, r, _, _ in prepared if r["id"] == tid)
                            turn_chunk_progress[tid] = {"total": total, "ok": 0}
                        turn_chunk_progress[tid]["ok"] += 1
                        complete = (
                            turn_chunk_progress[tid]["ok"] >= turn_chunk_progress[tid]["total"]
                        )
                    if complete:
                        psql_ok(
                            f"UPDATE turns SET pipeline_state = 'embedded' "
                            f"WHERE id = '{esc_sql(tid)}'::uuid"
                        )
                ok += 1
            else:
                fail += 1
                log(f"  [warn] null vector for {row.get('created_at', '')[:19]} chunk {ci}")
                if not facts_mode and not feedback_mode:
                    r = psql_json(f"""
                        UPDATE turns SET retry_count = COALESCE(retry_count, 0) + 1
                        WHERE id = '{esc_sql(row["id"])}'::uuid
                        RETURNING retry_count
                    """)
                    if r and r[0].get("retry_count", 0) >= 3:
                        meta = {}
                        if row.get("agent"):
                            meta["agent"] = row["agent"]
                        if row.get("model"):
                            meta["model"] = row["model"]
                        store_embedding(
                            row["id"],
                            [0.0] * EMBED_DIMS,
                            text or "(sentinel)",
                            0,
                            metadata=meta or None,
                        )
                        psql_ok(
                            f"UPDATE turns SET pipeline_state = 'embedded' "
                            f"WHERE id = '{esc_sql(row['id'])}'::uuid"
                        )
                        log(
                            f"  [skip] {row.get('created_at', '')[:19]} — 3 failures, sentinel stored"
                        )

        return {"ok": ok > 0 or fail == 0, "n_ok": ok, "n_fail": fail, "n_items": len(batch)}

    if dry_run:
        for batch in batches:
            for row, _, _ in batch:
                log(f"  DRY-RUN: {row.get('created_at', '')[:19]}")
        return

    stealer = WorkStealer(ports=ports, item_timeout=BATCH_TIMEOUT)

    def progress_cb(done: int, total: int):
        heartbeat("embed_batch")
        log(f"  [progress] {done}/{total} batches  {stealer.worker_report()}")

    results = stealer.run(
        items=batches,
        process_fn=process_embed_batch,
        progress_cb=progress_cb,
    )

    ok_count = sum(r.get("n_ok", 0) for r in results if r.get("ok"))
    fail_count = sum(r.get("n_fail", 0) for r in results if not r.get("ok"))

    elapsed = time.monotonic() - t_start
    log(
        f"Embed batch done: {ok_count} ok, {fail_count} failed, {elapsed:.0f}s ({stealer.worker_report()})"
    )
    if ok_count == 0 and fail_count > 0:
        sys.exit(1)  # ALL FAILED — signal upstream (day_cycle.sh) to retry


if __name__ == "__main__":
    main()
