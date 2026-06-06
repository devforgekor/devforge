#!/usr/bin/env bash
set -euo pipefail

# Claude Code proxy request runner — single-mode or A/B multi-variant.
#   Single mode (default):  1 config × N requests  (like old lowqps_runner)
#   A/B mode (--ab):        4 proxy config variants × N requests each  (like old ab_runner)
#
# Usage:
#   claude_code_runner.sh <prompt-file> [options]
#
# Options:
#   -n, --runs N        Requests per variant (default: 200 single, 20 --ab)
#   -o, --output DIR    Output directory
#   --jitter MIN-MAX    Random sleep range between requests (default: 1s fixed)
#   --source-secrets    Source ~/.config/devforge/secrets.env before each request
#   --ab                A/B test across 4 proxy config variants
#   -h, --help          Show this message

PROMPT_FILE="${1:-}"
shift 2>/dev/null || true
RUNS=200
OUTDIR=""
JITTER=""
SOURCE_SECRETS=false
AB_MODE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--runs) RUNS="$2"; shift 2 ;;
    -o|--output) OUTDIR="$2"; shift 2 ;;
    --jitter) JITTER="$2"; shift 2 ;;
    --source-secrets) SOURCE_SECRETS=true; shift ;;
    --ab) AB_MODE=true; shift ;;
    -h|--help)
      sed -n '3,18p' "$0" | sed 's/^# //; s/^#$//'
      exit 0 ;;
    *) echo "Unknown: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$PROMPT_FILE" ] || [ ! -f "$PROMPT_FILE" ]; then
  echo "Usage: claude_code_runner.sh <prompt-file> [options]" >&2
  exit 2
fi

OUTDIR="${OUTDIR:-/tmp/claude_code_runner_$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$OUTDIR"

WRAPPER="/opt/projects/server/scripts/claude_code_wrapper.sh"
PROXY_SERVICE=${PROXY_SERVICE:-"anthropic-deepseek-proxy.service"}

# ── A/B env exports ─────────────────────────────────────────────────
declare -A VAR_EXPORTS
VAR_EXPORTS[canon_sorted_compact]='export ANTHROPIC_PROXY_SORT_KEYS=1; export ANTHROPIC_PROXY_COMPACT_JSON=1; export ANTHROPIC_PROXY_DISABLE_CACHE_KEY=0'
VAR_EXPORTS[canon_sorted]='export ANTHROPIC_PROXY_SORT_KEYS=1; export ANTHROPIC_PROXY_COMPACT_JSON=0; export ANTHROPIC_PROXY_DISABLE_CACHE_KEY=0'
VAR_EXPORTS[nocanon_nosort]='export ANTHROPIC_PROXY_SORT_KEYS=0; export ANTHROPIC_PROXY_COMPACT_JSON=0; export ANTHROPIC_PROXY_DISABLE_CACHE_KEY=0'
VAR_EXPORTS[disable_cache_key]='export ANTHROPIC_PROXY_DISABLE_CACHE_KEY=1; export ANTHROPIC_PROXY_SORT_KEYS=1; export ANTHROPIC_PROXY_COMPACT_JSON=1'

_set_proxy_env() {
  local exports="$1"
  systemctl --user unset-environment ANTHROPIC_PROXY_SORT_KEYS ANTHROPIC_PROXY_COMPACT_JSON ANTHROPIC_PROXY_DISABLE_CACHE_KEY || true
  bash -c "$exports; env | grep -E '^ANTHROPIC_PROXY_(SORT_KEYS|COMPACT_JSON|DISABLE_CACHE_KEY)=' | xargs -L1 -I{} systemctl --user set-environment {}"
  systemctl --user restart "$PROXY_SERVICE"
  sleep 2
}

_restore_proxy_env() {
  systemctl --user unset-environment ANTHROPIC_PROXY_SORT_KEYS ANTHROPIC_PROXY_COMPACT_JSON ANTHROPIC_PROXY_DISABLE_CACHE_KEY || true
  systemctl --user restart "$PROXY_SERVICE" || true
}

# ── CSV header ──────────────────────────────────────────────────────
summary_csv="$OUTDIR/summary.csv"
echo "variant,run,http_code,out_file" > "$summary_csv"

# ── Sleep helper ────────────────────────────────────────────────────
_sleep_between() {
  if [ -n "$JITTER" ]; then
    local min="${JITTER%%-*}" max="${JITTER##*-}"
    local sec
    sec=$(python3 -c "import random; print(random.uniform($min, $max))")
    sleep "$sec"
  else
    sleep 1
  fi
}

# ── Single request ──────────────────────────────────────────────────
_run_one() {
  local variant="$1" i="$2" outfn="$3"
  if $SOURCE_SECRETS; then
    set -a; [ -f /home/opc/.config/devforge/secrets.env ] && source /home/opc/.config/devforge/secrets.env; set +a
  fi
  /bin/bash "$WRAPPER" deepseek-v4-flash "$PROMPT_FILE" "$outfn" || true
  local http_code
  http_code=$(tail -n1 "$outfn" 2>/dev/null | sed -n 's/^__HTTP_CODE__://p' || echo "000")
  [ -z "$http_code" ] && http_code="000"
  echo "$variant,$i,$http_code,$outfn" >> "$summary_csv"
  _sleep_between
}

# ── Main ────────────────────────────────────────────────────────────
if $AB_MODE; then
  # Override default runs for A/B (usually fewer per variant)
  [ "$RUNS" -eq 200 ] && RUNS=20

  for variant in "canon_sorted_compact" "canon_sorted" "nocanon_nosort" "disable_cache_key"; do
    echo "Starting variant: $variant" >&2
    _set_proxy_env "${VAR_EXPORTS[$variant]}"
    sleep 1  # warmup
    for i in $(seq 1 "$RUNS"); do
      _run_one "$variant" "$i" "$OUTDIR/${variant}_run${i}.json"
    done
    echo "Completed variant: $variant" >&2
    sleep 3  # cooldown
  done

  _restore_proxy_env
else
  for i in $(seq 1 "$RUNS"); do
    _run_one "default" "$i" "$OUTDIR/run_${i}.json"
  done
fi

echo "Completed. Summary: $summary_csv" >&2
exit 0
