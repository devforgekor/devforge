# MCP Server — Design & Implementation Plan

**Version**: 2.0 | **Status**: DRAFT | **Language**: English (code), Korean (plan description)

---

## 2026 Status: SSE Deprecated → Streamable HTTP

2025-03-26 MCP spec부터 **SSE는 deprecated**. 2026년 표준은 **Streamable HTTP**:

| Transport | Status | Config |
|-----------|--------|--------|
| `stdio` | ✅ Supported (local) | `"type": "stdio"`, spawn subprocess |
| **Streamable HTTP** | ✅ **Recommended** (remote) | `"type": "http"`, single POST endpoint |
| `SSE` | ❌ **Deprecated** | Legacy, two-endpoint |

**Claude Code mcpServers**: `"type": "http"` + `"url": "http://host:port/mcp"`

Python `mcp` SDK 1.28.0 (`FastMCP`) → `transport="http"` native 지원.

---

## 1. Current Status

### 1.1 What Exists

| 항목 | 상태 | 상세 |
|------|------|------|
| `scripts/mcp_server.py` | ✅ 418 lines, experimental | FastAPI + **SSE** (구식), hand-rolled MCP protocol |
| Tools: `fact_search` | ✅ pgvector semantic search | embedder(`:8081`) 필요 |
| Tools: `mem_save` | ✅ conversations + turns INSERT | |
| Tools: `mem_search` | ✅ pg_trgm text search | |
| Port | ✅ 127.0.0.1:8000 | |
| Python 3.11 + `mcp` 1.28.0 | ✅ 신규 설치 | `FastMCP` 사용 가능 |

### 1.2 What's Missing (P0 priority)

| 항목 | 이유 |
|------|------|
| Streamable HTTP 마이그레이션 | SSE deprecated, 새 클라이언트와 호환 불가 |
| systemd service unit | 수동 실행만 가능 |
| Caddy reverse proxy | 외부 클라이언트 접근 불가 |
| Claude Code mcpServers 연동 | 현재 세션에서 tool 사용 불가 |

### 1.3 미구현 항목 (phases.md 기준)

```
Phase 1: POST /ingest (batch conversation storage)  — tools/call로 구현 가능
Phase 2: MCP mem_search hybrid upgrade              — Phase 2 과제
```

---

## 2. Proposed Architecture

```
                      ┌──────────────────────┐
                      │  Claude Code (MCP)    │
                      │  type: http           │
                      │  url: http://.../mcp  │
                      └──────────┬───────────┘
                                 │ POST /mcp (Streamable HTTP)
                      ┌──────────▼───────────┐
                      │   Caddy              │
                      │  handle_path /mcp/*   │
                      │   → 127.0.0.1:8000   │
                      └──────────┬───────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
   ┌──────────────────┐ ┌──────────────┐ ┌────────────────┐
   │  mcp_server.py   │ │ Embedder API │ │  PostgreSQL    │
   │  FastMCP + uvic  │ │ :8081        │ │  (psql_json)   │
   │  :8000 (python3. │ │ (optional)   │ │                │
   └──────────────────┘ └──────────────┘ └────────────────┘
```

---

## 3. Design

### 3.1 mcp_server.py 전면 재작성 (FastMCP)

기존 418줄 hand-rolled SSE → **~150줄 FastMCP**로 재작성.

```python
#!/usr/bin/env python3.11
# Status: experimental
# Path: systemd:devforge-mcp.service
"""DevForge MCP Server — Streamable HTTP (spec 2025-03-26).

Tools: fact_search, mem_save, mem_search, search_conversations,
       get_conversation, search_turns, get_turn_facts, ingest
"""

import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP
from lib.db import psql_json, psql_ok, esc_sql

mcp = FastMCP(name="devforge-mcp")

# ── Tools ──

@mcp.tool(name="fact_search")
async def fact_search(query: str, limit: int = 10, fact_type: str = None) -> str:
    """Semantic search on review_facts via pgvector."""
    # 기존 _tool_fact_search 로직 그대로,
    # httpx embedder call → pgvector query → return JSON string

@mcp.tool(name="mem_save")
async def mem_save(tag: str, summary: str, detail: str) -> str:
    """Save conversation memory."""
    # 기존 _tool_mem_save 로직

@mcp.tool(name="mem_search")
async def mem_search(query: str, tag: str = None) -> str:
    """Search memories via pg_trgm."""
    # 기존 _tool_mem_search 로직

@mcp.tool(name="search_conversations")
async def search_conversations(source: str = None, model: str = None) -> str:
    """List/search conversations."""

@mcp.tool(name="get_conversation")
async def get_conversation(conversation_id: str) -> str:
    """Get full conversation thread."""

@mcp.tool(name="search_turns")
async def search_turns(keyword: str = None, agent: str = None,
                       pipeline_state: str = None, limit: int = 20) -> str:
    """Search turns with filters."""

@mcp.tool(name="get_turn_facts")
async def get_turn_facts(turn_id: str) -> str:
    """Get all facts for a turn."""

@mcp.tool(name="ingest")
async def ingest(conversation_json: str) -> str:
    """Batch store conversation + turns."""

if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8000, path="/mcp")
```

