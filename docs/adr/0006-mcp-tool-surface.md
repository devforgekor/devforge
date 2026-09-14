# MCP Tool Surface & Web Capture Ingestion Decision Record (ADR-0006)

## Status
Accepted (2026-09-14) — 12-tool contract enumerated per rf-triage-07; effective at cutover.

> 이전 상태: Proposed (2026-09-14). rf-triage-05에서 "12툴 목록 미열거" 판정 → rf-triage-07에서 수용.
> 본 ADR의 12툴 목록은 `specs/mcp-contract.json`과 일치. 이후 변경은 supersede 신규 ADR.

## Context
There are only **4 active MCP servers** (`devforge-mcp`, `yggdrasil`, `lsp`, `opencode-db`).
Servers *expose* many tools (devforge-mcp **25**, agent-lsp **65**), **but opencode prunes the
loaded surface via its `tools` allowlist**: enabled = **devforge 12, lsp 13, yggdrasil 4**
(+ `opencode-db` unfiltered) ≈ **33 loaded tools**. So each server is already within the
10–20 guidance and **tool sprawl is not a current problem for opencode**.

The real risks are: (a) clients **without** an allowlist (e.g. Claude Code) may load 25/65 as-is;
(b) the refactored MCP (`adapters/driving/mcp`) exposes only **5 tools** and must **preserve the
allowed 12-tool `devforge-mcp` contract** to avoid feature loss at cutover; (c) `ingest` is missing.

Separately, web-LLM capture (`chrome-web-llm`: extension + relay) is not wired into the
ingestion pipeline. The only batch-ingestion surface is the MCP `ingest` tool in the
legacy `scripts/mcp_server.py`; the refactored MCP has no `ingest`, and `POST /ingest`
is unimplemented. `turns.source` is `unknown` for all rows (no provenance).

Evidence: `docs/reports/industry-standard-comparison-20260914.md`,
`docs/reports/mcp-tool-audit-20260914.md` (30-day usage: 9 of 33 loaded tools had 0 calls;
target 33→~16 via remove+merge).

## Decision
1. **Preserve the contract (not expand)**: the refactored MCP must reproduce the
   opencode-allowed `devforge-mcp` tool set (**12**) with required namespaces; do **not**
   expose the full 25 by default. Adopt **progressive discovery** (`search_tools`) only when
   a client loads the full catalog (no allowlist) — optional, not urgent. Tool descriptions
   must encode the decision boundary ("use when … / do NOT use when …").

   **계약 툴 12개 (Enumerated Contract)** — `specs/mcp-contract.json`과 일치:

   | # | 툴 | disposition | lifecycle | merge_target |
   |---|---|---|---|---|
   | 1 | `deepdive_step_enter` | hook | active → hook@M1 → removed@M0 | — |
   | 2 | `deepdive_step_exit` | hook | active → hook@M1 → removed@M0 | — |
   | 3 | `deepdive_session_heartbeat` | internalize | active → deprecated@M1 → removed@M0 | — |
   | 4 | `deepdive_session_status` | internalize | active → deprecated@M1 → removed@M0 | — |
   | 5 | `deepdive_verify_sandbox` | keep | active | — |
   | 6 | `obs_write` | merge | active → merged@M3(M2) → removed@M0 | memory(action=save,kind=obs) |
   | 7 | `obs_search` | merge | active → merged@M3 → removed@M0 | memory(action=search,kind=obs) |
   | 8 | `search_turns` | merge | active → merged@M3 → removed@M0 | search_turns(mode=fts) |
   | 9 | `search_similarity` | merge | active → merged@M3 → removed@M0 | search_turns(mode=hybrid) |
   | 10 | `mem_save` | merge | active → merged@M3 → removed@M0 | memory(action=save,kind=mem) |
   | 11 | `mem_search` | merge | active → merged@M3 → removed@M0 | memory(action=search,kind=mem) |
   | 12 | `get_conversation` | keep | active | — |

   검증: `specs/mcp-contract.json` JSON Schema 검증 + `opencode.json` allowlist 이름 diff=0 + annotation 4종(`readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint`) 외 필드 금지.
2. **`ingest` contract**: provide batch conversation ingestion on **both** surfaces —
   `POST /api/v1/ingest` (HTTP) and the MCP `ingest` tool — in the refactored package.
3. **Web capture**: adopt **Chrome extension + Native Messaging + loopback relay** as the
   standard for capturing logged-in web-LLM sessions (not an isolated Playwright profile),
   and route captured conversations into `ingest`.
4. **Provenance**: every ingested turn records `source` (e.g. `chrome:qwen`, `chrome:deepseek`,
   `claude-code`, `opencode`) and `agent`.

## Consequences
- Cutover preserves the exact tool contract agents already use; no client surprises.
- Adding `ingest`/`/ingest` unblocks web capture and the `chat-history` lineage.
- Enables `turns.source`-based filtering/attribution (currently impossible).
- Full-catalog clients (no allowlist) remain a future progressive-discovery candidate.
- Requires updating the refactored MCP adapter and the API driving adapter, plus a
  schema/provenance convention documented in `docs/specs/`.
