#!/usr/bin/env python3
"""embed_turns.py — batch embed unembedded turns via Gemini gemini-embedding-001.

Pipeline:
  1. Query turns with embedding IS NULL
  2. Filter: skip empty/trivial turns (verification step)
  3. Call gemini-embedding-001 batchEmbedContents (768-dim)
  4. Store vectors in pgvector column

Uses KeyRotator for API key rotation. Runs nightly via nightly_batch.sh.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, "/opt/projects/server")

from lib.key_rotator import KeyRotator
from scripts.gemini_rotate import _load_keys, STATE_FILE

from lib.db import psql, psql_ok, PSQL

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768
BATCH_SIZE = 100
MAX_TEXT_CHARS = 1500
MIN_TEXT_CHARS = 20
FETCH_LIMIT = 500


def _psql_pipe(sql: str) -> bool:
    try:
        r = subprocess.run(
            PSQL, input=sql, capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0 and r.stderr:
            print(f"[embed] DB 오류: {r.stderr[:200]}", file=sys.stderr)
        return r.returncode == 0
    except Exception as e:
        print(f"[embed] DB 오류: {e}", file=sys.stderr)
        return False


def _get_rotator() -> KeyRotator:
    keys = _load_keys()
    if not keys:
        raise RuntimeError("No API keys found")
    rotator = KeyRotator(keys)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
            rotator._calls = {int(k): v for k, v in s.get("calls", {}).items()}
            rotator._fails = {int(k): v for k, v in s.get("fails", {}).items()}
            rotator._last_used = {int(k): v for k, v in s.get("last_used", {}).items()}
            rotator._backoff_until = {int(k): v for k, v in s.get("backoff_until", {}).items()}
        except Exception:
            pass
    return rotator


def _save_state(rotator: KeyRotator):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state = {
        "calls": rotator._calls,
        "fails": rotator._fails,
        "last_used": rotator._last_used,
        "backoff_until": rotator._backoff_until,
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.rename(tmp, STATE_FILE)


def _embed_batch(texts: list[str], rotator: KeyRotator) -> list[list[float]]:
    """Call batchEmbedContents, return list of 768-dim vectors."""
    picked = rotator.pick()
    if picked is None:
        raise RuntimeError("All API keys in backoff")
    idx, name, key = picked

    url = (
        f"https://generativelanguage.googleapis.com/v1beta"
        f"/models/{EMBED_MODEL}:batchEmbedContents?key={key}"
    )
    payload = {
        "requests": [
            {
                "model": f"models/{EMBED_MODEL}",
                "content": {"parts": [{"text": t}]},
                "outputDimensionality": EMBED_DIM,
            }
            for t in texts
        ],
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )

    try:
        resp = urllib.request.urlopen(req, timeout=60)
        result = json.loads(resp.read())
        rotator.success(idx)
        _save_state(rotator)
        return [e.get("values", []) for e in result.get("embeddings", [])]
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        if e.code == 429:
            rotator.rate_limited(idx, 60)
            _save_state(rotator)
            raise RuntimeError(f"Rate limited (429): {body}")
        rotator.rate_limited(idx, 30)
        _save_state(rotator)
        raise RuntimeError(f"HTTP {e.code}: {body}")


def _prepare_text(query: str, answer: str) -> Optional[str]:
    """Combine Q+A into embeddable text. Returns None if too short."""
    combined = f"Q: {query}\nA: {answer}".strip()
    if len(combined) < MIN_TEXT_CHARS:
        return None
    if len(combined) > MAX_TEXT_CHARS:
        combined = combined[:MAX_TEXT_CHARS]
    return combined


def _fetch_unembedded() -> list[dict]:
    raw = psql(
        f"SELECT json_build_object("
        f"  'id', id::text,"
        f"  'q', left(user_turn, {MAX_TEXT_CHARS}),"
        f"  'a', left(text, {MAX_TEXT_CHARS})"
        f") FROM turns"
        f" WHERE embedding IS NULL"
        f" ORDER BY created_at"
        f" LIMIT {FETCH_LIMIT}"
    )
    if not raw:
        return []
    rows = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _store_embeddings(pairs: list[tuple[str, list[float]]]) -> bool:
    """Batch UPDATE turns with embedding vectors."""
    stmts = []
    for turn_id, vec in pairs:
        vec_literal = "[" + ",".join(f"{v:.8f}" for v in vec) + "]"
        stmts.append(
            f"UPDATE turns SET embedding = '{vec_literal}'"
            f" WHERE id = '{turn_id}';"
        )
    return _psql_pipe("BEGIN;\n" + "\n".join(stmts) + "\nCOMMIT;\n")


def main():
    rows = _fetch_unembedded()
    if not rows:
        print("[embed] 임베딩할 턴 없음")
        return

    valid = []
    skipped = 0
    for r in rows:
        text = _prepare_text(r["q"], r["a"])
        if text is None:
            skipped += 1
            continue
        valid.append((r["id"], text))

    print(f"[embed] 대상 {len(valid)}개 (건너뜀 {skipped}개, 전체 {len(rows)}개)")
    if not valid:
        return

    rotator = _get_rotator()
    total_ok = 0

    for i in range(0, len(valid), BATCH_SIZE):
        batch = valid[i : i + BATCH_SIZE]
        ids = [b[0] for b in batch]
        texts = [b[1] for b in batch]

        embeddings = None
        for attempt in range(3):
            try:
                embeddings = _embed_batch(texts, rotator)
                break
            except Exception as e:
                wait_secs = rotator.wait_seconds()
                wait = max(2 ** (attempt + 1), wait_secs)
                print(
                    f"[embed] 재시도 {attempt + 1}/3 ({wait:.0f}s 대기): {e}",
                    file=sys.stderr,
                )
                time.sleep(wait)

        if embeddings is None:
            print(f"[embed] 배치 {i // BATCH_SIZE + 1} 실패", file=sys.stderr)
            continue

        if len(embeddings) != len(batch):
            print(
                f"[embed] 배치 크기 불일치: {len(embeddings)} != {len(batch)}",
                file=sys.stderr,
            )
            continue

        pairs = list(zip(ids, embeddings))
        if _store_embeddings(pairs):
            total_ok += len(batch)
            print(f"[embed] 배치 {i // BATCH_SIZE + 1}: {len(batch)}개 저장")
        else:
            print(
                f"[embed] 배치 {i // BATCH_SIZE + 1}: DB 저장 실패",
                file=sys.stderr,
            )

        time.sleep(3)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[embed] 완료 {ts}: {total_ok}/{len(valid)}개 임베딩")


if __name__ == "__main__":
    main()
