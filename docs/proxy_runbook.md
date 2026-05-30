Proxy Runbook — Anthropic DeepSeek compatibility proxy

Purpose
- Document how to manage the local Anthropic compatibility proxy (127.0.0.1:44777) that forwards requests to the local LLM upstream (e.g. Qwen on :8080).

Quick actions
- Restart proxy (systemd user):
  systemctl --user daemon-reload
  systemctl --user restart anthropic-deepseek-proxy.service
- Stop/start:
  systemctl --user stop anthropic-deepseek-proxy.service
  systemctl --user start anthropic-deepseek-proxy.service

Environment variables
- ANTHROPIC_PROXY_UPSTREAM: upstream base URL (default https://api.deepseek.com/anthropic). Example: http://127.0.0.1:8080
- ANTHROPIC_PROXY_LISTEN: listen address (default 127.0.0.1:44777)
- ANTHROPIC_PROXY_DEBUG=1 : enable writing request/headers dumps to /tmp/anthropic_debug_<ts>_*.json (only for short-term debugging)
- ANTHROPIC_PROXY_STREAM_CHUNK: chunk size (bytes) for streaming reads (default 4096)
- ANTHROPIC_PROXY_STREAM_TIMEOUT: idle timeout (seconds) for streaming reads (default 10)

Logs
- journalctl --user -u anthropic-deepseek-proxy.service -n 400 --no-pager
- temporary debug dumps: /tmp/anthropic_debug_*.json (only when ANTHROPIC_PROXY_DEBUG=1)
- E2E test artifacts collected in /tmp/full_e2e_results.tar.gz and /tmp/debug_dump_archive.tar.gz

Behavior and notes
- Path normalization: proxy supports /anthropic/v1/*, /v1/*, legacy /messages endpoint and collapses duplicate /anthropic/anthropic prefixes.
- Model remap: configured MODEL_MAP maps incoming model aliases (e.g., deepseek-chat) to local model names (e.g., qwen3-30b-a3b).
- Input validation: JSON parsing errors return structured 400 responses.
- Streaming: proxy now forwards Transfer-Encoding: chunked responses and streams upstream chunks to the client. Tunables above control chunk size and idle timeout.
- Large prompts: upstream model enforces context/token limits — large payloads may be rejected with 400. Trim prompts or increase upstream context config if supported.

Debugging checklist
1. Reproduce request locally with curl (use --data-binary @file to avoid shell quoting issues):
   curl -i -X POST "http://127.0.0.1:44777/anthropic/v1/chat/completions" -H 'Content-Type: application/json' --data-binary @payload.json
2. If response is 400 (Malformed JSON), run with ANTHROPIC_PROXY_DEBUG=1 and re-run to capture /tmp/anthropic_debug_<ts>_forward.json and _headers.json.
3. Check journal for remap/log lines (search for "remapped model" / "moved system-role" / "stripped cache_control").
4. For streaming issues, confirm upstream returned Transfer-Encoding: chunked and proxy forwarded chunks. Increase ANTHROPIC_PROXY_STREAM_TIMEOUT to 30s if client is slow.

Maintenance
- Keep runbook and tasks.yaml updated when making further proxy changes.
- After debugging, unset ANTHROPIC_PROXY_DEBUG and remove any debug dumps from /tmp.

Contact
- DevForge on-call: ops-team@example.com

