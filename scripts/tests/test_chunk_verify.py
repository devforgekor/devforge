#!/usr/bin/env python3
# Status: experimental
# Path: none — test script
"""Test chunk-aware verification — creates synthetic long-source turn + predicates, runs day_verify."""

import json
import os
import subprocess
import sys
import time
from uuid import uuid4

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import PSQL
from lib.pod_manager import ensure_model

TURN_ID = str(uuid4())

# 10 paragraphs × ~380c each = ~3800 chars — duplicates to reach ~8000c for multi-chunk
_SRC = [
    "The ETL pipeline processing time increased from 45 minutes to 3 hours after deployment. The root cause was traced to a missing index on the event_logs table, causing full table scans. A composite index on (created_at, event_type) resolved it and processing returned to 42 minutes.",
    "Memory utilization on worker nodes peaked at 87% during peak hours. Java GC ran every 2 seconds with 4GB heap and 3.8GB live set. After increasing heap to 8GB and tuning to G1GC, peak utilization dropped to 52% and GC pauses went from 200ms to 15ms.",
    "The API gateway returned 503 errors for 12% of requests during the incident. The connection pool to the inventory service was exhausted at 50 connections. Fix: increase pool to 200 and add HikariCP. Error rate dropped to 0.1%.",
    "The auth service logged PII — emails and GPS coordinates — in plain text. Security mandated structured logging with PII redaction. Logs now route through Logstash which strips credit cards, emails, and GPS before Elasticsearch.",
    "CI/CD pipeline failed intermittently on integration tests. 847 tests would randomly fail 3-5 date/time tests. Root cause: shared mutable clock in test fixtures. After refactoring to immutable timestamps and per-test clocks, all 847 pass in under 4 min.",
    "Database replication lag hit 12 seconds causing read-after-write issues. Standby was on 4 vCPU vs primary's 8 vCPU. After upgrading standby to 8 vCPU and enabling sync commit, lag stays under 200ms.",
    "The monitoring stack was missing alerting on p90 latency. Incident response took 45 minutes because no one noticed the degradation. PagerDuty integration was added for all services with p90 > 500ms thresholds. Response time is now under 5 minutes.",
    "Docker image builds were taking 22 minutes due to no layer caching. The Dockerfile rebuilt all dependencies from scratch every time. After restructuring with multi-stage builds and caching the pip layer, build time dropped to 3 minutes.",
    "SSL certificate renewal failed silently, causing the API to serve expired certs for 6 hours. The certbot cron job had been disabled during a server migration. Automated monitoring for cert expiry was added with 30-day warning.",
    "Load testing revealed the checkout service could only handle 50 concurrent users. The bottleneck was a serialized database write lock. After switching to optimistic locking and adding a Redis write queue, throughput increased to 5000 concurrent users.",
]

# Duplicate paragraphs to reach ~7600 chars for multi-chunk test
FULL_TEXT = "\n\n".join(_SRC * 2)


