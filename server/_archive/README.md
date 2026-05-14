# _archive — deprecated docs & logic (2026-05-14 기준)

보관 사유가 소멸되면 디렉터리 전체를 삭제합니다.

---

## push_claude_sessions.py

- **보관 사유**: Seedling 프로젝트 Azure Service Bus 연동 스크립트. OCI ARM 서버와 무관. SessionStart hook에서 제거됨 (2026-05-14).
- **대체 위치**: 없음 (이 서버에서 불필요)
- **삭제 조건**: 즉시 삭제 가능
- **주의**: Azure Service Bus 의존 — 이 서버에서 실행 불가

## DESIGN.md

- **보관 사유**: DevForge 애플리케이션 설계 초안 (2026-05-13, "구현 대기" 상태). 오늘(2026-05-14) 기준 Phase 1 완료로 outdated. 고유 내용(MCP 도구, 파일 구조, CLI)은 `/opt/projects/server/docs/design.md`에 병합.
- **대체 위치**: `/opt/projects/server/docs/design.md`
- **삭제 조건**: 즉시 삭제 가능 (모든 내용 이전 완료)
- **주의**: DB 스키마 섹션은 `schema.sql`과 중복 — 정본은 schema.sql

## backup_project_server/

- **보관 사유**: `/opt/projects/server/` 문서화 통합 이전 스냅샷. 옛 경로(`/opt/project/server`) 참조, 2026-05-10 시점.
- **대체 위치**: `/opt/projects/server/docs/` (design.md, phases.md, tasks.yaml), `/opt/projects/server/scripts/` (gen_server_state.py, update_handover.py), `/opt/projects/server/` (CLAUDE.yaml, handover.yaml, state.yaml, changelog.yaml, blueprint.yaml)
- **삭제 조건**: 신규 `/opt/projects/server/` 구조 3개월 무장애 운영 후 (2026-08-14)
- **주의**: `/opt/project/server` 옛 경로 하드코딩 — 빌드/배포에서 제외됨

## backup_project_seedling/

- **보관 사유**: Seedling 프로젝트 구 worklog (SQLite 기반, 877줄 JSON). DevForge는 PostgreSQL + 3항목 hot-warm 구조로 대체.
- **대체 위치**: `devforge_app.worklog_entries` (PostgreSQL, cli.py worklog로 조회)
- **삭제 조건**: DevForge worklog 3개월 무장애 운영 후 (2026-08-14)
- **주의**: Seedling worklog 형식과 호환되지 않음. migrate_worklog_v2.py는 SQLite 대상이므로 DevForge에 적용 불가. `worklog.json`도 2026-05-14 DB-only 전환으로 archive됨.

## worklog.json

- **보관 사유**: file+DB 하이브리드에서 DB-only(Approach B)로 전환 (2026-05-14). 3개 항목은 DB로 이전 완료.
- **대체 위치**: `devforge_app.worklog_entries` (PostgreSQL) + `cli.py worklog recent/search`
- **삭제 조건**: 즉시 삭제 가능
- **주의**: `rotate_worklog.py`도 함께 obsolete — SessionEnd hook에서 제거됨

## rotate_worklog.py

- **보관 사유**: DB-only worklog로 전환되며 rotation 불필요 (2026-05-14).
- **대체 위치**: 없음 (`cli.py worklog add`가 직접 DB에 INSERT)
- **삭제 조건**: 즉시 삭제 가능
- **주의**: SessionEnd hook 및 systemd timer/service에서 제거 완료

## copilot_session_drafts/

- **보관 사유**: DevForge 문서화 통합 이전 Copilot 세션 초안. 2026-05-13 설계 논의 과정에서 생성.
- **대체 위치**: `/opt/projects/server/docs/design.md`, `/opt/projects/server/docs/phases.md`
- **삭제 조건**: 즉시 삭제 가능 (초안 목적 달성, 최종 문서와 무관)
- **주의**: REFERENCE_CARD.txt, plan.md, workspace.yaml, session.db 는 Copilot 내부 파일 — 참고용 외 사용 금지
