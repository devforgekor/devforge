# 🗺️ 구현 로드맵 (우선순위 기반)

## Phase 1: 핵심 인프라 (4~6주) ✅ START HERE

### Week 1-2: 백엔드 구축
- [ ] PostgreSQL + pgvector 새 DB 생성 (`memories.db`)
- [ ] FastAPI 앱 보일러플레이트 (보안 포함)
  - [ ] JWT 인증
  - [ ] CORS 설정
  - [ ] 레이트 제한
- [ ] MCP 서버 구현 (stdio 기반)
  - [ ] `mem_save` 도구
  - [ ] `mem_search` 도구
  - [ ] 비용 최적화 (의사결정만 임베딩)

### Week 2-3: REST API 구현
- [ ] `/api/conversations` (생성, 조회)
- [ ] `/api/messages` (추가)
- [ ] `/api/decisions` (기록)
- [ ] `/api/search` (통합 검색)
- [ ] `/api/sync` (로컬 동기화)

### Week 3-4: 데스크톱 클라이언트
- [ ] Claude Code MCP 설정
  - [ ] config.json 작성
  - [ ] 도구 노출 테스트
- [ ] 로컬 동기화 에이전트 (`sync_agent.py`)
  - [ ] `local_memory.db` 스키마
  - [ ] 10분 주기 동기화
  - [ ] 부팅 자동 실행 설정

### Week 4-5: 검색 UI (React)
- [ ] React 대시보드 기본 구조
  - [ ] 검색 폼
  - [ ] 결과 디스플레이
  - [ ] 필터링 (Wing, Room, 타입)
- [ ] 모바일 반응형 CSS
- [ ] PWA 설정 (옵션)

### Week 5-6: 테스트 & 배포
- [ ] 엔드-투-엔드 테스트
- [ ] 보안 감시 (로깅 등)
- [ ] OCI 또는 AWS에 배포
- [ ] HTTPS 설정

---

## Phase 2: 모바일 저장 (2~3주)

### iOS 단축어
- [ ] 단축어 스크립트 작성
- [ ] HomeScreen에 추가

### Android
- [ ] 간단한 업로드 UI (또는 ADB 사용)

### 웹 앱 (선택)
- [ ] Flutter/React Native 웹뷰 (6개월 이상 소요, 보류)

---

## Phase 3: 자동화 & 모니터링 (진행 중)

- [ ] 의사결정 자동 감지 (NLP)
- [ ] 주기적 요약 생성
- [ ] 사용 통계 대시보드
- [ ] 경고 (저장소 용량, 비용 초과 등)

---

## 🎯 즉시 시작할 수 있는 것

**내일부터**:
1. PostgreSQL `memories` 데이터베이스 생성
   ```sql
   CREATE DATABASE memories;
   ```
2. FastAPI 프로젝트 스켈레톤 작성
3. MCP 서버 라이브러리 학습 (30분)
   ```bash
   pip install mcp asyncpg openai
   ```

**이번 주**:
- MCP 도구 2개 (`mem_save`, `mem_search`) 완성
- PostgreSQL 테이블 생성 스크립트 실행

**이달**:
- Phase 1 완성
- Claude Code로 기억 저장/검색 테스트 완료

