# AI 대화 기록 저장 시스템 분석

## 📊 당신의 현재 상황

- **기존 시스템**: seedling (PostgreSQL + pgvector + FTS5)
- **목표**: 다양한 AI와의 대화를 체계적으로 기록 + 검색
- **필요성**: 과거 대화 컨텍스트 활용 → 더 나은 응답 생성

---

## 🎯 7개 프로젝트 중 당신에게 적합한 것은?

### Tier 1: 즉시 도입 가능한 것들

| 프로젝트 | 이유 | 예상 효과 |
|---|---|---|
| **MemPalace** | 96.6% R@5 검색율 + MCP 지원 | 과거 대화에서 최적의 컨텍스트 검색 |
| **Kept** | 다중 AI 통합 + Markdown 호환 | Claude, Gemini, ChatGPT 동시 기록 |
| **Simple Memory MCP** | 가볍고 MCP 기반 | Claude Code/Copilot과 즉시 연동 |

### Tier 2: seedling 통합 가능 (추가 개발 필요)

| 프로젝트 | 이유 |
|---|---|
| **Memos** | PostgreSQL 기반이므로 seedling과 호환 가능 |
| **ChatVault** | RAG 구조가 seedling 파이프라인과 유사 |

### Tier 3: 개발자 환경 특화 (선택적)

| 프로젝트 | 용도 |
|---|---|
| **WayLog** | IDE 내 Claude/Copilot 대화만 기록 |
| **Total Recall** | Claude Code 자체의 메모리 강화 |

---

## 💡 권장 아키텍처 (2가지 옵션)

### 옵션 A: 독립 시스템 (빠른 구축)
```
┌─────────────────────────────────────┐
│ 당신의 다양한 AI 상호작용          │
│ (Claude, Gemini, ChatGPT, etc.)    │
└────────────┬────────────────────────┘
             │
             ↓
┌─────────────────────────────────────┐
│ MemPalace 또는 Kept               │
│ (대화 자동 캡처 + 저장)           │
└─────────────┬───────────────────────┘
             │
             ↓
┌─────────────────────────────────────┐
│ 로컬 저장소                        │
│ (SQLite + ChromaDB 또는           │
│  Markdown + CozoDB)               │
└─────────────────────────────────────┘
```

**장점**: 즉시 도입, 독립적, 타 시스템 영향 없음
**단점**: seedling과 연동 없음

### 옵션 B: seedling 통합 (장기 구축)
```
┌─────────────────────────────────────┐
│ Claude Code / Copilot / 외부 AI   │
└────────────┬────────────────────────┘
             │ (MCP server via Simple Memory MCP)
             ↓
┌─────────────────────────────────────┐
│ seedling PostgreSQL DB             │
│ + pgvector (대화 임베딩)           │
│ + InteractionLogs_fts (검색)      │
└────────────┬────────────────────────┘
             │
             ↓
┌─────────────────────────────────────┐
│ Claude Code → 자신의 지식 활용    │
│ (RAG 기반 응답 개선)              │
└─────────────────────────────────────┘
```

**장점**: seedling과 통합, 기존 RAG 활용
**단점**: 개발 복잡도 높음, 타이밍 필요

---

## ✅ 즉시 실행 가능한 것들 (우선순위)

### Phase 1 (1주)
1. **MemPalace 또는 Kept** 설치 + 테스트
   - 기존 대화 임포트 (Markdown 형식)
   - 크롬 확장 또는 자동 캡처 확인

### Phase 2 (2~3주)
2. **Simple Memory MCP** 설정
   - Claude Code와 연동
   - 현재 세션 대화 자동 저장 확인

### Phase 3 (선택적)
3. seedling의 InteractionLogs_fts와 메모리 통합 검토

---

## 🚨 주의사항

- **DeepSeek, Qwen**: 공식 지원 없음 (API 직접 연동 필요)
- **프라이버시**: 로컬 저장 권장 (CloudFlare 등 외부 저장소 피할 것)
- **스타 수 vs 신뢰성**: MemPalace(1200⭐) > Kept(80⭐) 하지만 Kept도 활발함

---

## 🎯 당신을 위한 최종 권고

**`MemPalace` + `Simple Memory MCP` 조합 추천**

이유:
1. MemPalace: 의미 기반 검색 (96.6% R@5) ← 과거 대화에서 최고의 컨텍스트 찾음
2. Simple Memory MCP: Claude Code와 즉시 연동 ← 새로운 대화 자동 저장
3. 둘 다 로컬 저장 + 경량 ← 프라이버시 + 빠른 응답

### 설정 예상 시간
- MemPalace: 30분 (초기 설치 + 기존 대화 임포트)
- Simple Memory MCP: 20분 (MCP 설정 + Claude Code 연동)
- **총 1시간 이내에 가능**

