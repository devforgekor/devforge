Claude Code proxy request tools

Files:
- claude_code_wrapper.sh : canonicalizes prompts and posts to proxy
- claude_code_runner.sh  : request runner — single-mode or A/B multi-variant

Usage:

  1) Single request:
     /opt/projects/server/scripts/claude_code_wrapper.sh deepseek-v4-flash ./prompt.txt /tmp/resp.json

  2) 200 sequential requests (기본, 1s 간격):
     /opt/projects/server/scripts/claude_code_runner.sh ./prompt.txt

  3) A/B experiment (4 variants × 20회):
     /opt/projects/server/scripts/claude_code_runner.sh ./prompt.txt --ab

   4) 2-5s random jitter (구 lowqps_runner):
      /opt/projects/server/scripts/claude_code_runner.sh ./prompt.txt --jitter 2-5

Options:
  -n, --runs N        Requests per variant (default: 200 single, 20 --ab)
  -o, --output DIR    Output directory
  --jitter MIN-MAX    Random sleep range (default: 1s fixed)
  --ab                A/B test across 4 proxy config variants

Notes:
- Scripts use systemd --user set-environment to set ANTHROPIC_PROXY_* variables used by the proxy (anthropic_proxy.py).
- The proxy service name defaults to 'anthropic-deepseek-proxy.service'. Override by exporting PROXY_SERVICE before running.
- The wrapper strips common volatile tokens (ISO timestamps, UUIDs) and serializes JSON with sorted keys and compact separators to improve cache key stability.
- These tools run locally and do not upload artifacts.
