# DevForge — 개인용 AI 대화 기록 시스템

**상태**: Phase 1 설계 완료, 구현 대기  
**날짜**: 2026-05-13  
**서버**: OCI ARM (devforge), Podman/Quadlet, PostgreSQL 16

---

## 아키텍처

```
Mac (로컬)
├─ Claude Code CLI ──(MCP SSE)──→ mem_save / mem_search
├─ Copilot CLI ──(주기적 sync)──→ POST /ingest
└─ Gemini CLI  ──(주기적 sync)──→ POST /ingest

서버 (devforge, OCI ARM)
┌─────────────────────────────────────────┐
│ ai-pod (10.89.0.8)                      │
│ ├─ litellm         :4000               │
│ ├─ devforge-llm    :8080 (Qwen 7B)     │
│ └─ devforge-api    :8000 ← 🆕          │
│    ├─ MCP SSE:  /mcp/sse, /mcp/messages│
│    ├─ Ingest:   POST /ingest           │
│    └─ CLI:      podman exec → 검색      │
│         │                               │
├─ data-pod (10.89.0.9)                   │
│ └─ postgres :5432                       │
│    └─ devforge_app                      │
│       ├─ conversations                  │
│       └─ turns (user/reasoning/assistant)│
│                                         │
├─ Caddy (root, host network)             │
│  ├─ /api/*       → litellm:4000        │
│  └─ /devforge/*  → localhost:8000 🆕   │
└─────────────────────────────────────────┘
```

---

## DB 스키마

```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT,
    source TEXT NOT NULL,       -- claude, copilot, gemini
    model TEXT,                 -- claude-sonnet-4-6 등
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE turns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID REFERENCES conversations(id),
    seq INT NOT NULL,
    user_query TEXT NOT NULL,
    reasoning TEXT,
    assistant_answer TEXT NOT NULL,
    meta JSONB DEFAULT '{}',           -- {tokens, latency, source, model, ...}
    wing TEXT,                         -- MemPalace 분류 (Phase 2)
    room TEXT,                         -- MemPalace 분류 (Phase 2)
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_turns_conversation ON turns(conversation_id, seq);
CREATE INDEX idx_turns_created ON turns(created_at DESC);
CREATE INDEX idx_conversations_source ON conversations(source);
```

---

## 파일 구조 (6개)

```
/opt/workspace/devforge/
├── DESIGN.md              ← 이 파일
├── Dockerfile
├── requirements.txt
├── app/
│   ├── mcp_server.py      ← MCP SSE 서버 (Claude Code 연동)
│   ├── ingest.py          ← POST /ingest (주기적 sync + Chrome Ext)
│   ├── db.py              ← PostgreSQL 연결 + 스키마 초기화
│   └── search.py          ← 검색 로직
└── cli.py                 ← CLI 도구 (SSH 접속용)
```

---

## MCP 도구

| Tool | 입력 | 동작 |
|------|------|------|
| `mem_save` | tag, summary, detail | conversations+turns 저장 |
| `mem_search` | query, tag(optional) | ILIKE 검색, JSON 반환 |

---

## 배포 (Quadlet)

```
~/.config/containers/systemd/
├── container-devforge-api.container  ← 신규
└── pod-ai-pod.pod                    ← PublishPort=8000:8000 추가
```

Caddyfile:
```
handle_path /devforge/* {
    reverse_proxy localhost:8000
}
```

---

## 결정 기록

- pgvector: Phase 2로 연기 (Phase 1은 ILIKE+FTS)
- AI 추론: 서버에서 하지 않음. Mac CLI가 담당
- MemPalace wing/room: 컬럼만 준비, 분류 로직은 Phase 2
- Seedling 공통 코어: DevForge에서 검증 후 seedling에 이식
- Docker 호환: Dockerfile은 Podman/Docker 공통 사용 가능하게 작성
