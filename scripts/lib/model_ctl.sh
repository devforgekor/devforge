#!/bin/bash
# model_ctl.sh — DevForge inference container model management shell functions
# Source this file in cycle scripts and pipeline wrappers.
#
# Usage:
#   source /opt/projects/server/scripts/lib/model_ctl.sh
#   _run_model "day-extractor"              # start on default port
#   _run_model "verifier" 8084              # start on custom port
#   _stop_model                            # stop inference
#   _ensure_model "day-extractor"           # smart start (skip if healthy)
#   _ensure_model "verifier" 8084 true 600  # with skip_probe + custom timeout
#
# Port resolution (from model_registry):
#   8080 — reranker
#   8081 — embeder, proposer
#   8082 — day-extractor, day-enricher, day-verifier, reflector
#   8083 — judge, verify-enrich, test-nextcoder, test-qwen
#   8084 — verifier

MODEL_CTL_SCRIPT_DIR="/opt/projects/server/scripts"
MODEL_MODE_ENV="/opt/ai_data/scripts/current-mode-inference.env"

# Inference — podman run --rm (no systemd service)
INFERENCE_RUN_CMD="podman run -d --replace --name devforge-inference --rm \
    --entrypoint /bin/bash \
    --pull newer \
    --network devforge-net \
    -v /opt/ai_data/models/gguf:/models:Z \
    -v /opt/ai_data/scripts/inference-entrypoint.sh:/entrypoint.d/inference-entrypoint.sh:Z \
    -v /opt/ai_data/scripts/current-mode-inference.env:/entrypoint.d/current-mode.env:Z \
    --publish 127.0.0.1:8080:8080 \
    --publish 127.0.0.1:8081:8081 \
    --publish 127.0.0.1:8082:8082 \
    --publish 127.0.0.1:8083:8083 \
    --publish 127.0.0.1:8084:8084 \
    --env SERVER_TIMEOUT=28800 \
    ghcr.io/ggml-org/llama.cpp:server \
    /entrypoint.d/inference-entrypoint.sh"

# ── Resolve model port from registry ──────────────────────────────────
_model_port() {
    local model_key="$1"
    python3 -c "
import sys; sys.path.insert(0, '$MODEL_CTL_SCRIPT_DIR')
from lib.model_registry import MODEL_METADATA
m = MODEL_METADATA.get('$model_key', {})
print(m.get('port', 8082))
"
}

# ── Resolve full env vars from registry ───────────────────────────────
_model_env_vars() {
    local model_key="$1"
    python3 -c "
import sys; sys.path.insert(0, '$MODEL_CTL_SCRIPT_DIR')
from lib.model_registry import MODEL_METADATA
m = MODEL_METADATA.get('$model_key', {})
if not m:
    sys.exit(1)
pairs = [('MODE', m.get('mode', 'day')), ('MODEL_NAME', m.get('model_name', '$model_key')),
         ('PORT', str(m.get('port', 8082))), ('MODEL_FILE', m.get('file', '?'))]
for k, ek in [('ctx','CTX_SIZE'),('threads','THREADS'),('threads_batch','THREADS_BATCH'),
              ('cache_ram','CACHE_RAM'),('mlock','MLOCK'),('evict_room','EVICT_ROOM'),
              ('memory_check','MEMORY_CHECK'),('memory_check_mode','MEMORY_CHECK_MODE'),
              ('report_memory','REPORT_MEMORY'),('cache_type_k','CACHE_TYPE_K'),
              ('cache_type_v','CACHE_TYPE_V'),('flash_attn','FLASH_ATTN'),
              ('batch_size','BATCH_SIZE'),('ubatch_size','UBATCH_SIZE'),
              ('parallel','PARALLEL'),('cpus','CPUS')]:
    v = m.get(k)
    if v is not None and v != '':
        pairs.append((ek, str(v)))
for k, v in pairs:
    print(f'{k}={v}')
"
}

# ── Write mode env file ───────────────────────────────────────────────
_write_mode_env() {
    local model_key="$1"
    local port="${2:-$(_model_port "$model_key")}"
    local env_data
    env_data=$(_model_env_vars "$model_key") || {
        echo "[model_ctl] ERROR: unknown model key '$model_key'" >&2
        return 1
    }
    printf '%s\n' "$env_data" > "$MODEL_MODE_ENV"
    echo "[model_ctl] wrote env for $model_key (:$port) → $MODEL_MODE_ENV"
}

# ── Wait for health endpoint ──────────────────────────────────────────
_wait_health() {
    local port="$1" max_wait="${2:-600}"
    local label="${3:-model}"
    local waited=0
    while [ "$waited" -lt "$max_wait" ]; do
        if curl -sf "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
            echo "[model_ctl] ${label} healthy on :${port} (${waited}s)"
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done
    echo "[model_ctl] ${label} TIMEOUT on :${port} after ${max_wait}s" >&2
    return 1
}

