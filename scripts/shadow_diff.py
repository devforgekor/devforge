#!/usr/bin/env python3
# Status: experimental
# Path: none — Phase B/C shadow validation harness (pre-cutover). Run manually or by cutover gate.
"""shadow_diff.py — production vs shadow extract-output diff.

Compares production `public.review_facts` with the shadow pipeline output
`devforge_shadow.review_facts_shadow`, keyed by (turn_id, fact_index, extract_model).
Compares fields: fact_type, evidence, verdict, nli_verdict.

Exit 0 iff the key sets are equal AND all compared fields match (diff=0) —
the Phase C cutover acceptance gate ("shadow 대조 diff=0"). Otherwise exit 1
and print missing/extra/mismatch counts and a sample.

Usage:
  python3 scripts/shadow_diff.py
  python3 scripts/shadow_diff.py --model day-extractor
  python3 scripts/shadow_diff.py --sample 10
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "/opt/projects/server/scripts")

from lib.db import psql_json  # noqa: E402

PROD_TABLE = "public.review_facts"
SHADOW_TABLE = "devforge_shadow.review_facts_shadow"
FIELDS = ("fact_type", "evidence", "verdict", "nli_verdict")


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="restrict to one extract_model")
    ap.add_argument("--sample", type=int, default=5, help="sample mismatches to print")
    args = ap.parse_args()

    prod = _fetch(PROD_TABLE, args.model)
    shadow = _fetch(SHADOW_TABLE, args.model)

    missing = sorted(prod.keys() - shadow.keys())   # in prod, absent in shadow
    extra = sorted(shadow.keys() - prod.keys())     # in shadow, absent in prod
    mismatched = [k for k in (prod.keys() & shadow.keys()) if prod[k] != shadow[k]]
    total = len(prod)
    diff = len(missing) + len(extra) + len(mismatched)

    print("=== shadow_diff ===")
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


if __name__ == "__main__":
    sys.exit(main())
