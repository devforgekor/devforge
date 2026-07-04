#!/usr/bin/env python3
# Status: experimental
# Path: none — 7B vs 9B text extraction comparison
"""Compare 7B vs 9B extraction from narrative Korean text where 4B returns 0.

Tests text sections from Turn 2 (deb06ca2) and Turn 3 (abe91c0c)
where 4B consistently returns 0 facts on both A-Strict and B-Xplore.

Method: Sequential model swap on :8083 (extract-b port).
Launch → health check → extract → pkill → next model."""

import json
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, "/opt/projects/server/scripts")

from lib.model_registry import MODEL_METADATA
from lib.pod_manager import wait_health

# ── Free-form text prompt (no predicate constraints) ──────────

SYSTEM_TEXT_FREE = """\
Extract factual triples (subject, predicate, object) from the ASSISTANT RESPONSE (text).
Base each fact on information present or clearly implied in the text.

Output ONLY valid JSON. No extra text.

RULES:
1. Max 4 facts. If fewer clear facts exist, return only what exists.
2. Each evidence MUST be a sentence ending in period.
3. Self-contained: Resolve pronouns and implicit references.
4. No duplicates: Same fact extracted once only.
5. No fabrication: Only extract what is present or clearly implied.
6. If uncertain, include "confidence": 0.5-0.9 in qualifiers rather than skipping.
7. Skip trivial conversation flow markers.
8. Predicate is descriptive verb phrase — natural language, NOT snake_case.

Output format:
{
  "extractions": [
    {
      "evidence": "<sentence ending with .>",
      "category": "code|decision|explanation|requirement|other",
      "subject": "<entity name>",
      "predicate": "<descriptive verb phrase>",
      "object": "<value>",
      "qualifiers": {},
      "source_context": "<surrounding text>"
    }
  ]
}

If nothing extractable: {"extractions": []}."""


# ── Helpers ──────────────────────────────────────────────────


def load_text_sections():
    """Load text_clean for test turns."""
    sql = """SELECT substring(id::text,1,8) as short_id, id, text_clean
FROM turns WHERE id IN (
  'abe91c0c-f311-48ea-a0a6-893b2ce663b9',
  'deb06ca2-9010-48cf-b964-6ae26ce3863b'
) ORDER BY created_at"""
    raw = (
        subprocess.check_output(
            [
                "podman",
                "exec",
                "postgres",
                "psql",
                "-U",
                "devforge",
                "-d",
                "devforge_app",
                "-t",
                "-A",
                "-F",
                "||",
                "-c",
                sql,
            ]
        )
        .decode()
        .strip()
    )
    sections = []
    for line in raw.strip().split("\n"):
        if not line:
            continue
        parts = line.split("||", 2)
        if len(parts) == 3:
            sections.append((parts[0], parts[2]))
    return sections


def pkill_on_port(port):
    subprocess.run(
        ["podman", "exec", "devforge-inference", "pkill", "-f", f"llama-server.*{port}[^0-9]"],
        timeout=10,
        capture_output=True,
    )


def start_model(model_key, port):
    """Start a model from MODEL_METADATA on the given port."""
    meta = MODEL_METADATA.get(model_key)
    if not meta:
        print(f"  FATAL: {model_key} not in MODEL_METADATA")
        return False
    model_file = meta["file"]
    ctx = meta.get("ctx", 8192)
    threads = meta.get("threads", 4)
    parallel = meta.get("parallel", 1)
    cpus = meta.get("cpus", "")
    launch_cmd = ["/app/llama-server"]
    if cpus:
        launch_cmd = ["taskset", "-c", cpus] + launch_cmd
    cmd = (
        ["podman", "exec", "-d", "devforge-inference"]
        + launch_cmd
        + [
            "-m",
            f"/models/{model_file}",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--ctx-size",
            str(ctx),
            "--parallel",
            str(parallel),
            "--threads",
            str(threads),
            "--temp",
            "0.1",
            "--flash-attn",
            "on",
            "--timeout",
            "28800",
            "--batch-size",
            "512",
            "--ubatch-size",
            "256",
            "--cont-batching",
            "--no-mmap",
            "-lv",
            "1",
            "--metrics",
        ]
    )
    print(f"  [{model_key}] launching {model_file} on :{port}...")
    r = subprocess.run(cmd, capture_output=True, timeout=30, text=True)
    if r.returncode != 0:
        print(f"  [{model_key}] launch failed: {r.stderr.strip()[:200]}")
        return False
    ok = wait_health(port, timeout=180)
    if ok:
        print(f"  [{model_key}] :{port} healthy ({model_file})")
    else:
        print(f"  [{model_key}] :{port} health timeout")
    return ok


def call_llm_direct(prompt, text, port, max_tokens=1024):
    """Direct HTTP call to llama-server on :port. Returns extracted facts list."""
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": text},
    ]
    body = json.dumps(
        {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "stream": False,
        }
    ).encode()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read())
            raw = data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"  [call] error: {e}")
        return []
    # Parse JSON from response
    try:
        parsed = json.loads(raw)
        ex = parsed.get("extractions", [])
        if isinstance(ex, list):
            return ex
    except json.JSONDecodeError:
        pass
    # Try to find JSON in response
    import re as _re

    m = _re.search(r"\{.*\}", raw, _re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group())
            ex = parsed.get("extractions", [])
            if isinstance(ex, list):
                return ex
        except json.JSONDecodeError:
            pass
    print("  [parse] could not extract JSON from response")
    print(f"  raw[:300]: {raw[:300]}")
    return []


# ── Main test ────────────────────────────────────────────────

PORT = 8083

MODELS_TO_TEST = [
    ("day-verifier-b", "7B (qwen2.5-coder-7b)"),
    ("day-enricher-b", "9B (Qwen3.5-9B)"),
]

print("=" * 72)
print("  7B vs 9B TEXT EXTRACTION COMPARISON")
print("  Test: text sections where 4B returns 0 facts")
print("=" * 72)

sections = load_text_sections()
print(f"\n  Loaded {len(sections)} text sections:")
for short_id, text in sections:
    print(f"    [{short_id}] {len(text)} chars")

print("\n  4B BASELINE (from test runs): all 0 facts\n")

for model_key, label in MODELS_TO_TEST:
    print(f"\n-- [{label}] --")

    # Kill whatever is on the port
    pkill_on_port(PORT)
    time.sleep(1)

    # Start test model
    ok = start_model(model_key, PORT)
    if not ok:
        print(f"  SKIP: {label} failed to start")
        continue

    for short_id, text in sections:
        print(f"  --- [{short_id}] text ({len(text)} chars) ---")
        facts = call_llm_direct(SYSTEM_TEXT_FREE, text, PORT)
        print(f"    facts={len(facts)}")
        if facts:
            for f in facts:
                s = f.get("subject", "?")[:40]
                p = f.get("predicate", "?")[:50]
                o = f.get("object", "?")[:60]
                print(f"      [{s}] {p} = {o}")
    # Stop before next model
    pkill_on_port(PORT)
    time.sleep(1)

print(f"\n{'=' * 72}")
print("  DONE. Cleanup: restarting 4B extract-b on :8083...")
pkill_on_port(PORT)
ok = start_model("day-extractor-b", PORT)
if ok:
    print("  4B extract-b restarted on :8083")
else:
    print("  WARNING: could not restart 4B on :8083")
print(f"{'=' * 72}")
