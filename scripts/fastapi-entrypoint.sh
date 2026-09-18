#!/bin/bash
set -euo pipefail

exec /scripts/deploy/kv-fetch-env.py python3 -m uvicorn devforge_fastapi.app:app --host 0.0.0.0 --port 8002 \
  --proxy-headers --forwarded-allow-ips='*' --log-level info
