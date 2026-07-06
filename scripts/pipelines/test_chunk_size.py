#!/usr/bin/env python3
"""Test chunk size impact: runs extract + supplement with EXTRACT_CHUNK_SIZE env var."""

import json, os, subprocess, sys, time, uuid

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)
from lib.db import esc_sql, psql_json, psql_ok

TEST_TEXT = """User: We had a production issue yesterday. The ETL pipeline processing time increased from 45 minutes to 3 hours after deployment. Root cause was a missing index on the event_logs table causing full table scans. A composite index on (created_at, event_type) resolved it and processing returned to normal.

Assistant: I see several issues to address. First, the memory utilization on worker nodes peaked at 87% during peak hours. After increasing heap to 8GB and tuning to G1GC peak utilization dropped to 52%. Second, the API gateway returned 503 errors for 12% of requests during the incident. The connection pool to the inventory service was exhausted at 50 connections.

Third, the SSL certificate renewal failed silently causing the API to serve expired certs for 6 hours. The certbot cron job had been disabled during a server migration. We've now automated monitoring for cert expiry with 30-day warning. Also, Java GC ran every 2 seconds with 4GB heap and 3.8GB live set."""

CHUNK_SIZE = int(sys.argv[1]) if len(sys.argv) > 1 else 600

def setup():
    turn_id, conv_id = str(uuid.uuid4()), str(uuid.uuid4())
    parts = TEST_TEXT.split("\n\nAssistant: ", 1)
    user_turn = parts[0].replace("User: ", "", 1)
    text_part = parts[1] if len(parts) > 1 else ""
    psql_ok(f"INSERT INTO conversations (id,title,source,created_at) VALUES ('{conv_id}'::uuid,'chunk-test','test','2026-07-06T00:00:00Z') ON CONFLICT DO NOTHING")
    seq = int(time.time() * -1000) % 100000
    psql_ok(f"INSERT INTO turns (id,user_turn,text,thinking,pipeline_state,conversation_id,source_message_id,created_at,detected_lang,est_chars,seq) VALUES ('{turn_id}'::uuid,'{esc_sql(user_turn)}','{esc_sql(text_part)}','','scanned','{conv_id}'::uuid,'chunk-test-{turn_id[:8]}','2026-07-06T00:00:00Z','en',{len(TEST_TEXT)},{seq}) ON CONFLICT DO NOTHING")
    return turn_id, conv_id

def cleanup(turn_id, conv_id):
    psql_ok(f"DELETE FROM review_facts WHERE turn_id='{turn_id}'::uuid AND source='extract_pipeline'")
    psql_ok(f"DELETE FROM pipeline_checkpoints WHERE pipeline='extract' AND turn_id='{turn_id}'::uuid")
    psql_ok(f"DELETE FROM turns WHERE id='{turn_id}'::uuid")
    psql_ok(f"DELETE FROM conversations WHERE id='{conv_id}'::uuid AND source='test'")

def check(turn_id, label):
    facts = psql_json(f"SELECT fact_index,extract_model,subject,predicate,object,qualifiers,nli_verdict,verdict FROM review_facts WHERE turn_id='{turn_id}'::uuid AND source='extract_pipeline' ORDER BY fact_index") or []
    g = sum(1 for f in facts if f.get('nli_verdict')=='GROUNDED')
    q = sum(1 for f in facts if f.get('qualifiers') and isinstance(f['qualifiers'],dict) and len(f['qualifiers'])>0)
    s = sum(1 for f in facts if f.get('extract_model')=='day_supplement')
    print(f"\n  [{label}] {len(facts)} facts (G={g} Q={q} S={s})")
    for f in facts:
        subj = (f.get('subject') or '')[:35]
        pred = (f.get('predicate') or '')[:30]
        obj = (f.get('object') or '')[:35]
        nli = f.get('nli_verdict') or 'pending'
        sup = ' [+SUPP]' if f.get('extract_model')=='day_supplement' else ''
        qs = ''
        if f.get('qualifiers'):
            qd = {k:v for k,v in f['qualifiers'].items() if k!='evidence_span'}
            if qd: qs = f' qual={qd}'
        print(f"    fi={f['fact_index']:2d} {nli:10s}{sup}{qs} | {subj} | {pred} | {obj}")
    return facts

turn_id = conv_id = None
t0 = time.monotonic()
try:
    turn_id, conv_id = setup()
    print(f"EXTRACT_CHUNK_SIZE={CHUNK_SIZE}")
    print(f"Turn {turn_id[:8]}")

    # Phase 1: extract
    env = os.environ.copy()
    env["EXTRACT_CHUNK_SIZE"] = str(CHUNK_SIZE)
    extract_script = os.path.join(SCRIPTS_DIR, "pipelines", "extract.py")
    r = subprocess.run([sys.executable, extract_script, "--turn-id", turn_id],
                       capture_output=True, text=True, timeout=3600, env=env)
    print(r.stdout[-3000:] if len(r.stdout)>3000 else r.stdout)
    check(turn_id, "Phase 1")

    # Phase 2: supplement
    supp_script = os.path.join(SCRIPTS_DIR, "pipelines", "post_extract_supplement.py")
    r2 = subprocess.run([sys.executable, supp_script, "--limit", "5"],
                        capture_output=True, text=True, timeout=600, env=env)
    print(r2.stdout[-2000:] if len(r2.stdout)>2000 else r2.stdout)
    check(turn_id, "Phase 2")

    total_s = time.monotonic() - t0
    print(f"\n=== CHUNK_SIZE={CHUNK_SIZE} done in {total_s:.0f}s ===")

except Exception as e:
    print(f"ERROR: {e}")
    import traceback; traceback.print_exc()
finally:
    if turn_id:
        cleanup(turn_id, conv_id)
