#!/bin/bash
# DevForge — Podman B mode switcher
# Usage: swap_mode.sh normal|batch|code

set -e
MODE="${1:-}"
if [[ ! "$MODE" =~ ^(normal|batch|code)$ ]]; then
    echo "Usage: $0 normal|batch|code"
    echo "  normal  — Phi-4-mini Q8_0  (8081, 4.1GB)"
    echo "  batch   — Phi-4 14B Q4_K_M  (8081, 8.3GB, 야간 리뷰)"
    echo "  code    — Qwen-32B IQ4_XS   (8081, 16.5GB, swap timers 중지)"
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
    echo "[swap_mode] Stopping ALL scheduled timers (32B needs uninterrupted runtime)..."
    systemctl --user stop swap-normal.timer swap-batch.timer \
        review-worker.timer devforge-nightly.timer 2>/dev/null || true

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

    # Wait for stopped containers to release memory
    echo "[swap_mode] Waiting for stopped containers to release memory..."
    sleep 3

    # Sync dirty pages + report memory state
    sync
    echo "[swap_mode] Memory after cleanup:"
    free -h | grep -E '^Mem:|^Swap:'

    avail_mb=$(free -m | awk 'NR==2{print $7}')
    if [[ "$avail_mb" -lt 19000 ]]; then
        echo "[swap_mode] WARNING: Only ${avail_mb}MB available, 32B needs ~19GB. Risk of OOM."
    fi
fi

if [[ "$CURRENT" == "code" ]]; then
    echo "[swap_mode] Restoring all scheduled timers..."
    systemctl --user start swap-normal.timer swap-batch.timer \
        review-worker.timer devforge-nightly.timer 2>/dev/null || true
fi

echo "[swap_mode] Restarting devforge-swap with $MODE mode..."
systemctl --user restart container-devforge-swap.service

echo "[swap_mode] Waiting for health check..."
MAX_WAIT=900  # 15min for 32B model loading (16.5GB on CPU: ~10-12min typical)
for i in $(seq 1 $((MAX_WAIT/2))); do
    http_code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8081/health 2>/dev/null || echo "000")
    if [[ "$http_code" == "200" ]]; then
        echo "[swap_mode] 8081 healthy — $MODE mode ready"

        # Restart Podman A (Qwen3-4B) when leaving code mode
        if [[ "$CURRENT" == "code" ]]; then
            echo "[swap_mode] Restarting Podman A (Qwen3-4B)..."
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