**핵심 변경**:
- `FastMCP.tool()` decorator → 자동 JSON Schema 생성, protocol handling 내장
- `transport="http"` → Streamable HTTP (단일 `/mcp` endpoint)
- Session 관리/lifecycle 자동 처리
- `/health` endpoint는 FastMCP 내장

### 3.2 Systemd Service Unit

```ini
[Unit]
Description=DevForge MCP server — Streamable HTTP (FastMCP)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3.11 /opt/projects/server/scripts/mcp_server.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

### 3.3 Caddy Route

```caddy
handle_path /mcp/* {
    reverse_proxy 127.0.0.1:8000
}
```

양 domain block (nip.io + duckdns.org)에 각각 추가.

### 3.4 Claude Code mcpServers

```json
{
  "mcpServers": {
    "devforge-mcp": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

`"type": "http"` = Streamable HTTP transport (SSE의 `"type": "sse"`와 다름).

### 3.5 Tool List

| Tool | Backend | Data | Phase |
|------|---------|------|-------|
| `fact_search` | review_facts + embeddings pgvector | 10 facts | 0 (migration) |
| `mem_save` | conversations + turns INSERT | — | 0 |
| `mem_search` | turns pg_trgm | 5,698 turns | 0 |
| `search_conversations` | conversations | 255 rows | 1 (+추가) |
| `get_conversation` | conversations + turns | — | 1 |
| `search_turns` | turns (agent/type/state filter) | 5,698 | 1 |
| `get_turn_facts` | review_facts by turn_id | 10 | 1 |
| `ingest` | conversations + turns INSERT | — | 1 |

---

## 4. Implementation Order

### Step 0: mcp_server.py 재작성 (FastMCP) — 1시간
- 기존 `mcp_server.py`를 `mcp_server_sse.py`로 rename (backup)
- `FastMCP` 기반 mcp_server.py 새로 작성
- 기존 3 tools (fact_search, mem_save, mem_search) 로직 이식
- 검증: `python3.11 mcp_server.py` → curl 테스트

### Step 1: systemd + Caddy — 30분
- systemd unit 생성, enable --now
- Caddyfile 수정, reload
- 검증: curl, journalctl

### Step 2: Claude Code mcpServers — 10분
- .claude/mcp.json 생성
- "MCP tool이 뭐 있지?" → 자동 tools/list 확인

### Step 3: Tool 확장 — 30분
- search_conversations, get_conversation, search_turns, get_turn_facts, ingest 추가

---

## 5. Files

| File | Action | Lines |
|------|--------|-------|
| `scripts/mcp_server.py` | **REWRITE** | 418 → ~150 (FastMCP) |
| `scripts/mcp_server_sse.py` | RENAME (backup) | — |
| `~/.config/systemd/user/devforge-mcp.service` | CREATE | ~15 |
| `/etc/caddy/Caddyfile` | MODIFY | +6 |
| `~/.claude/mcp.json` | CREATE | ~8 |

---

## 6. Verification

```bash
# FastMCP run 확인
python3.11 -c "from mcp.server.fastmcp import FastMCP; print('ok')"

# 서비스 시작
systemctl --user daemon-reload
systemctl --user enable --now devforge-mcp.service

# health check (FastMCP 내장)
curl http://127.0.0.1:8000/health  # 또는 FastMCP 기본 endpoint

# Streamable HTTP: POST /mcp with initialize
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'

# tools/list
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'

# Caddy external
curl https://devforge.152-69-229-246.nip.io/mcp -X POST ...

# Claude Code에서 "review_facts 검색해줘" → tool call 확인
```

---

## 7. Risks

| Risk | Mitigation |
|------|------------|
| SDK 호환성 (Python 3.9 → 3.11) | `mcp_server.py`는 `#!/usr/bin/env python3.11`로 실행 |
| embedder(:8081) unavailable | fact_search에 fallback message 내장 |
| Tool 확장 시 비대화 | 각 tool당 10-20 lines, ~200 lines면 충분 |
