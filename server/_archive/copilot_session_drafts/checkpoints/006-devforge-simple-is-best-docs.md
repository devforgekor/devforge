<overview>
사용자는 AI 대화를 체계적으로 저장·검색하는 개인용 시스템(DevForge)을 설계하고, 이를 문서화하는 작업을 진행했다. "Simple is Best" 원칙 하에 1인 개발자에게 필요한 최소한의 문서 세트를 만들었고, 마지막으로 외부 리뷰 결과 3가지 핵심 결함(진짜 MCP 서버 부재, decisions 테이블 누락, CORS 미설정)이 발견되어 보강이 필요한 상태다.
</overview>

<history>
1. DevForge vs Seedling 아키텍처 확정
   - DevForge = 현재 서버, 개인용 AI 지식 창고
   - Seedling = 추후 별도 서버, 다중 사용자 플랫폼
   - Chrome Extension v0.4.8 이미 완성 확인

2. 오픈소스 레퍼런스 검토 (MemPalace, Kept, WayLog 등)
   - PostgreSQL + pgvector + FastAPI + MCP 조합으로 설계 확정

3. 버전 관리 전략 수립 → "Simple is Best"로 축소
   - 엔터프라이즈 관행 제거: Nexus Registry, Staging 서버, Release Monitor 자체 호스팅 등
   - 5 Golden Rules로 압축

4. 문서 세트 생성 (6개 파일, 총 15KB)
   - plan.md, DEVFORGE_SIMPLE.md, QUICK_START_SOLO_DEV.md, README.md, REFERENCE_CARD.txt, AUDIT_SIMPLE_IS_BEST.md

5. 다운로드 시도 → HTTP 서버 시작했으나 사용자가 접근 불가
   - /tmp/devforge-docs.zip (7.7KB) 생성됨, 포트 8765

6. 외부 리뷰로 3가지 핵심 결함 식별
   - MCP 서버가 REST API일 뿐 (진짜 MCP 프로토콜 아님)
   - decisions 테이블 없음
   - CORS 미설정

7. 사용자 "진행" 요청 → 3가지 보강 작업 실행 필요
</history>

<work_done>
Files created/modified:
- `/home/opc/.copilot/session-state/2f311dcb-ab35-44fa-939d-30fef9b019d0/plan.md` — 820B, 미션 + 아키텍처 + 5 Rules
- `/home/opc/.copilot/session-state/2f311dcb-ab35-44fa-939d-30fef9b019d0/DEVFORGE_SIMPLE.md` — 5.3KB, Copy-Paste 템플릿 (docker-compose, requirements.txt, Dockerfile, main.py, dependabot.yml)
- `/home/opc/.copilot/session-state/2f311dcb-ab35-44fa-939d-30fef9b019d0/QUICK_START_SOLO_DEV.md` — 618B, Day 1 체크리스트
- `/home/opc/.copilot/session-state/2f311dcb-ab35-44fa-939d-30fef9b019d0/README.md` — 1.3KB
- `/home/opc/.copilot/session-state/2f311dcb-ab35-44fa-939d-30fef9b019d0/REFERENCE_CARD.txt` — 5.1KB
- `/home/opc/.copilot/session-state/2f311dcb-ab35-44fa-939d-30fef9b019d0/AUDIT_SIMPLE_IS_BEST.md` — 2.2KB
- `/tmp/devforge-docs.zip` — 7.7KB 압축본 (HTTP 서버 포트 8765에서 서빙 중)
- `/opt/workspace/seedling/docs/worklog.json` — DevForge/Seedling 아키텍처 결정 기록

Work completed:
- [x] DevForge vs Seedling 아키텍처 확정
- [x] 버전 관리 5 Golden Rules 수립
- [x] 문서 세트 생성 (Simple is Best 적용)
- [x] 압축 파일 생성 + HTTP 서버 기동
- [ ] MCP 서버 진짜 구현 추가 (app/mcp_server.py)
- [ ] decisions 테이블 스키마 추가
- [ ] CORS 미들웨어 추가
- [ ] .gitignore + .env 분리

Currently working on: 3가지 핵심 결함 보강 (MCP, decisions, CORS)
</work_done>

<technical_details>
- MCP 서버는 HTTP REST가 아니라 stdio 기반 Server 클래스 사용 필요 (`mcp` Python 패키지)
- 현재 main.py의 /mcp/memory/save 는 REST API일 뿐, Claude Code에서 직접 @memory 도구로 쓸 수 없음
- decisions 테이블이 핵심 기능("왜 그런 결정을 했는지")인데 스키마에서 누락됨
- CORS 없으면 Chrome Extension → FastAPI 직접 호출 불가
- pgvector 이미지: `pgvector/pgvector:0.5.1-pg15`
- DB 연결: asyncpg (not SQLAlchemy ORM)
- Seedling 현재 서버: PostgreSQL 16 + pgvector + asyncpg + Caddy (OCI ARM)
- Chrome Extension v0.4.8: 5개 AI 플랫폼 지원 (ChatGPT, Claude, Gemini, DeepSeek, AiStudio), /plugin/ingest 엔드포인트로 전송
- DevForge 예산: $8/mo, 1인 개발
</technical_details>

<important_files>
- `/home/opc/.copilot/session-state/2f311dcb-ab35-44fa-939d-30fef9b019d0/DEVFORGE_SIMPLE.md`
  - 가장 중요: Copy-Paste 템플릿 모음
  - 보강 필요: CORS 코드, decisions 테이블, .gitignore, mcp_server.py 추가
- `/home/opc/.copilot/session-state/2f311dcb-ab35-44fa-939d-30fef9b019d0/plan.md`
  - 전체 미션 + 아키텍처 + 5 Rules 요약
- `/opt/workspace/seedling/docs/worklog.json`
  - DevForge/Seedling 아키텍처 결정 기록
  - 최신 entries 유지 필요
- `/opt/workspace/seedling/app/routes_plugin.py`
  - Chrome Extension → 서버 연결 기존 엔드포인트
  - DevForge 확장 시 참고
</important_files>

<next_steps>
지금 당장 해야 할 작업 (3가지 핵심 보강):

1. **DEVFORGE_SIMPLE.md에 추가**
   - CORS 미들웨어 코드 (main.py에)
   - .gitignore 템플릿
   - decisions 테이블 SQL
   - mcp_server.py 최소 스텁

2. **압축 파일 재생성** (devforge-docs.zip)
   - 보강된 DEVFORGE_SIMPLE.md 포함

3. **worklog.json 업데이트**
   - 이번 작업 기록

Blockers:
- HTTP 다운로드 링크 제공 방법 미해결 (사용자가 접근 불가)
</next_steps>



---

이제 3가지 보강 작업을 진행합니다.