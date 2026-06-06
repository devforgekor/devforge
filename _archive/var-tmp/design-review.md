# �� AI 대화 기록 중앙화 시스템 설계 검토 (v1.0)

**검토 일시**: 2026-05-13  
**검토자 관점**: 구현 가능성, 현재 seedling과의 통합성, 운영 관점

---

## ✅ 강점 (칭찬할 점)

### 1. 아키텍처 설계
| 항목 | 평가 | 이유 |
|---|---|---|
| MCP 기반 | ⭐⭐⭐⭐⭐ | 표준화된 인터페이스 (Claude, Copilot 즉시 연동) |
| 다중 저장 방식 | ⭐⭐⭐⭐⭐ | 로컬 SQLite(오프라인) + PostgreSQL(중앙) = 현실적 |
| 의사결정 추적 | ⭐⭐⭐⭐⭐ | "왜?"에 대한 명시적 기록 구조 |
| 기억의 궁전 모델 | ⭐⭐⭐⭐ | 직관적 계층구조 (Wing > Room > Conversation) |

### 2. DB 설계
| 항목 | 평가 | 이유 |
|---|---|---|
| 정규화 수준 | ⭐⭐⭐⭐ | 1:N 관계 명확, CASCADE 설정 적절 |
| pgvector 활용 | ⭐⭐⭐⭐⭐ | 의미 검색 + 의사결정 검색 분리 |
| 확장성 | ⭐⭐⭐⭐ | 1M 벡터 규모 대응 가능 |

### 3. 클라이언트 전략
| 항목 | 평가 |
|---|---|
| 데스크톱 (MCP 우선) | ✅ 올바른 선택 |
| 로컬 동기화 에이전트 | ✅ 네트워크 단절 대비 좋음 |
| 모바일 검색 UI | ✅ 현실적 (대화 저장은 추후) |

---

## ⚠️ 위험 요소 (해결 필요)

### 1. **MCP 구현의 복잡성** (CRITICAL)

**현재 문제**:
```json
"command": "curl",
"args": ["-X", "POST", "..."]  // ← 이건 작동 안 함!
```

**왜?** 
- MCP는 `stdio` 기반 프로토콜 (HTTP REST가 아님)
- 표준화된 MCP 서버 라이브러리를 사용해야 함

**권장 수정**:
```python
# mcp_server.py (Python MCP 표준 라이브러리 사용)
from mcp.server import Server
from mcp.types import Tool

server = Server("my-memory")

@server.call_tool()
async def mem_save(name: str, arguments: dict):
    # content, user_id, tag 등 받음
    async with asyncpg.create_pool(...) as pool:
        async with pool.acquire() as conn:
            # PostgreSQL에 저장
    return {"success": True}

@server.call_tool()
async def mem_search(name: str, arguments: dict):
    # query, search_type 받음
    # pgvector 검색 수행
    return {"results": [...]}
```

**FastAPI는 MCP 통신용이 아니라 REST API용 분리**:
```
MCP Server (stdio, Claude Code와 통신)
   ↓
PostgreSQL
   ↓
FastAPI (모바일/웹 검색 UI용 REST API)
```

---

### 2. **임베딩 생성 비용 (IMPORTANT)**

**현재 문제**:
- 모든 메시지마다 임베딩 생성하면 비용 폭증
- `text-embedding-3-small`: $0.02 per 1M tokens
  - 평균 메시지 150토큰 = 대화 1000건당 약 $0.02
  - 1년 30,000건 대화 = $0.60 (합리적)
  - **하지만 모든 메시지 vs 중요 메시지 선별이 핵심**

**권장**:
```python
# 임베딩 생성 선별 전략
# 1. 의사결정 기록만 무조건 임베딩
# 2. 일반 메시지는 100개마다 1개만 임베딩
# 3. 사용자가 "기억해" 명시할 때만 즉시 임베딩

if msg.tag == "decision":
    embedding = await create_embedding(msg.content)  # 무조건
elif msg_count % 100 == 0:
    embedding = await create_embedding(msg.content)  # 100개마다 1개
else:
    embedding = None  # 건너뛰기
```

---

### 3. **모바일 대화 저장의 현실성 (HIGH)**

**현재 문제**:
- iOS: 웹뷰 앱 자체 개발 필요 (App Store 심사 필요)
- Android: 브라우저 확장 지원이 2024년에 겨우 시작됨
- **실제 구현하려면 3~6개월 소요**

**현실적 대안**:
```
Phase 1 (즉시): 모바일 검색만 (중요함)
  → FastAPI 검색 UI를 모바일 반응형으로 구축
  → "이전에 Kafka 사용한 이유가 뭐였지?" 검색 가능

Phase 2 (2~3개월): 수동 저장
  → iOS 단축어(Shortcuts)로 대화 복사 → `/api/conversations` POST
  → Android: 같은 방식

Phase 3 (추후): 자동 캡처
  → MCP 모바일 클라이언트 등장 후
```

