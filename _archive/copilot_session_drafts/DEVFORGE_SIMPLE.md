# DevForge: 개인용 AI 지식 창고

**상황**: 1인 개발, 4-6주, $8/mo 예산

---

## 아키텍처 (진짜 간단)

```
Chrome Extension (수집)
    ↓
FastAPI 서버 (저장 + 검색)
    ↓
PostgreSQL + pgvector (저장소)
    ↓
CLI / 웹 UI / Claude Code (조회)
```

---

## Phase 1 (4주)

### Week 1-2: 백엔드
- [ ] PostgreSQL 로컬 (docker-compose)
- [ ] FastAPI 기본 구조
- [ ] MCP 엔드포인트 (`mem_save`, `mem_search`)
- [ ] 기본 스키마 (conversations, messages)

### Week 3: 통합 테스트
- [ ] Claude Code에서 MCP 호출 확인
- [ ] Chrome Ext → FastAPI 연동

### Week 4: UI
- [ ] 간단한 웹 검색 화면
- [ ] CLI 명령어 (`devforge search`)

---

## 버전 관리 (진짜 최소)

### 5가지만 지키기
1. `requirements.txt`: 모든 버전 `==` (예: `fastapi==0.104.1`)
2. `git`: `poetry.lock` 또는 `package-lock.json` 커밋
3. **GitHub Watch**: 중요 저장소 → Releases 알림
4. **Dependabot**: `.github/dependabot.yml` (월간)
5. **테스트**: `docker compose up` → 5분 동작 확인

### 버린 것
- ❌ Staging 서버 (로컬 테스트만)
- ❌ Release Monitor 자체 호스팅
- ❌ Private Registry
- ❌ 변경 로그 상세 분석

**월간 루틴**: 첫째 주 금요일 30분
- Dependabot PR 확인
- 로컬에서 docker compose up 테스트
- 병합

---

## 파일 구조

```
devforge/
├── .env                     # 비밀값 (git 제외!)
├── .gitignore
├── docker-compose.yml
├── requirements.txt
├── Dockerfile
├── .github/dependabot.yml
├── scripts/init.sql         # DB 초기 스키마
└── app/
    ├── main.py              # FastAPI REST API
    └── mcp_server.py        # MCP stdio 서버 (Claude Code용)
```

---

## 즉시 시작 (Day 1)

```bash
# 1. 4개 파일 생성 (아래 템플릿 사용)
# 2. docker compose up
# 3. curl http://localhost:8000/health
# → "status":"ok" 나오면 OK!
```

---

## 템플릿 (Copy-Paste)

### .gitignore
```
.env
__pycache__/
*.pyc
pg_data/
*.log
```

### .env
```
DB_PASSWORD=devpass
OPENAI_API_KEY=sk-...
```

### docker-compose.yml
```yaml
version: '3.9'
services:
  postgres:
    image: pgvector/pgvector:0.5.1-pg15
    environment:
      POSTGRES_DB: devforge
      POSTGRES_USER: dev
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    ports:
      - "5432:5432"
    volumes:
      - pg_data:/var/lib/postgresql/data
      - ./scripts/init.sql:/docker-entrypoint-initdb.d/init.sql

  api:
    build: .
    ports:
      - "8000:8000"
    environment:
      DATABASE_URL: postgresql://dev:${DB_PASSWORD}@postgres:5432/devforge
      OPENAI_API_KEY: ${OPENAI_API_KEY}
    depends_on:
      - postgres
    command: uvicorn app.main:app --host 0.0.0.0 --reload

volumes:
  pg_data:
```

### requirements.txt
```
fastapi==0.104.1
uvicorn[standard]==0.24.0
asyncpg==0.29.0
openai==1.3.5
python-dotenv==1.0.0
pydantic==2.5.0
mcp==0.9.1
```

### Dockerfile
```dockerfile
FROM python:3.12.3-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### .github/dependabot.yml
```yaml
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "monthly"
    open-pull-requests-limit: 3
