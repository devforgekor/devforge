#!/usr/bin/env python3
# Status: experimental
# Path: none — manual 14B single-extract test with direct container start
"14B Q8_0 single extract test — manual container start, no health check race."

import os
import subprocess
import sys
import time
import urllib.request

SCRIPTS_DIR = "/opt/projects/server/scripts"
sys.path.insert(0, SCRIPTS_DIR)
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from lib.llm_client import MODEL_REGISTRY
from lib.pod_manager.container import _podman_start_inference, _podman_stop_inference
from lib.test_common import call_llm, log, parse_llm_json, test_complete, test_setup

# Write env file
ENV = "/opt/ai_data/scripts/current-mode-inference.env"
TEST = test_setup("day_model_test_direct", "14B single-extract test with direct container start")
with open(ENV, "w") as f:
    f.write(
        'MODE=day\nMODEL_NAME="Qwen 2.5 Coder 14B"\nPORT=8082\n'
        "MODEL_FILE=Qwen2.5-Coder-14B-Instruct.Q8_0.gguf\n"
        "CTX_SIZE=16384\nTHREADS=4\nTHREADS_BATCH=4\nCACHE_RAM=2048\n"
    )

# Start 14B container manually (no health check race)
print("\n── Starting 14B Q8_0 container (manual) ──", flush=True)
log("Removing old container...")
subprocess.run(["podman", "rm", "-f", "devforge-inference"], capture_output=True)

log("Starting with 14B Q8_0 (no health check restart)...")
r = subprocess.run(
    [
        "podman",
        "run",
        "-d",
        "--name",
        "devforge-inference",
        "--replace",
        "--rm",
        "--cgroups=split",
        "--pull",
        "missing",
        "--entrypoint",
        "/bin/bash",
        "-v",
        "/opt/ai_data/models/gguf:/models:Z",
        "-v",
        "/opt/ai_data/scripts/inference-entrypoint.sh:/entrypoint.d/inference-entrypoint.sh:Z",
        "-v",
        "/opt/ai_data/scripts/lib/memory_guard.sh:/entrypoint.d/lib/memory_guard.sh:Z",
        "-v",
        "/opt/ai_data/scripts/current-mode-inference.env:/entrypoint.d/current-mode.env:Z",
        "--publish",
        "127.0.0.1:8082:8082",
        "--publish",
        "127.0.0.1:8081:8081",
        "--env",
        "BATCH_SIZE=1024",
        "--env",
        "SERVER_TIMEOUT=28800",
        "--env",
        "MLOCK=0",
        "ghcr.io/ggml-org/llama.cpp:server",
        "/entrypoint.d/inference-entrypoint.sh",
    ],
    capture_output=True,
    timeout=30,
)
cid = r.stdout.decode().strip()
log(f"Container: {cid[:12]}")

# Wait for health (up to 15 min)
log("Waiting for health (max 15 min)...")
t0 = time.monotonic()
for i in range(600):
    try:
        r2 = urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{MODEL_REGISTRY['extractor']['port']}/health", method="GET"
            ),
            timeout=5,
        )
        body = r2.read().decode()
        if r2.status == 200:
            log(f"Ready in {i + 1}s ({time.monotonic() - t0:.0f}s total)")
            break
        if "error" in body:
            log(f"...still loading ({i + 1}s)")
    except Exception:
        if i > 0 and i % 60 == 0:
            log(f"... {i + 1}s (no response)")
    time.sleep(2)
else:
    log("FAILED: model not ready after 1200s")
    sys.exit(1)

# Warm-up call
log("\n── Warm-up ──")
t1 = time.monotonic()
meta = call_llm(
    [
        {"role": "system", "content": "Return empty findings."},
        {"role": "user", "content": "No issues to report."},
    ],
    model="extractor",
    max_tokens=64,
    temperature=0.1,
    timeout=120,
    json_mode=True,
    return_meta=True,
)
elapsed = time.monotonic() - t1
tokens = meta.get("usage", {}).get("completion_tokens", 0)
tps = round(tokens / elapsed, 2) if elapsed > 0 else 0
log(f"Warm-up: {elapsed:.1f}s, {tokens}tok, {tps}t/s")

# Test extract with enrich context
print("\n── Test extract (enrich context) ──", flush=True)
user = """=== USER TURN ===
로그인 API가 3초나 걸리는데 원인이 뭘까?

=== RESPONSE ===
/api/v1/auth/login 확인 결과 auth_routes.py login()이 매 요청마다 새 DB 세션을 염.
AsyncSession이지만 연결 풀링이 전혀 안 됨. 이것이 3초 응답의 주 원인.

=== EXTRACTED FACTS ===
[fact] login API response time is 3 seconds
[fact] auth_routes.py login() opens new async session per request — no pooling
[fact] DB connection pool exhaustion is root cause

=== ENRICH CONTEXT (Global Project Info) ===
[enrich] Repository: devforge/server, Branch: main
[enrich] PostgreSQL 16, pg_trgm enabled
[enrich] Caddy reverse proxy, blue-green deployment
[enrich] LLM models: 7B extractor, 14B verify, 30B night proposer
[enrich] inference swap mechanism for model switching"""

system = """You are a code review extractor. Analyze the code review turn and all context.
Extract ALL potential issues, bugs, security problems, and improvements.

RULES:
- Each finding MUST cite specific evidence from the provided text.
- If no clear issue exists, return empty findings list.
- Consider: security, performance, data_loss, bugs, naming, best practices
- Maximum 20 findings per turn.

Return JSON:
{"findings": [{"id":"D001","severity":"critical|high|medium|low","category":"bug|security|data_loss|performance|quality","description":"...","evidence":"..."}]}"""

t2 = time.monotonic()
meta = call_llm(
    [{"role": "system", "content": system}, {"role": "user", "content": user}],
    model="extractor",
    max_tokens=1024,
    temperature=0.1,
    timeout=600,
    json_mode=True,
    return_meta=True,
)
elapsed2 = time.monotonic() - t2
raw = meta.get("content", "")
parsed = parse_llm_json(raw)
tokens2 = meta.get("usage", {}).get("completion_tokens", 0)
tps2 = round(tokens2 / elapsed2, 2) if elapsed2 > 0 else 0

findings = parsed.get("findings", []) if parsed and isinstance(parsed, dict) else []
print("\n── Result ──")
print(f"  14B extract: {elapsed2:.1f}s, {tokens2}tok, {tps2}t/s")
print(f"  Findings: {len(findings)}")
for f in findings[:5]:
    print(
        f"  [{f.get('id', '?')}] {f.get('severity', '?')} {f.get('category', '?')}: {f.get('description', '')[:100]}"
    )
if len(findings) > 5:
    print(f"  ... and {len(findings) - 5} more")

# Restore 7B
print("\n── Restore 7B extractor ──", flush=True)
_podman_stop_inference()
with open(ENV, "w") as f:
    f.write(
        "MODE=day\nMODEL_NAME=extractor\nPORT=8082\n"
        "MODEL_FILE=Qwen2.5-Coder-7B-Instruct-Q8_0.gguf\n"
        "CTX_SIZE=8192\nTHREADS=4\nTHREADS_BATCH=4\nCACHE_RAM=1024\n"
    )
_podman_start_inference()
log("7B restored")
test_complete("14B direct test done")
