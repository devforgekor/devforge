# DevForge Server — 현황 및 계획

## Phase 1: 기본 인프라 + MCP 서버 (완료, 2026-05-14)

- [x] Podman Quadlet 컨테이너 (devforge-api, devforge-swap, devforge-qwen, postgres)
- [x] PostgreSQL 16 + pg_trgm + JSONB meta
- [x] MCP SSE 서버 (mem_save, mem_search)
- [x] POST /ingest (대화 배치 저장)
- [x] Python CLI (search/save/recent/worklog add/recent/search)
- [x] GET /stats (7섹션 대시보드)
- [x] daily pg_dump + monthly restore test
- [x] worklog_entries DB + tasks.yaml Kanban 작업 추적기
- [x] session_guard.py 자동 커밋 + session_start.py 컨텍스트 주입
- [x] collect_turns.py 15분 자동 수집 (Claude Code + Copilot 세션)
- [x] link_turns.py KST nightly 매칭 + orphan 감지
- [x] activity_log 테이블 + 커밋/스테이지/리뷰 로깅
- [x] session_guard.py git commit → activity_log dual-write
- [x] cli.py activity recent/stats 대시보드

## Phase 1.5: LLM 추론 인프라 (완료, 2026-05-23)

- [x] 2-Container 아키텍처 — Podman A(Qwen3-4B:8080) + Podman B(mode-switchable:8081-8082)
- [x] 모드 전환 시스템 (normal / batch / code)
- [x] code_mod_pipeline.py — 32B 4-stage 파이프라인 (ANALYZE→PLAN→IMPL→PACKAGE)
- [x] Prompt ablation — 코드 슬라이싱 89% 토큰 감축
- [x] review_worker.py — 3-LLM 토론 파이프라인 (Qwen3-4B + Llama-3B 병렬 → Phi-4-mini 중재)
- [x] review_facts DB + 성능 메트릭 (prompt_tokens, gen_tokens, gen_rate, cache_hit)
- [x] RateEstimator 동적 타임아웃
- [x] Slack 알림 연동 (스테이지 완료 시 DM)
- [x] 레퍼런스 추적 — lib/refs.py (GitHub API 5개 프로젝트 + 내부 grep, 15분 주기)
- [x] DB references 테이블 + gen_server_state.py 통합

## Phase 2: 고도화 (계획)

### 2.1 복구 / 안정화 (바로)
- [x] swap-batch.timer + swap-normal.timer 재활성화
- [x] review-worker.timer 재활성화
- [x] LiteLLM 복구 또는 제거 결정 (현재 failed, 미사용) → 제거 완료 (2026-05-19)
- [x] devforge-llm 복구 또는 제거 결정 (현재 failed, swap으로 대체) → 제거 완료 (2026-05-19)
- [ ] journald 로그 보존 설정 (MaxRetentionSec=30day)

### 2.2 시멘틱 검색 (pgvector)
- [x] pgvector 확장 설치 + turns.text 임베딩 벡터 컬럼
- [x] 임베딩 생성 (embed_turns.py, 로컬 Qwen 또는 API)
- [x] CLI search --semantic (pgvector ANN + ILIKE 하이브리드)
- [ ] MCP mem_search 벡터 검색 업그레이드

### 2.3 MemPalace 분류
- [ ] wing/room 카테고리 체계 정의
- [ ] 자동 분류 (LLM 기반, review_worker와 유사 패턴)
- [ ] CLI search --wing/--room 필터링
- [ ] 분류 결과 activity_log 기록

### 2.4 Search-Augmented 통합
- [ ] DuckDuckGo 검색 → LLM 추론 파이프라인
- [ ] /opt/projects/qwen-cli 기반 확장 (있는 경우)
- [ ] cli.py search --augmented (검색 + LLM 분석 결과)
- [ ] 참조: memory/qwen-cli-reference.md (2026-05-14)

### 2.5 레퍼런스 추적 고도화
- [x] rss-monitor.service — GitHub RSS 주기적 폴링 (reference-watchlist.md 9개 전 항목)
- [ ] Snyk/CISA 취약점 자동 스캔 (컨테이너 이미지)
- [x] 4-month refresh cycle 알림 → 자동화 (2026-08-15 최초 실행)
- [ ] refresh-log.md 기록 자동화

### 2.6 Web UI
- [ ] 대화 검색 대시보드 (FastAPI + 간단한 프론트엔드)
- [ ] 모델 성능 대시보드 (review_facts 통계 시각화)
- [ ] activity_log 실시간 피드

## Phase 3: someday/maybe

- [ ] Chrome Extension → 서버 직접 전송 (현재 Mac 경유)
- [ ] iOS Shortcuts 연동
- [ ] 다중 LLM 라우팅 (추가 모델 연동)
- [ ] T01~T08 32B 코드 수정 전체 재테스트 (간헐적 실행)




