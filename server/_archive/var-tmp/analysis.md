# 🔍 종합 검토: Seedling 기존 대화 캡처 + 새로운 AI 대화 중앙화 시스템

## 1️⃣ 현재 Seedling 상태 (이미 보유한 것)

### ✅ Chrome Extension v0.4.8 (이미 완성)
- **플랫폼**: ChatGPT, Claude, Gemini, DeepSeek, AiStudio
- **방식**: 브라우저 DOM 파싱 + JavaScript 주입
- **저장**: `/plugin/ingest` 엔드포인트 (http://localhost:8000)
- **기능**:
  - 사용자 + AI 답변 자동 감지
  - 30초 디바운싱 (중복 방지)
  - 튜턴 시퀀스 추적
  - 메타데이터: URL, title, platform, model, captured_at

### ✅ Chat Routes (이미 완성)
- `routes_chat.py`: 대화 저장 로직 완성
- `token_export.py`: 토큰 내보내기
- `feedback.py`: 사용자 반응 추적
- **저장소**: SQLite (분산 저장, 사용자별 DB 분리)

### ✅ Metadata 시스템 (부분 구현)
- `feedback.py`: 반응 감지 (Best/Good/Bad/Worst)
- `sentiment` 매핑: 10/7/4/1 스케일
- **문제**: 메타데이터가 경로별로 산재 (metadata_architecture_v1.md 참고)

### 🔴 부재한 것
- **중앙 DB**: PostgreSQL + pgvector 없음 (로컬 SQLite만 있음)
- **다중 디바이스 동기화**: 로컬 기반, 네트워크 버퍼 없음
- **결정 추적**: 의사결정 특화 저장/검색 없음
- **모바일 앱**: 웹뷰 앱 없음

---

## 2️⃣ 모바일 앱 리서치 문서 검토

### 결론: Kelivo가 정답이지만...
| 항목 | 현재 | 제안 |
|------|------|------|
| **웹뷰 기반** | sillyChat, aratheunseen/ChatGPT 예시 있음 | Flutter + `flutter_inappwebview` |
| **MCP 지원** | Kelivo 보유 | 새 시스템에서 도입 권장 |
| **데이터 백업** | Kelivo 보유 | 중앙 서버 필요 |

### 오픈소스 활용 전략
1. **Kelivo**: MCP 도구 확장 → 사용자 서버 API 연동 가능
2. **sillyChat**: 채팅 저장 로직 참고 → 사용자 서버로 전송
3. **flutter_inappwebview**: JS 주입 + 대화 감지 구현

---

## 3️⃣ 아키텍처 결정: 분리 vs 통합?

### 현재 설계 (분리 구조) ✅ 강력 권장
```
┌─────────────────────────────────────────┐
│  Seedling (기존 로컬 knowledge 시스템)    │
│  - SQLite (사용자별)                      │
│  - Chrome Extension (브라우저 캡처)       │
│  - 채팅 라우트 (Chat API)                 │
└───────────┬─────────────────────────────┘
            │ MCP Client
            │ (Memory Save/Search 호출)
            ▼
┌─────────────────────────────────────────┐
│  새 중앙화 시스템 (AI 결정 기록)         │
│  - PostgreSQL + pgvector (중앙)          │
│  - FastAPI (MCP 엔드포인트)              │
│  - 반응형 검색 UI (웹)                    │
│  - 로컬 SQLite 버퍼 (오프라인)            │
└─────────────────────────────────────────┘
```

### 왜 분리인가?
1. **독립적 진화**: 각 시스템의 요구사항이 다름
2. **Seedling 안정성**: 기존 기능 영향 없음
3. **마이크로서비스 준비**: 나중에 도커로 분리 가능
4. **백업/복구**: 독립적으로 관리 가능

---

## 4️⃣ 오전 작업 흔적 정리

### 발견된 것
1. **_pending/metadata_architecture_v1.md** (2026-05-06)
   - `wrap_llm_result()`: 메타데이터 추출 통합
   - `normalize_confidence()`: 신뢰도 스케일 통일
   - 추후 확장 계획

2. **_pending/phase2_plan.md**, **phase3_plan.md**
   - 이전 단계 계획 (현재는 사용 안 함)

3. **worklog.json** 최신 3개 항목
   - 2026-05-13: DevForge 자동화 완성 ✅
   - 2026-05-13: reference-tracking-policy v1.2.1 ✅
   - 2026-05-13 10:45: DevForge 최신 상태 동기화 ✅

### 오전에 한 일 (현 세션)
- ✅ DevForge 자동화 완성
- ✅ reference-tracking 정책 완성
- ❌ 모바일 앱은 아직 설계만 (구현 X)

---

## 5️⃣ 새 시스템 설계의 6가지 핵심 개선점

### 1. MCP 아키텍처 수정 ✅
```python
# ❌ 현재 (HTTP 기반 잘못된 설계)
curl -X POST https://server.com/mcp/memory/save

# ✅ 올바른 것 (stdio 기반)
class MemoryServer(mcp.server.Server):
    @mcp.server.tool()
    async def mem_save(self, user_id: str, tag: str, content: str):
        """대화 내용을 중앙 서버에 저장"""
        async with aiohttp.ClientSession() as session:
            await session.post("https://my-server.com/api/memory",
                json={"user_id": user_id, "tag": tag, "content": content})
```

### 2. 임베딩 비용 최적화 ✅
- **Tier 1** (항상 임베딩): 의사결정만 → $0.20/년
- **Tier 2** (샘플링): 일반 메시지 1/100 → $0.40/년
- **Tier 3** (사용자 선택): "Remember" 버튼 → 필요할 때만
- **합계**: $0.60-2/년 (vs $600 무최적화)

### 3. 모바일 현실적 로드맵 ✅
| 단계 | 방식 | 소요 시간 |
|------|------|---------|
| Phase 0 | 웹 검색 UI (반응형) | 2-4주 |
| Phase 1 | iOS Shortcuts + Android 폼 (수동) | 1-2주 |
| Phase 2 | 모바일 MCP 클라이언트 (미래) | TBD |
| Phase 3 | Flutter WebView 앱 (6+ 개월) | 미예정 |

### 4. 보안 구현 추가 ✅
```python
# JWT 토큰 검증
from fastapi_jwt_auth import AuthJWT

@app.post("/api/memory/save")
async def save_memory(req: MemorySave, authorize: AuthJWT = Depends()):
    authorize.jwt_required()  # 토큰 검증
    user_id = authorize.get_jwt_subject()
    # ... 사용자별 데이터 격리
```

### 5. Seedling 통합 전략 ✅
```
Seedling의 routes_plugin.py가 이미 있음:
  POST /plugin/ingest → PluginIngest 모델

새 시스템은:
  POST /api/memory/save → MemorySave 모델
  (독립적, 다른 엔드포인트)

향후 통합:
  routes_plugin.py의 ingest 로직을 MCP 도구로 래핑
```

### 6. 로컬 SQLite 스키마 정의 ✅
```sql
CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    tag TEXT CHECK (tag IN ('decision', 'fact', 'preference', 'note')),
    embedding_status TEXT CHECK (embedding_status IN ('pending', 'embedded', 'skipped')),
    synced BOOLEAN DEFAULT 0,
    server_id TEXT UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_synced ON memories(synced);
CREATE INDEX idx_server_id ON memories(server_id);
```

---

## 6️⃣ 최종 권고

### 지금 해야 할 일
1. **이 검토 문서를 worklog에 기록**
2. **Phase 1-A: 핵심 백엔드 (4-6주)**
   - PostgreSQL 스키마 구현
   - FastAPI MCP 엔드포인트
   - JWT 보안 레이어
   - Claude Code 연동 테스트
3. **Phase 1-B: 웹 UI (2-4주)**
   - 반응형 검색 UI (React)
   - 의사결정 기록 폼
4. **Phase 2: 모바일 (1-2주)**
   - 웹 검색 UI 모바일 최적화
   - iOS Shortcuts (자동 캡처)
   - Android 폼 (수동 저장)

### 하지 말아야 할 일
- ❌ iOS 앱스토어 앱은 나중에
- ❌ Seedling DB를 건드리지 말 것
- ❌ 모든 메시지를 임베딩하지 말 것
- ❌ MCP를 HTTP로 구현하지 말 것

### 예상 성과
**구현 후 (3-4개월)**
```
- 집/회사/모바일에서 AI와 나눈 대화 자동 기록
- "왜 이런 결정을 했는가?" 즉시 검색 가능
- Claude Code에서 과거 결정 컨텍스트 자동 제시
- 의사결정 이력 추적 (감사/학습용)
```

---

## 요약표

| 항목 | 현재 (Seedling) | 새 시스템 | 상태 |
|------|-----------------|----------|------|
| **대화 캡처** | ✅ Chrome Ext | ✅ 동일 | 통합 |
| **로컬 저장** | ✅ SQLite | ✅ SQLite 버퍼 | 호환 |
| **중앙화** | ❌ 없음 | ✅ PostgreSQL | 신규 |
| **결정 추적** | ❌ 없음 | ✅ 있음 | 신규 |
| **웹 검색** | ❌ 없음 | ✅ React UI | 신규 |
| **모바일** | ❌ 없음 | ✅ Phase 1 (웹) | 신규 |
| **보안** | ⚠️ 부분 | ✅ JWT+TLS | 강화 |