```

### app/main.py
```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import asyncpg
from contextlib import asynccontextmanager
import os

db_pool = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    db_pool = await asyncpg.create_pool(
        os.environ["DATABASE_URL"], min_size=2, max_size=10
    )
    yield
    await db_pool.close()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 운영 시 도메인으로 제한
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/api/conversations")
async def save(data: dict):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO conversations (summary) VALUES ($1) RETURNING id",
            data.get("summary")
        )
    return {"id": str(row["id"])}

@app.get("/api/search")
async def search(q: str):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, summary, created_at FROM conversations WHERE summary ILIKE $1 LIMIT 10",
            f"%{q}%"
        )
    return {"results": [dict(r) for r in rows]}
```

### app/mcp_server.py (Claude Code 연동 - stdio 방식)
```python
"""
Claude Code MCP 서버. REST API와 별도로 실행.
사용: python app/mcp_server.py

Claude Desktop config (~/.claude/claude_desktop_config.json):
{
  "mcpServers": {
    "devforge": {
      "command": "python",
      "args": ["/path/to/devforge/app/mcp_server.py"]
    }
  }
}
"""
import asyncio
import asyncpg
import os
import json
import sys

DB_URL = os.environ.get("DATABASE_URL", "postgresql://dev:devpass@localhost:5432/devforge")

async def get_db():
    return await asyncpg.connect(DB_URL)

async def mem_save(summary: str) -> str:
    conn = await get_db()
    try:
        row = await conn.fetchrow(
            "INSERT INTO conversations (summary) VALUES ($1) RETURNING id",
            summary
        )
        return f"saved: {row['id']}"
    finally:
        await conn.close()

async def mem_search(query: str) -> list:
    conn = await get_db()
    try:
        rows = await conn.fetch(
            "SELECT summary, created_at FROM conversations WHERE summary ILIKE $1 LIMIT 5",
            f"%{query}%"
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()

# MCP stdio 루프
async def main():
    for line in sys.stdin:
        req = json.loads(line)
        method = req.get("method")
        params = req.get("params", {})

        if method == "tools/list":
            result = {"tools": [
                {"name": "mem_save", "description": "대화 저장", "inputSchema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}},
                {"name": "mem_search", "description": "대화 검색", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}
            ]}
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "mem_save":
                r = await mem_save(args["summary"])
                result = {"content": [{"type": "text", "text": r}]}
            elif name == "mem_search":
                r = await mem_search(args["query"])
                result = {"content": [{"type": "text", "text": json.dumps(r, ensure_ascii=False, default=str)}]}
            else:
                result = {"error": "unknown tool"}
        else:
            result = {}

        sys.stdout.write(json.dumps({"id": req.get("id"), "result": result}) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    asyncio.run(main())

---

## DB 초기화 (scripts/init.sql)

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    summary TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID REFERENCES conversations(id),
    role TEXT CHECK (role IN ('user', 'assistant')),
    content TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 의사결정 추적 ("왜 그런 선택을 했는가")
CREATE TABLE decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID REFERENCES conversations(id),
    question TEXT NOT NULL,
    decision TEXT NOT NULL,
    rationale TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ON conversations (created_at DESC);
CREATE INDEX ON decisions (created_at DESC);
```

---

## 다음 마일스톤

| 주차 | 목표 | 확인 |
|------|------|------|
| 1-2 | FastAPI + DB 동작 | `curl http://localhost:8000/health` 200 OK |
| 3 | Claude Code 연동 | Claude에서 `@memory` 도구 호출 가능 |
| 4 | 웹 UI 완성 | `/search?q=...` 페이지 동작 |

---

## 핵심
- 필요한 것: 5가지 버전 관리 규칙
- 버린 것: 모든 "enterprise 과정"
- 목표: 4주 안에 동작하는 MCP 서버 1개

**시작하자!** 🚀
