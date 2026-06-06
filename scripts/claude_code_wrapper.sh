#!/usr/bin/env bash
set -euo pipefail

# Simple Claude Code wrapper that performs prompt canonicalization and sends to the proxy (DeepSeek-compatible).
# Usage: claude_code_wrapper.sh <model> <prompt-file> <out-json>
# Example: claude_code_wrapper.sh deepseek-v4-flash ./prompt.txt /tmp/resp.json

PROXY_URL=${PROXY_URL:-"http://127.0.0.1:44777/anthropic/v1/chat/completions"}
MODEL="$1"
PROMPT_FILE="$2"
OUT_FILE="${3:-/tmp/claude_wrapper_resp.json}"
SYSTEM_PROMPT="${SYSTEM_PROMPT:-You are a helpful programming assistant. Answer concisely.}"

# Map local/unsupported model names to DeepSeek-supported model names to avoid upstream 400 errors
# e.g., qwen3-30b-a3b-local -> deepseek-v4-flash
case "$MODEL" in
  qwen3-30b-a3b*|qwen3-30b-a3b-local)
    echo "[wrapper] remapping model '$MODEL' -> 'deepseek-v4-flash'" >&2
    MODEL="deepseek-v4-flash"
    ;;
  qwen3-30b)
    echo "[wrapper] remapping model '$MODEL' -> 'deepseek-v4-flash'" >&2
    MODEL="deepseek-v4-flash"
    ;;
  *)
    # leave MODEL unchanged
    ;;
esac

if [ -z "$MODEL" ] || [ ! -f "$PROMPT_FILE" ]; then
  echo "Usage: $0 <model> <prompt-file> <out-json>" >&2
  exit 2
fi

PROMPT_CONTENT=$(cat "$PROMPT_FILE")

# Build a canonical JSON payload using Python: sorted keys, compact separators, and simple normalization.
python3 - "$MODEL" "$SYSTEM_PROMPT" "$PROMPT_CONTENT" /tmp/claude_wrapper_payload.json <<'PY'
import sys, json, re
model = sys.argv[1]
system = sys.argv[2]
user = sys.argv[3]
out_path = sys.argv[4]

# Basic normalizer: remove ISO timestamps and 36-char UUIDs that commonly change between requests.
def normalize(s):
    if not isinstance(s, str):
        return s
    s = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.[0-9]+)?(?:Z|[+-]\d{2}:?\d{2})?","", s)
    s = re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}","", s)
    # trim repeated whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s

payload = {
    "model": model,
    "system": normalize(system),
    "messages": [
        {"role": "user", "content": normalize(user)}
    ]
}
# Write canonical JSON: sort keys and compact separators to keep byte-stable representation
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

print(out_path)
PY

# Send to proxy with curl; capture HTTP code and body
# include Authorization header if DEEPSEEK_API_KEY / ANTHROPIC_AUTH_TOKEN set
CURL_AUTH_OPTS=( )
if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
  CURL_AUTH_OPTS+=( -H "Authorization: Bearer ${DEEPSEEK_API_KEY}" )
elif [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  CURL_AUTH_OPTS+=( -H "Authorization: Bearer ${ANTHROPIC_AUTH_TOKEN}" )
fi

HTTP_CODE=$(curl --http1.1 --connect-timeout 10 --max-time 300 -sS -X POST "$PROXY_URL" -H "Content-Type: application/json" "${CURL_AUTH_OPTS[@]}" --data-binary @/tmp/claude_wrapper_payload.json -o "$OUT_FILE" -w "%{http_code}")

# Append http code to output file for quick inspection
echo "\n__HTTP_CODE__:$HTTP_CODE" >> "$OUT_FILE"

# Print summary
echo "-> Sent canonicalized payload to $PROXY_URL (model=$MODEL). HTTP=$HTTP_CODE -> $OUT_FILE"
exit 0
