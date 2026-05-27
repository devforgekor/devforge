#!/bin/bash
# DevForge — Podman B mode switcher
# Usage: swap_llm_mode.sh debate|code (normal is deprecated alias for debate)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MEMORY_GUARD="/opt/ai_data/scripts/lib/memory_guard.sh"
if [[ -f "$MEMORY_GUARD" ]]; then
    source "$MEMORY_GUARD"
else
    echo "[swap_mode] WARNING: memory_guard.sh not found at $MEMORY_GUARD"
fi

MODE="${1:-}"
if [[ ! "$MODE" =~ ^(normal|debate|code)$ ]]; then
    echo "Usage: $0 debate|code"
    echo "  debate  — Qwen3-4B (8081) + Phi-mini-MoE (8082) resident debate v4.6"
    echo "  code    — Qwen-32B IQ4_XS (8081, 16.5GB, swap timers 중지)"
    echo "  (normal is deprecated alias for debate)"
    exit 1
fi

MODE_FILE="/opt/ai_data/scripts/current-mode.env"
CURRENT=$(grep -oP 'MODE=\K.*' "$MODE_FILE" 2>/dev/null || echo "unknown")
echo "[swap_mode] current: $CURRENT → $MODE"

if [[ "$CURRENT" == "$MODE" ]]; then
    echo "[swap_mode] Already in $MODE mode, skipping"
    exit 0
fi

echo "MODE=$MODE" > "$MODE_FILE"

# code mode: stop ALL other LLM containers + timers to free memory for 32B
if [[ "$MODE" == "code" ]]; then
    echo "[swap_mode] Stopping scheduled timers (32B needs uninterrupted runtime)..."
    systemctl --user stop review-worker.timer devforge-nightly.timer 2>/dev/null || true

    echo "[swap_mode] Stopping all other LLM containers..."
    for cid in $(podman ps --format '{{.ID}} {{.Names}}' | grep -v 'devforge-swap' | awk '{print $1}'); do
        img=$(podman inspect "$cid" --format '{{.ImageName}}' 2>/dev/null || true)
        if echo "$img" | grep -qi 'llama\|ggml'; then
            name=$(podman inspect "$cid" --format '{{.Name}}' 2>/dev/null)
            echo "[swap_mode]   stopping $name ($img) via systemctl"
            svc="container-${name#/}.service"
            systemctl --user stop "$svc" 2>/dev/null || true
        fi
    done

    # Wait for stopped containers to release memory (with verification)
    echo "[swap_mode] Waiting for stopped containers to release memory..."
    sync

    # Evict all other model page caches — 32B needs every MB
    if declare -f evict_page_cache >/dev/null 2>&1; then
        echo "[swap_mode] Evicting all non-32B model page caches..."
        for m in /opt/ai_data/models/gguf/*.gguf; do
            case "$(basename "$m")" in
                *32B*|*32b*) ;;  # skip 32B itself
                *) evict_page_cache "$m" ;;
            esac
        done
        sync
    fi

    if declare -f wait_memory_release >/dev/null 2>&1; then
        wait_memory_release 19000 30 "32B model load" || {
            echo "[swap_mode] FATAL: Insufficient memory for 32B model. Aborting switch to code mode."
            echo "MODE=$CURRENT" > "$MODE_FILE"
            systemctl --user start review-worker.timer devforge-nightly.timer 2>/dev/null || true
            exit 1
        }
    else
        # Fallback: old behavior with sleep + check
        sleep 5
        sync
        avail_mb=$(free -m | awk 'NR==2{print $7}')
        if [[ "$avail_mb" -lt 19000 ]]; then
            echo "[swap_mode] FATAL: Only ${avail_mb}MB available, 32B needs ~19GB. Aborting."
            echo "MODE=$CURRENT" > "$MODE_FILE"
            systemctl --user start review-worker.timer devforge-nightly.timer 2>/dev/null || true
            exit 1
        fi
    fi

    echo "[swap_mode] Memory after cleanup:"
    free -h | grep -E '^Mem:|^Swap:'
fi

if [[ "$CURRENT" == "code" ]]; then
    echo "[swap_mode] Leaving code mode — restoring timers + evicting 32B page cache..."
    systemctl --user start review-worker.timer devforge-nightly.timer 2>/dev/null || true

    # Evict 32B model from page cache before loading new models
    # 32B is 17GB — if we don't evict, normal mode (14GB) starts with only 7GB free
    if declare -f evict_page_cache >/dev/null 2>&1; then
        evict_page_cache "/opt/ai_data/models/gguf/Qwen2.5-Coder-32B-Instruct-IQ4_XS.gguf"
        sync
        echo "[swap_mode] Memory after 32B eviction:"
        free -h | grep -E '^Mem:|^Swap:'
    fi
fi

echo "[swap_mode] Restarting devforge-swap with $MODE mode..."
systemctl --user restart container-devforge-swap.service

echo "[swap_mode] Waiting for health check..."
MAX_WAIT=900  # 15min for 32B model loading (16.5GB on CPU: ~10-12min typical)
for i in $(seq 1 $((MAX_WAIT/2))); do
    http_code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8081/health 2>/dev/null || echo "000")
    if [[ "$http_code" == "200" ]]; then
        echo "[swap_mode] 8081 healthy — $MODE mode ready"

        # Restart Podman A (DeepSeek-V2-Lite) when leaving code mode
        if [[ "$CURRENT" == "code" ]]; then
            echo "[swap_mode] Restarting Podman A (DeepSeek-V2-Lite :8080)..."
            systemctl --user start container-devforge-qwen.service 2>/dev/null || true
        fi

        exit 0
    elif [[ "$http_code" == "503" ]]; then
        # Model still loading — show progress every 30s
        if [[ $((i % 15)) -eq 0 ]]; then
            echo "[swap_mode] Still loading (${i}x2s elapsed, ${http_code})..."
        fi
    fi
    sleep 2
done
echo "[swap_mode] WARNING: 8081 did not become healthy within $((MAX_WAIT/60))min"
exit 1