def _psql(sql: str) -> subprocess.CompletedProcess:
    """Run SQL via podman exec postgres psql."""
    r = subprocess.run(PSQL + ["-c", sql], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        print(f"  [psql] ERROR: {r.stderr.strip()[:200]}")
    return r


def _sql_str(val: str) -> str:
    """Escape a string value for SQL single-quoted literal."""
    return "'" + val.replace("'", "''") + "'"


def setup_test_data() -> None:
    print(f"[setup] Creating test turn {TURN_ID[:12]}")
    print(f"[setup] Source text length: {len(FULL_TEXT)} chars")
    print(f"[setup] Expected chunks: ~{len(FULL_TEXT) // 3500 + 1} at 3500c")

    # Create a turn with long source — use existing conversation
    CONV_ID = "0003636b-bc29-4b64-b80b-b9ff78b50f6a"
    text_esc = _sql_str(FULL_TEXT)
    r = _psql(f"""
        INSERT INTO turns (id, conversation_id, seq, user_turn, text, pipeline_state, detected_lang, est_chars)
        VALUES (
            '{TURN_ID}'::uuid,
            '{CONV_ID}'::uuid,
            9999,
            'What caused the performance degradation and what fixes were applied?',
            {text_esc},
            'extracted',
            'en',
            {len(FULL_TEXT)}
        )
    """)
    assert r.returncode == 0, f"INSERT turn failed: {r.stderr}"

    # Insert predicates targeting different chunks + 2 hallucinated
    predicates = [
        (
            0,
            "ETL pipeline processing time increased from 45 minutes to 3 hours after deployment",
            "text",
        ),
        (1, "A missing index on event_logs table caused the slowdown", "text"),
        (2, "Memory utilization peaked at 87% on worker nodes during peak hours", "text"),
        (3, "Java heap was increased from 4GB to 8GB and G1GC was configured", "text"),
        (4, "API gateway had 503 errors for 12% of requests during the incident", "text"),
        (5, "Connection pool was increased from 50 to 200 connections", "text"),
        (6, "Authentication service was logging emails and GPS coordinates in plain text", "text"),
        (7, "Logs are now routed through a Logstash pipeline with PII redaction", "text"),
        (8, "CI/CD pipeline had 847 tests and randomly failed on 3-5 date/time tests", "text"),
        (9, "The fix was to use immutable timestamps and per-test clock instances", "text"),
        # HALLUCINATED — not in source
        (10, "The database was migrated to PostgreSQL 17 during the incident", "text"),
        (11, "A new Kubernetes cluster was deployed with 5 additional worker nodes", "text"),
    ]

    for fi, ev, ft in predicates:
        ev_esc = _sql_str(ev)
        r = _psql(f"""
            INSERT INTO review_facts (turn_id, fact_index, evidence, fact_type, source, verdict, nli_llm)
            VALUES ('{TURN_ID}'::uuid, {fi}, {ev_esc}, '{ft}', 'extract_pipeline', 'pending', NULL)
        """)
        assert r.returncode == 0, f"INSERT fact {fi} failed: {r.stderr}"

    print(f"[setup] Inserted {len(predicates)} predicates (10 real + 2 hallucinated)")


def run_pipeline() -> dict:
    print("\n[run] Starting inference container with day-verifier...")
    ensure_model("day-verifier")
    time.sleep(2)

    print("[run] Running day_verify pipeline on test turn...")
    r = subprocess.run(
        [
            "python3",
            "/opt/projects/server/scripts/pipelines/day_verify.py",
            "--turn-id",
            TURN_ID,
            "--mode",
            "q8",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    stdout = r.stdout
    stderr = r.stderr
    print(stdout)
    if stderr:
        print(f"[run] STDERR: {stderr[:500]}", flush=True)

    for line in reversed(stdout.strip().split("\n")):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"ok": False, "error": "No JSON result found"}


def check_results() -> None:
    print("\n[check] DB results:")
    r = _psql(f"""
        SELECT rf.fact_index, substring(rf.evidence::text, 1, 60) as evidence,
               rf.nli_verdict
        FROM review_facts rf
        WHERE rf.turn_id = '{TURN_ID}'::uuid
          AND rf.source = 'extract_pipeline'
        ORDER BY rf.fact_index
    """)
    print(r.stdout)

    r2 = _psql(f"""
        SELECT rf.nli_verdict, count(*) as cnt
        FROM review_facts rf
        WHERE rf.turn_id = '{TURN_ID}'::uuid
          AND rf.source = 'extract_pipeline'
        GROUP BY rf.nli_verdict ORDER BY rf.nli_verdict
    """)
    print(r2.stdout)

    r3 = _psql(f"""
        SELECT pipeline_state FROM turns WHERE id = '{TURN_ID}'::uuid
    """)
    print(f"Turn state: {r3.stdout.strip()}")

    r4 = _psql(f"""
        SELECT rf.fact_index, rf.nli_verdict FROM review_facts rf
        WHERE rf.turn_id = '{TURN_ID}'::uuid AND rf.fact_index IN (10, 11)
    """)
    print(f"Hallucinated: {r4.stdout.strip()}")


def cleanup() -> None:
    _psql(f"DELETE FROM review_facts WHERE turn_id = '{TURN_ID}'::uuid")
    _psql(f"DELETE FROM turns WHERE id = '{TURN_ID}'::uuid")
    print("[cleanup] test data removed")


if __name__ == "__main__":
    try:
        setup_test_data()
        result = run_pipeline()
        check_results()

        g = result.get("verdicts", {}).get("grounded", 0)
        a = result.get("verdicts", {}).get("ambiguous", 0)
        u = result.get("verdicts", {}).get("ungrounded", 0)
        c = result.get("verdicts", {}).get("contradiction", 0)

        print("\n=== SUMMARY ===")
        print(f"  GROUNDED:      {g} (expect ≥10 real facts)")
        print(f"  AMBIGUOUS:     {a}")
        print(f"  UNGROUNDED:    {u} (expect 2 hallucinated)")
        print(f"  CONTRADICTION: {c}")
        print(f"Source: {len(FULL_TEXT)} chars → ~{len(FULL_TEXT) // 3500 + 1} chunks")
        print(
            f"Chunk-aware MAX-over-chunks: {'ACTIVE' if len(FULL_TEXT) > 3500 else 'N/A (source too short)'}"
        )

    finally:
        cleanup()
