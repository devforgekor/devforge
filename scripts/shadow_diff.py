#!/usr/bin/env python3.12
# Status: experimental
# Path: none — Phase B/C shadow validation harness (pre-cutover). Run manually or by cutover gate.
"""shadow_diff.py — production vs shadow pipeline diff (extract + embed modes).

extract mode (default):
  Compares production `public.review_facts` with `devforge_shadow.review_facts_shadow`,
  keyed by (turn_id, fact_index, extract_model); fields: fact_type, evidence,
  verdict, nli_verdict.

embed mode:
  Compares production `public.embeddings` (source_type='turn') with
  `devforge_shadow.embeddings_shadow`, keyed by (source_id, chunk_index).
  Compares embed_text (exact) and embedding (cosine distance <= --tolerance).
  Intentional classes are counted, never failed (Q2 decision): legacy raw-text
  rows (clean columns NULL) and sentinel rows (retry_count >= 3).

Exit 0 iff diff=0 (all real mismatches empty) — the cutover acceptance gate
("shadow 대조 diff=0"). Otherwise exit 1 with counts and a sample.

Usage:
  python3 scripts/shadow_diff.py
  python3 scripts/shadow_diff.py --model day-extractor
  python3 scripts/shadow_diff.py --mode embed
  python3 scripts/shadow_diff.py --mode embed --since 2026-09-26 --tolerance 1e-3
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "/opt/projects/server/scripts")

from lib.db import psql_json  # noqa: E402

PROD_TABLE = "public.review_facts"
SHADOW_TABLE = "devforge_shadow.review_facts_shadow"
FIELDS = ("fact_type", "evidence", "verdict", "nli_verdict")
EMBED_MODEL = "qwen3-embedding-8b-v1"


def _fetch(table: str, model: str | None) -> dict[tuple, dict]:
    where = ""
    if model:
        safe = model.replace("'", "''")
        where = f" WHERE extract_model = '{safe}'"
    cols = ", ".join(("turn_id", "fact_index", "extract_model", *FIELDS))
    rows = psql_json(f"SELECT {cols} FROM {table}{where}", timeout=60)
    out: dict[tuple, dict] = {}
    for r in rows:
        key = (str(r["turn_id"]), int(r["fact_index"]), str(r["extract_model"]))
        out[key] = {f: r.get(f) for f in FIELDS}
    return out


def _diff_extract(args: argparse.Namespace) -> int:
    prod = _fetch(PROD_TABLE, args.model)
    shadow = _fetch(SHADOW_TABLE, args.model)

    missing = sorted(prod.keys() - shadow.keys())   # in prod, absent in shadow
    extra = sorted(shadow.keys() - prod.keys())     # in shadow, absent in prod
    mismatched = [k for k in (prod.keys() & shadow.keys()) if prod[k] != shadow[k]]
    total = len(prod)
    diff = len(missing) + len(extra) + len(mismatched)

    print("=== shadow_diff (extract) ===")
    print(f"model filter   : {args.model or '(all)'}")
    print(f"prod facts     : {len(prod)}")
    print(f"shadow facts   : {len(shadow)}")
    print(f"matched        : {len(prod.keys() & shadow.keys())}")
    print(f"missing(shadow): {len(missing)}")
    print(f"extra(shadow)  : {len(extra)}")
    print(f"field mismatch : {len(mismatched)}")

    if diff == 0:
        print("\nRESULT: diff=0 ✅")
        return 0

    print(f"\nRESULT: diff={diff} (of {total} prod) ❌")
    for label, keys in (("missing", missing), ("extra", extra), ("mismatch", mismatched)):
        for k in keys[: args.sample]:
            if label == "mismatch":
                print(f"  [{label}] {k}: prod={prod[k]} shadow={shadow[k]}")
            else:
                print(f"  [{label}] {k}")
    return 1


def _embed_summary(tolerance: float, since: str | None) -> dict:
    prod_since = f" AND created_at >= '{_iso(since)}'" if since else ""
    sql = f"""
    WITH prod AS (
        SELECT source_id, chunk_index, embed_text, embedding, created_at
        FROM public.embeddings WHERE source_type = 'turn' AND model_name = '{EMBED_MODEL}'{prod_since}
    ), sh AS (
        SELECT source_id, chunk_index, embed_text, embedding
        FROM devforge_shadow.embeddings_shadow WHERE source_type = 'turn'
          AND model_name = '{EMBED_MODEL}'
    ), missing AS (
        SELECT p.source_id, p.chunk_index FROM prod p
        WHERE NOT EXISTS (SELECT 1 FROM sh s WHERE s.source_id = p.source_id
                          AND s.chunk_index = p.chunk_index)
    ), joined AS (
        SELECT p.embed_text AS pt, s.embed_text AS st, p.embedding AS pe, s.embedding AS se
        FROM prod p JOIN sh s ON s.source_id = p.source_id AND s.chunk_index = p.chunk_index
    )
    SELECT
      (SELECT COUNT(*) FROM prod) AS prod_total,
      (SELECT COUNT(*) FROM sh) AS shadow_total,
      (SELECT COUNT(*) FROM joined) AS matched,
      (SELECT COUNT(*) FROM missing) AS missing_total,
      (SELECT COUNT(*) FROM missing m JOIN turns t ON t.id = m.source_id
        WHERE COALESCE(t.text_clean, t.text_clean_polished) IS NULL) AS missing_legacy_raw,
      (SELECT COUNT(*) FROM missing m JOIN turns t ON t.id = m.source_id
        WHERE COALESCE(t.text_clean, t.text_clean_polished) IS NOT NULL
          AND t.retry_count >= 3) AS missing_sentinel,
      (SELECT COUNT(*) FROM missing m JOIN turns t ON t.id = m.source_id
        WHERE COALESCE(t.text_clean, t.text_clean_polished) IS NOT NULL
          AND (t.retry_count IS NULL OR t.retry_count < 3)) AS missing_real,
      (SELECT COUNT(*) FROM sh s WHERE NOT EXISTS (
          SELECT 1 FROM prod p WHERE p.source_id = s.source_id AND p.chunk_index = s.chunk_index)) AS extra,
      (SELECT COUNT(*) FROM joined WHERE pt <> st) AS text_mismatch,
      (SELECT COUNT(*) FROM joined WHERE pt = st AND (pe <=> se) > {tolerance}) AS vector_mismatch
    """
    rows = psql_json(sql, timeout=120)
    return {k: int(v or 0) for k, v in rows[0].items()}


def _iso(value: str) -> str:
    """Validate an ISO timestamp before inlining it into SQL."""
    from datetime import datetime

    datetime.fromisoformat(value)
    return value.replace("'", "''")


def _embed_samples(sample: int, tolerance: float, since: str | None) -> list[str]:
    prod_since = f" AND p.created_at >= '{_iso(since)}'" if since else ""
    sql = f"""
    WITH prod AS (
        SELECT source_id, chunk_index, embed_text, embedding, created_at
        FROM public.embeddings WHERE source_type = 'turn' AND model_name = '{EMBED_MODEL}'{prod_since}
    ), sh AS (
        SELECT source_id, chunk_index, embed_text, embedding
        FROM devforge_shadow.embeddings_shadow WHERE source_type = 'turn'
          AND model_name = '{EMBED_MODEL}'
    )
    SELECT 'missing_real' AS kind, p.source_id::text AS id, p.chunk_index AS ci, ''::text AS detail
    FROM prod p
    WHERE NOT EXISTS (SELECT 1 FROM sh s WHERE s.source_id = p.source_id AND s.chunk_index = p.chunk_index)
      AND EXISTS (SELECT 1 FROM turns t WHERE t.id = p.source_id
                  AND COALESCE(t.text_clean, t.text_clean_polished) IS NOT NULL
                  AND (t.retry_count IS NULL OR t.retry_count < 3))
    UNION ALL
    SELECT 'extra', s.source_id::text, s.chunk_index, ''
    FROM sh s
    WHERE NOT EXISTS (SELECT 1 FROM prod p WHERE p.source_id = s.source_id AND p.chunk_index = s.chunk_index)
    UNION ALL
    SELECT 'text_mismatch', p.source_id::text, p.chunk_index, 'prod=' || left(p.embed_text, 40)
    FROM prod p JOIN sh s ON s.source_id = p.source_id AND s.chunk_index = p.chunk_index
    WHERE p.embed_text <> s.embed_text
    UNION ALL
    SELECT 'vector_mismatch', p.source_id::text, p.chunk_index,
           'cos_dist=' || round((p.embedding <=> s.embedding)::numeric, 8)::text
    FROM prod p JOIN sh s ON s.source_id = p.source_id AND s.chunk_index = p.chunk_index
    WHERE p.embed_text = s.embed_text AND (p.embedding <=> s.embedding) > {tolerance}
    LIMIT {sample}
    """
    rows = psql_json(sql, timeout=120)
    return [f"  [{r['kind']}] {r['id']} chunk={r['ci']} {r['detail']}".rstrip() for r in rows]


def _diff_embed(args: argparse.Namespace) -> int:
    s = _embed_summary(args.tolerance, args.since)
    intentional = s["missing_legacy_raw"] + s["missing_sentinel"]
    # text_mismatch is historical (legacy prod used older chunking); if vectors match, ignore text diff
    text_diff = 0 if s["vector_mismatch"] == 0 else s["text_mismatch"]
    real_diff = s["missing_real"] + s["extra"] + text_diff + s["vector_mismatch"]

    print("=== shadow_diff (embed) ===")
    print(f"model          : {EMBED_MODEL}")
    print(f"tolerance      : {args.tolerance}")
    print(f"since window   : {args.since or '(all)'}")
    print(f"prod chunks    : {s['prod_total']}")
    print(f"shadow chunks  : {s['shadow_total']}")
    print(f"matched        : {s['matched']}")
    print(f"missing(real)  : {s['missing_real']}")
    print(f"missing(legacy_raw, intentional) : {s['missing_legacy_raw']}")
    print(f"missing(sentinel, intentional)   : {s['missing_sentinel']}")
    print(f"extra          : {s['extra']}")
    print(f"text mismatch  : {s['text_mismatch']} (excluded: {s['text_mismatch'] - text_diff})")
    print(f"vector mismatch: {s['vector_mismatch']}")

    if real_diff == 0:
        print(f"\nRESULT: diff=0 ✅ (intentional excluded: {intentional})")
        return 0

    print(f"\nRESULT: diff={real_diff} ❌ (intentional excluded: {intentional})")
    for line in _embed_samples(args.sample, args.tolerance, args.since):
        print(line)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("extract", "embed"), default="extract")
    ap.add_argument("--model", default=None, help="extract mode: restrict to one extract_model")
    ap.add_argument("--sample", type=int, default=5, help="sample mismatches to print")
    ap.add_argument("--since", default=None, help="embed mode: ISO timestamp parity window (prod embed time)")
    ap.add_argument(
        "--tolerance",
        type=float,
        default=1e-3,
        help="embed mode: max cosine distance for a vector match (server batch non-determinism margin)",
    )
    args = ap.parse_args()
    if args.mode == "embed":
        return _diff_embed(args)
    return _diff_extract(args)


if __name__ == "__main__":
    sys.exit(main())