**수정 권고**:
```markdown
## 6. 📱 모바일 환경 대응 (현실적 로드맵)

### 6.1 Phase 1 (즉시, 2~4주): 검색만
- React 대시보드 반응형 구축 + 모바일 접속
- PWA화 (Progressive Web App) → 앱처럼 홈화면에 설치 가능

### 6.2 Phase 2 (1~2개월): 수동 저장
- iOS 단축어 + Android 수동 업로드 UI

### 6.3 Phase 3 (추후): 자동 캡처
- 웹뷰 앱 개발 또는 MCP 모바일 클라이언트 사용
```

---

### 4. **seedling과의 통합 명확화 (MEDIUM)**

**현재 문제**:
- 이 시스템이 seedling의 **연장인지, 독립 시스템인지** 불명확
- 기존 seedling의 `InteractionLogs_fts`와의 관계 정의 필요

**권고**:
```markdown
## 9. 🔗 기존 seedling 시스템과의 관계

### 9.1 분리 vs 통합 결정
옵션 A (권장): 완전 분리
  - 새 시스템: "AI 대화 기록 저장소" (이 문서)
  - 기존 seedling: "QDP + RAG 파이프라인" (유지)
  - 연동: seedling의 Claude Code가 MCP로 새 시스템의 기억 활용

옵션 B: 통합
  - 기존 seedling.db (PostgreSQL)에 conversations, messages, decisions 테이블 추가
  - 동일 DB 사용으로 중복 제거
  - 복잡도 증가

### 9.2 선택 근거
옵션 A 선택 이유:
  1. 독립성: 시스템 개발/운영 분리 용이
  2. 성능: 각각 최적화 가능
  3. 백업: 독립적 복구 가능
  4. 마이크로서비스: 앞으로의 확장 고려
```

---

### 5. **보안 고려사항 미흡 (HIGH)**

**현재 문제**:
- API 키/JWT 인증 "추가 필요"만 명시
- 실제 구현 없음

**권고 추가**:
```python
# FastAPI 보안 설정
from fastapi.security import HTTPBearer, HTTPAuthCredentialDetails
from jose import JWTError, jwt

security = HTTPBearer()

async def verify_token(credentials: HTTPAuthCredentialDetails = Depends(security)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401)
    except JWTError:
        raise HTTPException(status_code=401)
    return user_id

@app.post("/mcp/memory/search")
async def mcp_search(req: MemorySearch, user_id: str = Depends(verify_token)):
    # user_id로 필터링된 데이터만 반환
    ...
```

**추가 권고**:
- MCP 통신: TLS 필수 (HTTPS)
- API 레이트 제한: 시간당 1000 요청 등
- 감시 로깅: 모든 API 호출 기록

---

### 6. **로컬 SQLite 스키마 미정의 (MEDIUM)**

**현재 문제**:
```python
conn = sqlite3.connect("local_memory.db")
cursor = conn.execute("SELECT * FROM memories WHERE synced = 0")
```
- 스키마 정의 없음
- PostgreSQL과의 맵핑 불명확

**권고**:
```sql
-- local_memory.db 스키마
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    tag TEXT CHECK (tag IN ('decision', 'fact', 'preference', 'note')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    synced BOOLEAN DEFAULT 0,
    server_id TEXT  -- 서버에 저장된 후 이곳에 UUID 저장
);

CREATE INDEX IF NOT EXISTS idx_synced ON memories(synced);
```

---

## 🔧 권장 수정 사항 (우선순위)

| 순서 | 항목 | 난이도 | 영향 |
|---|---|---|---|
| 1 | MCP 서버 구현 재정의 (stdio 기반) | 중간 | CRITICAL |
| 2 | 모바일 대응 로드맵 현실화 | 낮음 | HIGH |
| 3 | 임베딩 비용 최적화 전략 추가 | 낮음 | MEDIUM |
| 4 | seedling과의 명확한 관계 정의 | 낮음 | MEDIUM |
| 5 | 보안 구체적 구현 추가 | 중간 | HIGH |
| 6 | 로컬 SQLite 스키마 정의 | 낮음 | LOW |

---

## ✨ 최종 평가

| 항목 | 점수 | 코멘트 |
|---|---|---|
| **개념 타당성** | 9/10 | 의사결정 추적 + MCP + 다중 디바이스 = 우수한 설계 |
| **기술 적용** | 7/10 | MCP 구현 재정의 + 모바일 현실화 필요 |
| **구현 난이도** | 중간 | FastAPI + PostgreSQL은 표준, MCP는 학습곡선 있음 |
| **운영 복잡도** | 중간 | 중앙 서버 유지보수 필요, 하지만 가치가 높음 |

**결론**:
✅ **진행 가능한 시스템입니다.**
- Phase 1 (검색 UI + MCP 기본): 4~6주
- Phase 2 (모바일 수동 저장): 2~3주
- Phase 3 (자동 캡처): 추후 (현재 모바일 MCP 성숙도 낮음)

**우선 Phase 1부터 시작 권고.**

