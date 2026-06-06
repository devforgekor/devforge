## timer-registry.yaml

- **보관 사유**: `cli.py status --json` (timers 섹션) + `systemctl --user list-timers` 로 완전 대체. systemd가 SSOT이므로 수동 YAML 문서화가 불필요해짐. Session 26 — Phase A 완료 (2026-06-06).
- **대체 위치**: `python3 scripts/cli.py status --json` → `timers` 필드. 개별 타이머 정보는 `systemctl --user list-timers --output json` 으로 직접 조회.
- **삭제 조건**: `cli.py status --json` 1개월 무장애 운영 후 (2026-07-06)
- **주의**: 타이머별 purpose(설명)는 systemd unit 파일의 `Description=` 필드에서 직접 조회할 것.

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
- **대체 위치**: ~~`/opt/projects/server/docs/design.md`, `/opt/projects/server/docs/phases.md`~~ → `docs/specs/system-design.md`, `docs/plans/phases.md`
- **삭제 조건**: 즉시 삭제 가능 (초안 목적 달성, 최종 문서와 무관)
- **주의**: REFERENCE_CARD.txt, plan.md, workspace.yaml, session.db 는 Copilot 내부 파일 — 참고용 외 사용 금지

---

## docs 리팩터링 (2026-06-03) — archive된 설계 문서들

### server-specs-and-llm-architecture.md

- **보관 사유**: `docs/architecture/software.md`(auto-gen)로 대체. LLM model catalog, mode system, pipeline 상세는 CLAUDE.yaml `software:` 섹션으로 이전 예정.
- **대체 위치**: `docs/architecture/software.md`, `docs/architecture/code-structure.md`
- **삭제 조건**: CLAUDE.yaml `software:` 섹션 채워지고 software.md auto-gen 정상화 후 1개월 (2026-07-03)

### agent-architecture.yaml (v3.0.1, 61KB)

- **보관 사유**: `docs/architecture/software.md`(auto-gen)로 대체. 61KB 단일 파일 → 여러 auto-gen 문서로 분산.
- **대체 위치**: `docs/architecture/software.md`, `docs/architecture/code-structure.md`
- **삭제 조건**: software.md가 모든 pipeline/mode/agent 상세를 커버한 후 3개월 (2026-09-03)

### debate-system-spec.yaml

- **보관 사유**: 이미 모든 내용 구현 완료 (`orchestrator.py`, `cooperative_debate.py`, `test_dart_round.py`). 코드를 통한 단일 진실 공급원.
- **대체 위치**: `docs/architecture/software.md` (pipeline 섹션)
- **삭제 조건**: 즉시 삭제 가능

### debate-review-report.yaml / debate-review-report-v1.1.yaml

- **보관 사유**: 1회성 리뷰 완료. 설계 결정은 debate-system-spec에 반영됨.
- **대체 위치**: 없음
- **삭제 조건**: 즉시 삭제 가능

### activity-log-unified-plan.md / activity-log-peer-review-report.md

- **보관 사유**: Activity log 통합 계획 및 리뷰 — 이미 구현 완료.
- **대체 위치**: `docs/architecture/code-structure.md` (data flow)
- **삭제 조건**: 즉시 삭제 가능

### code-as-documentation-review.md

- **보관 사유**: 1회성 리뷰 완료. 결정 사항은 이미 코드에 반영됨.
- **대체 위치**: 없음
- **삭제 조건**: 즉시 삭제 가능

### audit-2026-05-18.md

- **보관 사유**: 1회성 감사 완료.
- **대체 위치**: 없음
- **삭제 조건**: 즉시 삭제 가능

### prompt-ablation-report.md

- **보관 사유**: 프롬프트 실험 결과. 실험 결정은 이미 코드에 반영됨.
- **대체 위치**: 없음
- **삭제 조건**: 즉시 삭제 가능

### gemini-model-comparison.md

- **보관 사유**: Gemini 모델 비교 — 당시 결정에만 사용됨.
- **대체 위치**: 없음
- **삭제 조건**: 즉시 삭제 가능

### seedling-gemini-review.md

- **보관 사유**: Seedling Gemini 검토 — 1회성.
- **대체 위치**: 없음
- **삭제 조건**: 즉시 삭제 가능

### azure-nemotron-setup.md

- **보관 사유**: Nemotron 셋업 기록 — 1회성.
- **대체 위치**: 없음
- **삭제 조건**: 즉시 삭제 가능

### translation-quality-report.md / translation-quality-feedback-loop.md

- **보관 사유**: 번역 품질 분석 — 주기적 생성 중단. 결과는 코드(`lib/text_quality.py`)에 반영됨.
- **대체 위치**: `lib/text_quality.py`
- **삭제 조건**: 즉시 삭제 가능
