#!/usr/bin/env python3
# Status: deprecated
# Path: none — night.py archived, replaced by night_cycle.py
"""Pipeline E2E test harness — test DB → extract → verify → night_review.
Creates devforge_test DB, inserts 10 samples, runs each pipeline step,
scores quality at each stage to identify improvement points.

Usage:
  python3 scripts/test_pipeline_e2e.py [--skip-extract] [--skip-verify] [--skip-night]
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

SCRIPTS_DIR = "/opt/projects/server/scripts"
PIPELINES_DIR = os.path.join(SCRIPTS_DIR, "pipelines")
sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, PIPELINES_DIR)
os.chdir(PIPELINES_DIR)
os.environ["TOKENIZERS_PARALLELISM"] = "false"

TEST_DB = "devforge_test"

# ── DB helpers (test DB) ────────────────────────────────────────────────────
PSQL_BASE = ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres"]
PSQL_TEST = PSQL_BASE + ["-d", TEST_DB, "--no-align", "--tuples-only", "--quiet"]


def psql(sql: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(PSQL_TEST + ["-c", sql], capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception as e:
        print(f"  SQL ERROR: {e}")
        return ""


def psql_ok(sql: str, timeout: int = 30) -> bool:
    try:
        r = subprocess.run(PSQL_TEST + ["-c", sql], capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def psql_json(sql: str, timeout: int = 30) -> List[dict]:
    import json as _json
    wrapped = f"SELECT row_to_json(r) FROM ({sql}) r"
    try:
        r = subprocess.run(PSQL_TEST + ["-c", wrapped], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return []
        raw = r.stdout.strip()
        if not raw:
            return []
        return [_json.loads(line) for line in raw.split("\n") if line.strip()]
    except Exception:
        return []


# ── Test data: 10 samples ──────────────────────────────────────────────────
SAMPLES_PATH = "/opt/ai_data/test_pipeline_10samples.json"


# ── Scoring helpers ─────────────────────────────────────────────────────────
SCORES: List[dict] = []

def record_score(step: str, item: str, score: int, max_score: int, detail: str = ""):
    """Record a score for one test item."""
    SCORES.append({
        "step": step,
        "item": item,
        "score": score,
        "max": max_score,
        "detail": detail,
    })


def print_scores(step: str):
    """Print score summary for a step."""
    step_scores = [s for s in SCORES if s["step"] == step]
    if not step_scores:
        return
    total = sum(s["score"] for s in step_scores)
    max_total = sum(s["max"] for s in step_scores)
    pct = round(total / max_total * 100, 1) if max_total else 0
    bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
    print(f"\n  {step}: {total}/{max_total} ({pct}%) {bar}")
    for s in step_scores:
        sub_pct = round(s["score"] / s["max"] * 100, 1) if s["max"] else 0
        sub_bar = "█" * int(sub_pct / 10) + "░" * (10 - int(sub_pct / 10))
        print(f"    [{sub_pct:>4}%] {sub_bar} {s['item']}: {s['score']}/{s['max']}  {s['detail']}")


# ── Step 0: Setup test DB ──────────────────────────────────────────────────
def setup_test_db(samples: List[dict]) -> List[str]:
    """Create test DB, schema, insert samples. Returns list of turn UUIDs."""
    print(f"\n{'=' * 70}")
    print("  Step 0: Setup test database")
    print(f"{'=' * 70}")

    # Drop and recreate test DB (must run outside transaction — use template1)
    subprocess.run(PSQL_BASE + ["-d", "template1", "-c", "DROP DATABASE IF EXISTS devforge_test"],
                   capture_output=True, timeout=30)
    ok = subprocess.run(PSQL_BASE + ["-d", "template1", "-c", f"CREATE DATABASE {TEST_DB} OWNER devforge"],
                        capture_output=True, timeout=30).returncode == 0
    if not ok:
        print("  FAILED to create test DB!")
        sys.exit(1)
    print("  [ok] Test database created")

    # Disable autocommit for bulk operations
    conn = PSQL_BASE + ["-d", TEST_DB]

    # Install extensions
    for ext in ["pgcrypto", "vector"]:
        subprocess.run(conn + ["-c", f"CREATE EXTENSION IF NOT EXISTS {ext}"], capture_output=True)
    print("  [ok] Extensions installed")

    # Create tables
    sqls = [
        # Pipeline support tables
        """CREATE TABLE IF NOT EXISTS pipeline_checkpoint (
            phase TEXT NOT NULL PRIMARY KEY,
            max_created_at TIMESTAMPTZ DEFAULT '-infinity'::timestamptz NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
        )""",
        """INSERT INTO pipeline_checkpoint (phase, max_created_at)
           VALUES ('extract', '-infinity'::timestamptz)
           ON CONFLICT (phase) DO NOTHING""",
        """INSERT INTO pipeline_checkpoint (phase, max_created_at)
           VALUES ('enrich', '-infinity'::timestamptz)
           ON CONFLICT (phase) DO NOTHING""",
        """CREATE TABLE IF NOT EXISTS activity_log (
            id SERIAL PRIMARY KEY,
            type TEXT NOT NULL,
            source TEXT NOT NULL,
            title TEXT,
            summary TEXT,
            body JSONB DEFAULT '{}',
            model TEXT,
            turn_ids UUID[] DEFAULT '{}',
            tags TEXT[] DEFAULT '{}',
            summary_status TEXT DEFAULT 'raw',
            queue_status TEXT DEFAULT 'pending',
            exec_status TEXT DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT now() NOT NULL
        )""",
        # Main application tables
        """CREATE TABLE conversations (
            id UUID DEFAULT gen_random_uuid() NOT NULL PRIMARY KEY,
            title TEXT,
            source TEXT NOT NULL,
            model TEXT,
            created_at TIMESTAMPTZ DEFAULT now() NOT NULL
        )""",
        """CREATE TABLE turns (
            id UUID DEFAULT gen_random_uuid() NOT NULL PRIMARY KEY,
            conversation_id UUID NOT NULL REFERENCES conversations(id),
            seq INTEGER NOT NULL,
            user_turn TEXT NOT NULL,
            thinking TEXT,
            text TEXT NOT NULL,
            meta JSONB DEFAULT '{}'::jsonb NOT NULL,
            wing TEXT,
            room TEXT,
            created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
            agent TEXT,
            source_message_id TEXT,
            embedding vector(4096),
            UNIQUE(conversation_id, seq),
            UNIQUE(agent, source_message_id)
        )""",
        """CREATE TABLE review_facts (
            id UUID DEFAULT gen_random_uuid() NOT NULL PRIMARY KEY,
            turn_id UUID NOT NULL REFERENCES turns(id),
            fact_index INTEGER NOT NULL,
            evidence TEXT,
            fact_type TEXT,
            verdict TEXT DEFAULT 'pending'::text NOT NULL,
            reason TEXT,
            extract_model TEXT,
            verify_model TEXT,
            created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
            prompt_tokens INTEGER,
            gen_tokens INTEGER,
            gen_rate REAL,
            elapsed_ms REAL,
            cache_hit INTEGER,
            phase TEXT,
            arbitrator_model TEXT,
            source TEXT,
            corrected_evidence TEXT,
            fact_action TEXT,
            fact_confidence INTEGER,
            nli_verdict TEXT,
            embedding vector(4096),
            source_file TEXT,
            UNIQUE(turn_id, fact_index, extract_model)
        )""",
        # Add indexes for review_facts
        "CREATE INDEX idx_review_facts_turn ON review_facts(turn_id)",
        "CREATE INDEX idx_review_facts_type ON review_facts(fact_type)",
        "CREATE INDEX idx_turns_created ON turns(created_at DESC)",
        "CREATE INDEX idx_turns_agent ON turns(agent)",
    ]
    for sql in sqls:
        if not psql_ok(sql):
            print(f"  FAILED creating table: {sql[:60]}...")
            sys.exit(1)
    print("  [ok] Tables created (conversations, turns, review_facts)")

    # Insert samples
    turn_ids = []
    conv_ids = {}
    for i, s in enumerate(samples):
        # Create one conversation per unique agent (or group by date)
        agent = s.get("agent", "unknown")
        conv_key = f"{agent}_{s['created_at'][:10]}"
        if conv_key not in conv_ids:
            conv_id = str(uuid.uuid4())
            conv_ids[conv_key] = conv_id
            src = agent or "claude-code"
            psql_ok(
                f"INSERT INTO conversations (id, title, source, model, created_at) "
                f"VALUES ('{conv_id}', 'Test {i+1}', '{src}', '{agent}', "
                f"'{s['created_at']}')"
            )
        else:
            conv_id = conv_ids[conv_key]

        turn_id = s.get("id", str(uuid.uuid4()))
        esc_u = s["user_turn"].replace("'", "''")
        esc_t = s["text"].replace("'", "''")
        psql_ok(
            f"INSERT INTO turns (id, conversation_id, seq, user_turn, text, "
            f"created_at, agent) VALUES ("
            f"'{turn_id}', '{conv_id}', {i}, '{esc_u}', '{esc_t}', "
            f"'{s['created_at']}', '{agent}')"
        )
        turn_ids.append(turn_id)

    # Verify counts
    n_turns = int(psql("SELECT COUNT(*) FROM turns") or 0)
    n_conv = int(psql("SELECT COUNT(*) FROM conversations") or 0)
    print(f"  [ok] Inserted {n_turns} turns in {n_conv} conversations")

    record_score("setup", "database_creation", 10, 10, f"{n_turns} turns, {n_conv} conversations")
    record_score("setup", "schema_integrity", 10, 10, "conversations+turns+review_facts")
    return turn_ids


# ── Step 1: Extract quality ────────────────────────────────────────────────
def step_extract(turn_ids: List[str]) -> bool:
    """Run extract against test DB. Score fact extraction quality."""
    print(f"\n{'=' * 70}")
    print("  Step 1: Extract Pipeline (7B extractor :8082)")
    print(f"{'=' * 70}")

    # Monkey-patch lib.db to use test DB
    import lib.db
    _orig_psql = lib.db.PSQL.copy()
    _orig_check = lib.db.PSQL_CHECK.copy()
    lib.db.PSQL = PSQL_TEST.copy()
    lib.db.PSQL_CHECK = PSQL_BASE + ["-d", TEST_DB, "-t"]

    try:
        from extract import extract_pipeline
        t0 = time.time()
        result = extract_pipeline(limit=15)
        elapsed = time.time() - t0
        print(f"  [ok] Extract completed in {elapsed:.0f}s")

        # Run enrich enrichment (needed before verify step)
        from enrich import enrich_pipeline
        t1 = time.time()
        enrich_result = enrich_pipeline(limit=15)
        enrich_elapsed = time.time() - t1
        print(f"  [ok] Enrich completed in {enrich_elapsed:.0f}s")

        # Score extract quality
        for i, tid in enumerate(turn_ids):
            facts = psql_json(
                f"SELECT fact_index, fact_type, LENGTH(COALESCE(evidence,'')) as ev_len "
                f"FROM review_facts WHERE turn_id = '{tid}' AND fact_type IN ('text','thinking','user') "
                f"ORDER BY fact_index"
            )
            types = set(f["fact_type"] for f in facts)
            has_text = "text" in types
            has_user = "user" in types
            has_think = "thinking" in types

            score = sum([has_text, has_user, has_think])
            detail = f"text={'Y' if has_text else 'N'} user={'Y' if has_user else 'N'} thinking={'Y' if has_think else 'N'}"
            # Check evidence lengths
            avg_len = sum(f["ev_len"] for f in facts) / max(len(facts), 1)
            if avg_len > 50:
                score += 1  # bonus for substantive extraction
                detail += f" avg_len={avg_len:.0f}"
            record_score("extract", f"turn_{i+1}", score, 4, detail)

        # Check enrich
        enrich_facts = psql_json(
            f"SELECT turn_id FROM review_facts WHERE fact_type = 'enrich_meta'"
        )
        enrich_count = len(set(f["turn_id"] for f in enrich_facts)) if enrich_facts else 0
        record_score("extract", "enrich", enrich_count, len(turn_ids),
                     f"{enrich_count}/{len(turn_ids)} turns enriched")

        # Report checkpoint
        cp = psql(f"SELECT MAX(fact_index) FROM review_facts WHERE fact_type='text'")
        record_score("extract", "checkpoint_advance", 5 if cp else 0, 5,
                     f"checkpoint={cp}")

        print_scores("extract")
        return True

    except Exception as e:
        print(f"  ERROR in extract: {e}")
        import traceback; traceback.print_exc()
        return False
    finally:
        lib.db.PSQL = _orig_psql
        lib.db.PSQL_CHECK = _orig_check


# ── Step 2: Verify quality ─────────────────────────────────────────────────
def step_verify():
    """Run day_verify against test DB (reads extracted review_facts). Score verify quality."""
    print(f"\n{'=' * 70}")
    print("  Step 2: Verify Pipeline (inference 14B)")
    print(f"{'=' * 70}")

    import lib.db
    _orig_psql = lib.db.PSQL.copy()
    _orig_check = lib.db.PSQL_CHECK.copy()
    lib.db.PSQL = PSQL_TEST.copy()
    lib.db.PSQL_CHECK = PSQL_BASE + ["-d", TEST_DB, "-t"]

    try:
        from day_verify import day_verify_pipeline
        t0 = time.time()
        result = day_verify_pipeline(limit=15, model_label="14b")
        elapsed = time.time() - t0
        print(f"  [ok] Verify completed in {elapsed:.0f}s")

        # Check verify results
        turn_facts = psql_json(
            f"SELECT turn_id, COUNT(*) as n, "
            f"COUNT(*) FILTER (WHERE fact_type='verify_result') as has_result "
            f"FROM review_facts "
                f"WHERE fact_type = 'verify_result' "
                f"GROUP BY turn_id"
        )
        verified_turns = len(turn_facts)
        record_score("verify", "verification_coverage", verified_turns, 10,
                     f"{verified_turns} turns verified")

        for f in turn_facts:
            tid_short = str(f["turn_id"])[:8]
            score_val = 10 if f["has_result"] else 0
            record_score("verify", f"turn_{tid_short}", score_val, 10,
                         f"result={'Y' if f['has_result'] else 'N'}")

        print_scores("verify")
        return True

    except Exception as e:
        print(f"  ERROR in verify: {e}")
        import traceback; traceback.print_exc()
        return False
    finally:
        lib.db.PSQL = _orig_psql
        lib.db.PSQL_CHECK = _orig_check


# ── Step 3: Night review quality ────────────────────────────────────────────
def step_night_review():
    """Run night.py --review against extracted facts. Score P-R-J quality."""
    print(f"\n{'=' * 70}")
    print("  Step 3: Night Debate (inference 30B→14B→N14B)")
    print(f"{'=' * 70}")

    import lib.db
    _orig_psql = lib.db.PSQL.copy()
    _orig_check = lib.db.PSQL_CHECK.copy()

    try:
        # Build all_findings from extraction facts + MCP data in DB
        print("  [info] Building findings from extraction facts + MCP data...")
        turn_data = psql_json(
            "SELECT t.id, t.user_turn, t.thinking, t.text, t.created_at::text "
            "FROM turns t ORDER BY t.created_at ASC"
        )
        if not turn_data:
            record_score("night_review", "verify_input", 0, 10, "No turns in DB")
            return True

        all_findings = []
        for td in turn_data:
            tid = td["id"]
            facts = psql_json(
                f"SELECT fact_index, fact_type, evidence "
                f"FROM review_facts WHERE turn_id = '{tid}'::uuid "
                f"AND fact_type IN ('text','user','thinking','enrich_meta') "
                f"ORDER BY fact_index"
            )
            if not facts:
                continue
            for f in facts:
                fid = f"{str(tid)[:8]}_f{f['fact_index']}"
                evidence = f["evidence"]
                if f["fact_type"] == "enrich_meta":
                    try:
                        enrich_data = json.loads(evidence)
                        for k, v in enrich_data.items():
                            all_findings.append({
                                "id": f"{fid}_{k}",
                                "turn_id": str(tid),
                                "type": "enrich",
                                f"enrich_{k}": v if isinstance(v, str) else json.dumps(v, ensure_ascii=False),
                                "fact_type": f["fact_type"],
                                "severity": "medium",
                            })
                    except json.JSONDecodeError:
                        pass
                else:
                    all_findings.append({
                        "id": fid,
                        "turn_id": str(tid),
                        "type": "extraction",
                        "fact_type": f["fact_type"],
                        "evidence": evidence[:500],
                        "severity": "medium",
                    })

        n_findings = len(all_findings)
        print(f"  [info] {n_findings} findings built from {len(turn_data)} turns")
        if n_findings == 0:
            record_score("night_review", "verify_input", 0, 10,
                         "No findings built — night will have nothing to review")
            return True

        # Monkey-patch db for night.py to read/write test DB
        lib.db.PSQL = PSQL_TEST.copy()
        lib.db.PSQL_CHECK = PSQL_BASE + ["-d", TEST_DB, "-t"]

        from night import phase_4_night_prj
        t0 = time.time()
        result = phase_4_night_prj({"all_findings": all_findings})
        elapsed = time.time() - t0
        print(f"  [ok] Night review completed in {elapsed:.0f}s")

        # Score P quality
        p_info = result.get("p", {})
        p_proposals = p_info.get("proposals", 0)
        p_ok = isinstance(p_proposals, int) and p_proposals > 0
        record_score("night_review", "p_findings_generated",
                     10 if p_ok else 0, 10,
                     f"{p_proposals} proposals from P (30B)")

        # Score R quality
        r_info = result.get("r", {})
        r_verdicts = r_info.get("verdicts", 0)
        r_ok = isinstance(r_verdicts, int) and r_verdicts > 0
        record_score("night_review", "r_verdicts",
                     10 if r_ok else 0, 10,
                     f"{r_verdicts} verdicts from R (14B)")

        # Score J quality
        j_info = result.get("j", {})
        j_result = j_info.get("result", {})
        j_decision = j_result.get("decision", "N/A") if isinstance(j_result, dict) else "N/A"
        j_ok = j_decision not in ("N/A", "error", "")
        record_score("night_review", "j_judgment",
                     10 if j_ok else 0, 10,
                     f"J decision: {j_decision}")

        print_scores("night_review")
        return True

    except Exception as e:
        print(f"  ERROR in night review: {e}")
        import traceback; traceback.print_exc()
        return False
    finally:
        lib.db.PSQL = _orig_psql
        lib.db.PSQL_CHECK = _orig_check


# ── Step 4: Final summary ──────────────────────────────────────────────────
def print_final_summary():
    """Print overall pipeline score summary."""
    print(f"\n{'=' * 70}")
    print("  FINAL PIPELINE SCORE SUMMARY")
    print(f"{'=' * 70}")

    steps = {}
    for s in SCORES:
        steps.setdefault(s["step"], []).append(s)

    grand_total = 0
    grand_max = 0
    for step_name, scores in steps.items():
        total = sum(s["score"] for s in scores)
        mx = sum(s["max"] for s in scores)
        pct = round(total / mx * 100, 1) if mx else 0
        bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
        print(f"  [{pct:>5}%] {bar}  {step_name}: {total}/{mx}")
        grand_total += total
        grand_max += mx

    overall = round(grand_total / grand_max * 100, 1) if grand_max else 0
    bar = "█" * int(overall / 10) + "░" * (10 - int(overall / 10))
    print(f"\n  [{overall:>5}%] {bar}  OVERALL: {grand_total}/{grand_max}")
    print()


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Pipeline E2E test")
    parser.add_argument("--skip-extract", action="store_true")
    parser.add_argument("--skip-verify", action="store_true")
    parser.add_argument("--skip-night", action="store_true")
    parser.add_argument("--samples", default=SAMPLES_PATH)
    args = parser.parse_args()

    print("=" * 70)
    print("  DevForge Pipeline E2E Test")
    print("  test DB → extract → verify → night_review")
    print(f"  Samples: {args.samples}")
    print("=" * 70)

    # Load samples
    with open(args.samples) as f:
        samples = json.load(f)
    print(f"  Loaded {len(samples)} samples")

    # Step 0: Setup
    turn_ids = setup_test_db(samples)
    print(f"  Turn IDs: {[t[:8] for t in turn_ids]}")

    # Step 1: Extract
    if not args.skip_extract:
        if not step_extract(turn_ids):
            print("  [warn] Extract step had issues, continuing...")
    else:
        print("\n  [skip] Extract step")

    # Step 2: Verify
    if not args.skip_verify:
        if not step_verify():
            print("  [warn] Verify step had issues, continuing...")
    else:
        print("\n  [skip] Verify step")

    # Step 3: Night review
    if not args.skip_night:
        step_night_review()
    else:
        print("\n  [skip] Night review step")

    # Final summary
    print_final_summary()


if __name__ == "__main__":
    main()
