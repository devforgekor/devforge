# DevForge Server — 현황 및 계획

## Phase 1: 기본 인프라 + MCP 서버 (완료, 2026-05-14)

- [x] Podman Quadlet 6개 컨테이너 (ai-pod + data-pod)
- [x] PostgreSQL 16 + pg_trgm + JSONB meta
- [x] MCP SSE 서버 (mem_save, mem_search)
- [x] POST /ingest (대화 배치 저장)
- [x] Python CLI 도구 (search/save/recent/worklog add/recent/search)
- [x] GET /stats (7섹션 대시보드)
- [x] daily pg_dump + monthly restore test
- [x] RSS 업스트림 버전 모니터링
- [x] worklog_entries DB + tasks.yaml Kanban 작업 추적기
- [x] session_guard.py 자동 커밋 + session_start.py 컨텍스트 주입
- [x] collect_turns.py 15분 주기 자동 수집 (Claude Code + Copilot 세션)
- [x] link_turns.py KST 기준 턴↔워크로그 nightly 매칭
- [x] source_message_id + UNIQUE 인덱스 (AI 원본 UUID 기반 중복 차단)
- [x] turns.agent denormalization (JOIN 없는 매칭 쿼리)
- [x] link_turns.py Phase 2 deep review (orphan 감지, 커버리지, 교차 검증)
- [x] tm 세션 런처 (devforge/seedling/커스텀)
- [x] AGENT_MAP SSOT (claude→claude-code 정규화, lib/agents.py)
- [x] orphan 백필 (429/429 턴↔워크로그 완전 연결)
- [x] agent 불일치 버그 수정 (worklog claude-code vs turns claude)

## Phase 2: 고도화 (계획)

- [ ] pgvector 임베딩 검색 (시멘틱 검색)
- [ ] MemPalace 분류 (wing/room, 대화 분류 자동화)
- [ ] CLI Search-Augmented 통합
  - `/opt/projects/qwen-cli` 기반 확장
  - DuckDuckGo 검색 → Qwen 추론 파이프라인
  - DevForge `cli.py`와 통합 (검색 결과 자동 저장)
  - 참조: memory/qwen-cli-reference.md (2026-05-14)
- [ ] Web UI (대화 검색 대시보드)

## Phase 3: someday/maybe

- [ ] Chrome Extension → 서버 직접 전송 (현재는 Mac 경유)
- [ ] iOS Shortcuts 연동
- [ ] 다중 LLM 라우팅 (litellm에 추가 모델)
