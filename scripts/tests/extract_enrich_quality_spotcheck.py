#!/usr/bin/env python3
# Status: experimental
# Path: none — manual extract+enrich quality spot-check
"""extract_enrich_quality_spotcheck.py

샘플 10~15개 turn을 뽑아서 extract facts + enrich metadata 품질을
사람이 직접 평가할 수 있는 보고서를 생성.

사용법:
  python3 scripts/tests/extract_enrich_quality_spotcheck.py [--limit 12] [--days 5]
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql, psql_json, esc_sql

REPORT = []

def pr(title: str = "", body: str = "", end: str = "\n"):
    REPORT.append((title, body, end))

def collect_samples(limit: int = 12, days: int = 5):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    sql = f"""
        SELECT t.id, t.user_turn, t.text, t.created_at::text
        FROM turns t
        WHERE EXISTS (
            SELECT 1 FROM review_facts rf
            WHERE rf.turn_id = t.id AND rf.fact_type = 'enrich_meta'
        )
        AND t.created_at > '{cutoff.isoformat()}'
        ORDER BY t.created_at DESC
        LIMIT {limit}
    """
    return psql_json(sql) or []

def get_facts(turn_id: str):
    sql = f"""
        SELECT fact_type, evidence::text, fact_index, fact_action
        FROM review_facts
        WHERE turn_id = '{esc_sql(turn_id)}'::uuid
          AND fact_type IN ('text','user','thinking')
        ORDER BY fact_index ASC
    """
    return psql_json(sql) or []

def get_enrich(turn_id: str):
    sql = f"""
        SELECT evidence::text
        FROM review_facts
        WHERE turn_id = '{esc_sql(turn_id)}'::uuid
          AND fact_type = 'enrich_meta'
        ORDER BY fact_index DESC LIMIT 1
    """
    rows = psql_json(sql) or []
    if not rows:
        return None
    try:
        return json.loads(rows[0]["evidence"])
    except (json.JSONDecodeError, KeyError):
        return None

def get_enrich_verify(turn_id: str):
    sql = f"""
        SELECT evidence::text
        FROM review_facts
        WHERE turn_id = '{esc_sql(turn_id)}'::uuid
          AND fact_type = 'enrich_verify'
        ORDER BY fact_index DESC LIMIT 1
    """
    rows = psql_json(sql) or []
    if not rows:
        return None
    try:
        return json.loads(rows[0]["evidence"])
    except (json.JSONDecodeError, KeyError):
        return None

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--days", type=int, default=5)
    args = ap.parse_args()

    turns = collect_samples(args.limit, args.days)
    pr(f"# Extract + Enrich Quality Spot-Check", end="\n\n")
    pr(f"샘플: {len(turns)}개 turn (최근 {args.days}일, enrich_meta 존재)", end="\n\n")
    pr("---", end="\n\n")

    for i, turn in enumerate(turns, 1):
        tid = turn["id"]
        user_turn = turn.get("user_turn", "") or ""
        text = turn.get("text", "") or ""
        created = turn.get("created_at", "?")

        pr(f"## [{i}] {tid[:8]} ({created})", end="\n\n")

        # 원본 대화
        pr("### User Turn", end="\n\n")
        pr(user_turn[:500], end="\n\n") if user_turn else pr("*(empty)*", end="\n\n")
        pr("### Response (text)", end="\n\n")
        pr(text[:600], end="\n\n") if text else pr("*(empty)*", end="\n\n")

        # Extracted facts
        facts = get_facts(tid)
        pr("### Extracted Facts", end="\n\n")
        if facts:
            pr("| # | type | evidence (first 120) |", end="\n")
            pr("|---|------|---------------------|", end="\n")
            for j, f in enumerate(facts):
                ev = (f.get("evidence") or "")[:120]
                pr(f"| {j} | {f['fact_type']} | {ev} |", end="\n")
        else:
            pr("*(no extraction facts found)*", end="\n\n")
        pr(end="\n")

        # Enrich metadata
        enrich = get_enrich(tid)
        pr("### Enrich Metadata", end="\n\n")
        if enrich:
            pr(f"- **tldr**: {enrich.get('tldr', '?')}", end="\n")
            pr(f"- **intent**: {enrich.get('intent', '?')}", end="\n")
            pr(f"- **category**: {enrich.get('category', '?')}", end="\n")

            entities = enrich.get("entities", {}) or {}
            files = entities.get("files", [])
            funcs = entities.get("functions", [])
            techs = entities.get("technologies", [])
            users = entities.get("mentioned_users", [])
            pr(f"- **entities**: {len(files)} files, {len(funcs)} funcs, {len(techs)} techs, {len(users)} users", end="\n")
            if files:
                pr(f"  - files: {files}", end="\n")
            if funcs:
                pr(f"  - funcs: {funcs}", end="\n")
            if techs:
                pr(f"  - techs: {techs}", end="\n")

            tags = enrich.get("tags", [])
            pr(f"- **tags**: {tags}", end="\n")

            # Faithfulness scores
            faith = enrich.get("faithfulness", {}) or {}
            if faith:
                flist = []
                for fcat in ("files", "technologies", "functions"):
                    for item in faith.get(fcat, []):
                        flist.append(f"{item['entity']}={item.get('score',0):.0f} {item.get('grounding','?')}")
                if faith.get("tldr"):
                    flist.append(f"tldr={faith['tldr'].get('score',0):.0f} {faith['tldr'].get('grounding','?')}")
                pr(f"- **faithfulness**: {', '.join(flist)}", end="\n")

            # Verified
            verified = enrich.get("verified", {}) or {}
            missing_files = [f["path"] for f in verified.get("files", []) if not f.get("exists")]
            if missing_files:
                pr(f"- ⚠️ **missing files**: {missing_files}", end="\n")
        else:
            pr("*(no enrich metadata)*", end="\n\n")
        pr(end="\n")

        # Enrich_verify if exists
        evf = get_enrich_verify(tid)
        if evf:
            pr("### Enrich Verify Score", end="\n\n")
            pr(f"- overall: **{evf.get('overall', '?')}**", end="\n")
            pr(f"- tldr_accuracy: {evf.get('tldr_accuracy', '?')}, intent: {evf.get('intent_correctness', '?')}", end="\n")
            pr(f"- entity_precision: {evf.get('entity_precision', '?')}, entity_recall: {evf.get('entity_recall', '?')}", end="\n")
            pr(f"- tag_relevance: {evf.get('tag_relevance', '?')}", end="\n")
            if evf.get("issues"):
                pr("  **issues**:", end="\n")
                for iss in evf["issues"]:
                    pr(f"  - {iss}", end="\n")
        pr(end="\n")
        pr("---", end="\n\n")

    # Summary
    pr("# Summary", end="\n\n")
    pr(f"총 {len(turns)}개 turn 검토.", end="\n")
    pr("각 항목별로 확인할 점:", end="\n")
    pr("1. **Extract hallucination** — facts가 원본 turn과 일치하는가?", end="\n")
    pr("2. **Enrich tldr** — 한 줄 요약이 정확한가?", end="\n")
    pr("3. **Enrich intent/category** — 의도 분류가 맞는가?", end="\n")
    pr("4. **Enrich entities/files** — 코드베이스와 일치하는가? 누락/환각?", end="\n")
    pr("5. **Enrich tags** — 검색에 유용한가?", end="\n")
    pr("6. **Faithfulness** — reranker 점수가 실제로 믿을 만한가? (거짓 긍정/부정)", end="\n")

    # Output
    output = []
    for title, body, end in REPORT:
        output.append(f"{title}{body}{end}")
    report = "".join(output)

    out_path = os.path.join(SCRIPTS_DIR, "..", "data", "eval", f"quality_spotcheck_{datetime.now():%m%d_%H%M}.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(report)
    print(f"Report saved to {out_path}")
    print(report)

if __name__ == "__main__":
    main()
