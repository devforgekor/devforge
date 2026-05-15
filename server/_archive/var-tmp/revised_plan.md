# 🎯 수정된 아키텍처: Seedling vs 새 시스템 (명확한 역할 분리)

## 사용자 의도 정리

> "서버에서는 이렇게까지 안할 거야. 수집은 하지만. 내가 직접 ssh에 접속해서 cli로 검색하면서 데이터를 조회하면 된다. 
> seedling은 별도로 이렇게 하면 좋을 것 같아. 여기에 사용자 별로 별도 db만 만들어주면 되니까"

### 해석
1. **현재 서버 (Seedling)**: 수집만 (Chrome Ext), CLI 조회 (SSH 접속)
2. **새 시스템**: 중앙화 + 웹 UI + 모바일 등 모든 고급 기능
3. **차이점**: Seedling은 "간단한 로컬 DB", 새 시스템은 "중앙 PostgreSQL + 검색"

---

## 최종 아키텍처

```
┌─────────────────────────────────────────┐
│        Seedling (현재 서버)              │
│  ┌─────────────────────────────────────┐│
│  │ Chrome Extension v0.4.8              ││
│  │ (수집만: ChatGPT, Claude, ...)      ││
│  │ → /plugin/ingest                   ││
│  └──────────────┬──────────────────────┘│
│                 │                       │
│  ┌──────────────▼──────────────────────┐│
│  │ SQLite (사용자별 DB)                 ││
│  │ - conversations                    ││
│  │ - messages                         ││
│  │ ✅ 완성 (이미 있음)                 ││
│  └──────────────┬──────────────────────┘│
│                 │                       │
│  ┌──────────────▼──────────────────────┐│
│  │ CLI 조회 (신규, 간단함)             ││
│  │ $ seedling search "python"         ││
│  │ $ seedling list --user me          ││
│  └──────────────────────────────────────┘│
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│    새 시스템 (독립된 서버)                │
│    "AI 대화 중앙화 시스템"               │
│  ┌─────────────────────────────────────┐│
│  │ PostgreSQL + pgvector              ││
│  │ - decisions (의사결정)             ││
│  │ - embeddings (벡터 검색)           ││
│  │ - 사용자별 격리                    ││
│  └──────────────┬──────────────────────┘│
│                 │                       │
│  ┌──────────────▼──────────────────────┐│
│  │ FastAPI MCP Server                 ││
│  │ - mem_save 도구                    ││
│  │ - mem_search 도구                  ││
│  └──────────────┬──────────────────────┘│
│                 │                       │
│  ┌──────────────▼──────────────────────┐│
│  │ 웹 UI + 모바일 (React, Shortcuts)  ││
│  │ (중앙 검색, 의사결정 관리)          ││
│  └──────────────────────────────────────┘│
└─────────────────────────────────────────┘
```

---

## 역할 분명한 설명

### Seedling의 역할: "데이터 수집소"
- **수집**: Chrome Extension으로 AI 대화 자동 캡처
- **저장**: SQLite (사용자별 DB, 이미 완성)
- **조회**: CLI 명령어 (간단한 검색, SSH 접속)
- **비용**: 거의 없음 (로컬 DB만)

### 새 시스템의 역할: "지식 창고"
- **입수**: Seedling에서 수집한 데이터 또는 직접 MCP 도구 호출
- **분석**: pgvector로 의사결정 추적, 벡터 검색
- **제공**: 웹 UI, 모바일 앱, CLI (SSH)
- **비용**: PostgreSQL 호스팅, OpenAI 임베딩

---

## Seedling에 추가할 것 (간단함)

### 1. 기존: /plugin/ingest (이미 완성)
```
POST http://localhost:8000/plugin/ingest
{
  "user_id": "me",
  "turn_id": "turn_xxx",
  "source": "chrome-plugin",
  "payload": {
    "user": "Python 배우고 싶어",
    "answer": "Python은...",
    "meta": {
      "url": "https://claude.ai",
      "platform": "claude",
      "captured_at": "2026-05-13T12:00Z"
    }
  }
}
```

### 2. 신규: CLI 조회 (추가)
```bash
# SSH 접속 후
$ sqlite3 /data/seedling_me.db

# 또는 Python CLI 스크립트
$ python -m seedling search "python" --user me --limit 10
$ python -m seedling list --user me --since "2026-05-01"
$ python -m seedling export --user me --format json > me_export.json
```

### 3. 스키마: 이미 있는 것 확인만 하면 됨
```sql
-- routes_chat.py에 이미 있는 것 확인
SELECT * FROM conversations WHERE user_id = 'me';
SELECT * FROM messages WHERE conversation_id = ?;
```

---

## 새 시스템에서 해야 할 것

### Phase 1: 백엔드 (4-6주)
- PostgreSQL + pgvector 스키마
- MCP 서버 (mem_save, mem_search)
- JWT 보안

### Phase 2: 웹 UI (2-4주)
- React 검색 인터페이스
- 의사결정 기록 폼

### Phase 3: 모바일 (1-2주)
- iOS Shortcuts
- Android 폼

---

## 정리

| 항목 | Seedling | 새 시스템 |
|------|----------|---------|
| **수집** | ✅ Chrome Ext | ❌ |
| **저장소** | SQLite (로컬) | PostgreSQL (중앙) |
| **조회** | ✅ CLI (ssh) | ✅ 웹 UI + 모바일 |
| **의사결정** | ❌ | ✅ |
| **벡터 검색** | ❌ | ✅ |
| **비용** | 무료 | 저가 (호스팅 + API) |

---

## 다음 액션

### 이 주
1. ✅ Seedling CLI 조회 기능 구현 (간단함, 1-2일)
2. 새 시스템 PostgreSQL 환경 구성 시작

### 추후
- 새 시스템 Phase 1-A 백엔드 구현

