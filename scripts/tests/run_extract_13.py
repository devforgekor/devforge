#!/usr/bin/env python3
"""Run extract on our 13 test turns."""
import json, os, sys, time
SCRIPTS_DIR = "/opt/projects/server/scripts"
PIPELINES_DIR = os.path.join(SCRIPTS_DIR, "pipelines")
sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, PIPELINES_DIR)
os.chdir(PIPELINES_DIR)

from lib.db import psql_ok, psql_json, esc_sql
from lib.common import log
from extract import extract_pipeline

TEST_TURNS = [
    "64c18c1a-995b-48ce-88df-f543dd48994c",
    "f6db0d1c-4813-4578-858d-beeb43ff8705",
    "37f7dbe1-6d30-4d3a-b812-bcb99d21c87c",
    "5541eaa4-cf57-4bd4-ab6f-6d76792fe189",
    "5c2dde84-914e-46ab-b5ca-e8814287b599",
    "c3f1df7e-60c8-4bab-a29b-579810005a94",
    "b743a040-515b-405f-ac9d-7ab03abc8bf8",
    "8d123e5b-1e3e-457b-bc37-de3edbc26299",
    "01f63985-1337-4395-85a7-9b2f1cf24c3e",
    "2859bed5-9b18-41a7-8b86-1ac2d1ba6fd5",
    "d452206c-7bf9-43fb-aa9c-71aab593e94c",
    "416bf41f-d416-407d-84c8-24adde4109ac",
    "16bd1c7c-081a-4d28-b57a-71106a162f4a",
]

t0 = time.monotonic()
ok = fail = 0
for i, tid in enumerate(TEST_TURNS, 1):
    log(f"--- [{i}/{len(TEST_TURNS)}] {tid[:12]} ---")
    try:
        r = extract_pipeline(turn_id=tid)
        if r.get("ok"):
            ok += 1
        else:
            fail += 1
            log(f"  FAIL: {r}")
    except Exception as e:
        fail += 1
        log(f"  ERROR: {e}")

elapsed = time.monotonic() - t0
log(f"\nDone: {ok} ok, {fail} fail in {elapsed:.0f}s")