# ── Wait for probe (text generation works) ────────────────────────────
_wait_probe() {
    local port="$1" model_key="$2" max_wait="${3:-300}"
    local waited=0
    local body='{"messages":[{"role":"user","content":"hi"}],"max_tokens":5,"temperature":0.1,"stream":false}'
    while [ "$waited" -lt "$max_wait" ]; do
        if curl -sf -X POST "http://127.0.0.1:${port}/v1/chat/completions" \
            -H "Content-Type: application/json" -d "$body" -o /dev/null 2>/dev/null; then
            echo "[model_ctl] probe OK ($model_key)"
            return 0
        fi
        sleep 3
        waited=$((waited + 3))
    done
    echo "[model_ctl] probe TIMEOUT ($model_key)" >&2
    return 1
}

# ── Check model identity (fingerprint match) ──────────────────────────
_check_model_id() {
    local port="$1" model_key="$2"
    local expected
    expected=$(python3 -c "
import sys; sys.path.insert(0, '$MODEL_CTL_SCRIPT_DIR')
from lib.model_registry import MODEL_METADATA
print(MODEL_METADATA.get('$model_key', {}).get('file', ''))
")
    [ -z "$expected" ] && return 0
    local actual
    actual=$(curl -sf "http://127.0.0.1:${port}/v1/models" 2>/dev/null | python3 -c "
import json,sys; data=json.load(sys.stdin); ms=data.get('models',[]); print(ms[0].get('model','') or ms[0].get('name','') if ms else '')" 2>/dev/null || echo "")
    [ -z "$actual" ] && return 1
    actual=$(basename "$actual")
    if [ "$actual" = "$expected" ]; then
        return 0
    fi
    echo "[model_ctl] MISMATCH :${port} has ${actual}, expected ${expected}" >&2
    return 1
}

# ── Check if a test heartbeat is active ───────────────────────────────
_test_heartbeat_active() {
    python3 -c "
import sys; sys.path.insert(0, '$MODEL_CTL_SCRIPT_DIR')
from lib.db import psql_json
rows = psql_json(\"SELECT pulse_id FROM watchdog_pulses WHERE pulse_id LIKE 'heartbeat_test_%' AND status = 'IN_PROGRESS' LIMIT 1\")
if rows:
    print(rows[0]['pulse_id'])
    sys.exit(0)
sys.exit(1)
" 2>/dev/null
}

# ── Stop inference ────────────────────────────────────────────────────────
_stop_model() {
    echo "[model_ctl] Stopping inference..."
    podman rm -v -f -i devforge-inference 2>/dev/null || true
    echo "[model_ctl] Inference stopped"
}

# ── Start inference + wait ───────────────────────────────────────────────
_run_model() {
    local model_key="$1"
    local port="${2:-$(_model_port "$model_key")}"
    local skip_probe="${3:-false}"
    local health_timeout="${4:-600}"

    echo "[model_ctl] INFERENCE → ${model_key} (:$port)"
    _write_mode_env "$model_key" "$port" || return 1

    # Start container (--replace kills any existing instance)
    $INFERENCE_RUN_CMD 2>&1 || {
        echo "[model_ctl] ERROR: failed to start inference" >&2
        return 1
    }

    # Wait for health
    _wait_health "$port" "$health_timeout" "$model_key" || {
        echo "[model_ctl] health timeout — restarting" >&2
        podman rm -v -f -i devforge-inference 2>/dev/null || true
        $INFERENCE_RUN_CMD 2>&1 || return 1
        _wait_health "$port" 300 "$model_key" || return 1
    }

    # Probe (skip for embed-only models)
    if [ "$skip_probe" != "true" ]; then
        _wait_probe "$port" "$model_key" || return 1
    fi

    echo "[model_ctl] :${port} ready (${model_key})"

    # Verify identity
    _check_model_id "$port" "$model_key" || {
        echo "[model_ctl] WARNING: model identity mismatch on :${port}" >&2
    }

    return 0
}

# ── Smart ensure: skip if healthy + correct model ─────────────────────
_ensure_model() {
    local model_key="$1"
    local port="${2:-$(_model_port "$model_key")}"
    local skip_probe="${3:-false}"
    local health_timeout="${4:-600}"

    # Resolve port from registry if not provided
    [ -z "$2" ] && port=$(_model_port "$model_key")

    # Check if already healthy with correct model
    local current_model=""
    [ -f "$MODEL_MODE_ENV" ] && current_model=$(grep '^MODEL_NAME=' "$MODEL_MODE_ENV" | cut -d= -f2)
    if [ "$current_model" = "$model_key" ] && curl -sf "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
        echo "[model_ctl] Inference already ${model_key} (:$port) — skip restart"
        return 0
    fi

    # Check test heartbeat
    local test_active
    test_active=$(_test_heartbeat_active)
    if [ -n "$test_active" ]; then
        echo "[model_ctl] Test active (${test_active}) — skip inference restart"
        return 0
    fi

    _run_model "$model_key" "$port" "$skip_probe" "$health_timeout"
}

# ── Kill all (emergency stop) ────────────────────────────────────────
_kill_model() {
    echo "[model_ctl] kill: stopping inference..."
    _stop_model
    echo "[model_ctl] kill complete"
}
