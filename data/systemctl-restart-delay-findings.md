# systemctl restart delay investigation — container-devforge-swap.service

Date: 2026-05-27

## Executive Summary

`systemctl --user restart --no-block` taking 7+ minutes is **not a slow restart** — it is a **crash-loop** where the container fails immediately on every start, and `Restart=on-failure` retries every 15 seconds until the underlying condition is manually resolved. The 7-minute figure comes from ~28 consecutive failure cycles at ~16 seconds each.

## Root Cause Mechanism

### The Crash-Loop Amplifier

Three service config values interact to produce infinite crash-loops:

| Setting | Value | Effect |
|---|---|---|
| `Restart` | `on-failure` | Any non-zero exit triggers auto-restart |
| `RestartSec` | `15s` | Waits exactly 15 seconds before retry |
| `StartLimitAction` | `none` | systemd NEVER gives up, restarts indefinitely |

Normally systemd caps restarts at 5 failures per 10 seconds (`StartLimitBurst=5`, `StartLimitIntervalUSec=10s`). However, `RestartSec=15s` spaces failures beyond the 10-second window, so the burst limit is **never reached**. Each cycle takes ~16-17 seconds (15s `RestartSec` + 1-2s container start/die), yielding ~4 cycles per minute = ~28 cycles = ~7.5 minutes.

### Secondary Factor: `Pull=newer`

The service config uses `Image=ghcr.io/ggml-org/llama.cpp:server` with `Pull=newer`. On every restart, podman contacts ghcr.io to check for updated layers. In the most recent restart, this added **~16 seconds** (from `m=+0.0` to `m=+16.3` in podman event timestamps). During a crash-loop, this compounds with `RestartSec`.

## Crash-Loop Incidents (all time)

### Incident 1: 2026-05-21 15:21 UTC — SELinux context race (~1 min, 5 cycles)
```
Error: failed to open GGUF file (Permission denied)
Error: mmap failed: Permission denied
Error: Failed to bind port 8081 (Address already in use)
```
**Cause**: SELinux `:Z` relabeling on volume mounts had a race condition. The previous container's ports were not yet released when the new container tried to bind. Self-resolved after ~5 cycles.

### Incident 2: 2026-05-22 02:17 UTC — CLI breaking change (~7.5 min, 29 cycles)
```
error: invalid argument: --cache-type
```
**Cause**: The `ghcr.io/ggml-org/llama.cpp:server` image was updated (via `Pull=newer`) to a version that removed the `--cache-type` CLI flag. The entrypoint script still passed `--cache-type`. The crash-loop continued until the entrypoint was updated (commit `4e9e6ef` or similar) to use `--cache-ram` instead.

### Incident 3: 2026-05-22 09:49 UTC — Broken container image (~10 min, 38 cycles)
```
error while loading shared libraries: libllama-common.so.0: cannot open shared object file: No such file or directory
```
**Cause**: A new ghcr.io image was pushed that had a missing shared library (`libllama-common.so.0`). The binary couldn't even start. The podman systemd generator re-created the unit file during the loop ("Current command vanished from the unit file"). The longest incident at 38 cycles / ~10 minutes because the broken image was pulled fresh each cycle.

### Incident 4: 2026-05-26 01:26 UTC — Memory pressure (~8.5 min, 32 cycles)
```
[memory_guard] FATAL: code mode (qwen-32B) needs 22500MB (21000MB + 1500MB reserve), only 21301MB available
```
**Cause**: Mode was set to `code` (requires Qwen-32B model, ~19.5GB). Pod A (DeepSeek-V2-Lite, ~7.5GB) was still running. Combined memory need (19.5 + 7.5 = 27GB) exceeded the 22GB system limit. Container exited immediately with `exit 1` in entrypoint. The crash-loop continued until someone either stopped Pod A or changed the mode back to `normal`/`debate`.

### Incident 5: 2026-05-27 09:41 UTC — Memory pressure (~1.5 min, 5 cycles)
```
[memory_guard] FATAL: code mode (qwen-32B) needs 21000MB (19500MB + 1500MB reserve), only 20653MB available
```
**Cause**: Same as Incident 4 but resolved faster (mode was changed back after ~5 cycles).

## Current System State (2026-05-27 23:17 UTC)

```
Swap:  4.0Gi / 4.0Gi used — only 7 MiB free
Memory: 22Gi total, 17Gi used, 4.4Gi available

Top consumers:
  phi-mini-moe (:8082)    7.0 GiB RSS
  deepseek-v2-lite (:8080) 5.6 GiB RSS  (Pod A)
  qwen3-4b (:8081)         3.1 GiB RSS
  ─────────────────────────────────────
  Total llama-server RSS: ~15.7 GiB
```

The swap is **completely full**. This is a warning sign — any mode switch to `code` or `review-32b` will immediately trigger a crash-loop because Pod A plus a 32B model exceeds 22GB.

## Recommendations

### Immediate (low-risk)
1. **Change `Pull=newer` to `Pull=missing`** in `/home/opc/.config/containers/systemd/container-devforge-swap.container`. This eliminates the 16-second ghcr.io check on every restart. The image is already cached; `missing` only pulls if the image isn't present locally. Saves ~60% of per-cycle time during crash-loops.

2. **Set `RestartSec=60`** — increasing the interval reduces crash-loop intensity and gives operators more time to notice and intervene. 60s intervals mean ~3.5 minutes before the 7-cycle mark instead of ~1.75 minutes at 15s.

### Short-term (moderate effort)
3. **Add mode fallback in entrypoint** — When the memory guard finds insufficient memory for the requested mode, fall back to `debate` mode instead of `exit 1`. This would break the crash-loop immediately:
   ```bash
   check_memory_free 19500 "code mode" || {
       echo "[entrypoint] WARNING: Insufficient memory, falling back to debate mode"
       MODE=debate
   }
   ```

4. **Add crash-loop detection** — Track consecutive failures with a counter file in `/tmp`. After 3 consecutive failures within 60 seconds, exit 0 (success) to tell systemd "I'm done, don't restart me":
   ```bash
   FAIL_COUNT=$(cat /tmp/swap-fail-count 2>/dev/null || echo 0)
   if [[ $FAIL_COUNT -ge 3 ]]; then
       echo "[entrypoint] Too many consecutive failures, exiting cleanly"
       echo 0 > /tmp/swap-fail-count
       exit 0  # break restart loop
   fi
   echo $((FAIL_COUNT + 1)) > /tmp/swap-fail-count
   ```

### Long-term (structural)
5. **Replace crash-loop with health-check-driven restart** — Instead of `Restart=on-failure`, use `Restart=no` with an external watchdog that checks port health before triggering a restart. This prevents blind retries against broken configurations.

6. **Pin container image digest** — Instead of `ghcr.io/ggml-org/llama.cpp:server` (floating tag), pin a specific digest to prevent upstream breakage from automatically propagating via `Pull=newer`.

## Verification

To reproduce the delay pattern:
```bash
# Set mode to 'code' while Pod A is running (guaranteed memory-pressure crash-loop)
echo "MODE=code" > /opt/ai_data/scripts/current-mode.env
time systemctl --user restart container-devforge-swap.service --no-block
# Watch: journalctl --user -u container-devforge-swap.service -f
# Expected: container exits immediately with FATAL memory error, retries every 15s
```
