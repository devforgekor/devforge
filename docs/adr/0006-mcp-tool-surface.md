# MCP Tool Surface & Web Capture Ingestion Decision Record (ADR-0006)

## Status
Proposed (2026-09-14) — recommendation; not yet implemented.

## Context
`devforge-mcp` currently exposes 25+ tools, all loaded into the model's context up front.
Industry guidance is 10–20 active tools per server; selection accuracy degrades past
~30–50, and servers above ~15–20 tools should adopt **progressive discovery**
(`search_tools` / `defer_loading`). The agent framework in this system already collapses
upstream MCPs (search-proxy/exa/context7/fetch/github disabled) into `devforge-mcp`.

Separately, web-LLM capture (`chrome-web-llm`: extension + relay) is not wired into the
ingestion pipeline. The only batch-ingestion surface is the MCP `ingest` tool in the
legacy `scripts/mcp_server.py`; the refactored MCP has no `ingest`, and `POST /ingest`
is unimplemented. `turns.source` is `unknown` for all rows (no provenance).

Evidence: `docs/reports/industry-standard-comparison-20260914.md`.

## Decision
1. **Tool budget**: keep the always-on MCP surface to **10–20 tools**; group by namespace
   (knowledge / memory / pipeline / inference / actions / watchdog / deepdive). Expose the
   long tail via a **`search_tools` meta-tool** (progressive discovery). Tool descriptions
   must encode the decision boundary ("use when … / do NOT use when …").
2. **`ingest` contract**: provide batch conversation ingestion on **both** surfaces —
   `POST /api/v1/ingest` (HTTP) and the MCP `ingest` tool — in the refactored package.
3. **Web capture**: adopt **Chrome extension + Native Messaging + loopback relay** as the
   standard for capturing logged-in web-LLM sessions (not an isolated Playwright profile),
   and route captured conversations into `ingest`.
4. **Provenance**: every ingested turn records `source` (e.g. `chrome:qwen`, `chrome:deepseek`,
   `claude-code`, `opencode`) and `agent`.

## Consequences
- Agent tool-selection accuracy and context cost improve; servers stay composable.
- Adding `ingest`/`/ingest` unblocks web capture and the `chat-history` lineage.
- Enables `turns.source`-based filtering/attribution (currently impossible).
- Requires updating the refactored MCP adapter and the API driving adapter, plus a
  schema/provenance convention documented in `docs/specs/`.
