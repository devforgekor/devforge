# 문서 규칙 (CONVENTIONS)

> Status: active · Date: 2026-09-11 · Owner: devforge · Related: `docs/INDEX.md`
> 이 문서는 `docs/` 작성·배치 규칙의 SSOT다. 새 문서·이동·개명 시 이 규칙을 따른다.

---

## 1. 디렉토리 = 문서의 성격
```
docs/
├─ INDEX.md            # 전체 지도(진입점)
├─ CONVENTIONS.md       # 이 문서(규칙 SSOT)
├─ architecture/        # 구조 SSOT (동결 — 수동 관리)
├─ specs/               # 계약·스키마(DDL, registry, references)
├─ plans/               # 앞을 향한 계획·로드맵·설계 (proposed|active)
├─ reports/             # 시점 기록·조사·검증 (record, superseded 가능)
├─ runbooks/           # 실행 절차 (operational)
├─ _archive/            # 폐기·일회성
└─ (루트)               # 최상위 SSOT 참조 + 리팩토링 정본
```
- `architecture/`의 `infrastructure.md`·`software.yaml`·`code-structure.yaml`은 생성기(`gen_architecture.py`) **은퇴(2026-09-14)** 로 동결 — 이제 **수동 관리**한다(불필요 시 `_archive/`).
- 루트에는 **최상위 SSOT 참조 + 코드/설정이 경로를 참조하는 정본**만 둔다: `INDEX.md`, `CONVENTIONS.md`,
  `domain-glossary.yaml`, `system-architecture.md`, `object-storage.md`, 그리고 리팩토링 정본군
  (`REFACTORING_PLAN.md`, `ARCHITECTURE.md`, `MIGRATION_GUIDE.md`, `LLM_PROVIDER_PLAN.md`,
  `API_REFERENCE.md`, `OPERATIONS_GUIDE.md`). 나머지는 성격별 폴더로.

## 2. 문서 타입별 템플릿
| 타입 | 위치 | 필수 섹션 |
|---|---|---|
| **Plan** | `plans/` | 목적·배경 / 범위(비목표) / 최소 조건 / 단계(+수락기준) / 롤백 / 미결 / 근거 |
| **Report** | `reports/` | 목적 / 방법 / 결과(데이터) / 검증 / 결론 / 출처 |
| **Runbook** | `runbooks/` | 선행조건 / 절차(번호) / 검증 / 롤백 / 주의 |
| **Spec** | `specs/` | 계약(스키마/인터페이스) / 버전 / 변경이력 |

## 3. 공통 헤더 + 상태
모든 문서는 제목 바로 아래에 한 줄:
```
> Status: proposed|active|done|record|superseded · Date: YYYY-MM-DD · Owner: <agent> · Related: <links>
```
| Status | 의미 | 위치 |
|---|---|---|
| `proposed` | 제안(미착수) | plans/ |
| `active` | 진행 중 | plans/ |
| `done` | 완료(계획) | plans/ 또는 승격 |
| `record` | 시점 기록(불변) | reports/ |
| `superseded` | 대체됨 | _archive/ |

- 레거시 문서는 **다음 수정 시** 헤더를 부여한다(과거 문서 일괄 개서 금지).

## 4. 언어·길이
- **human-facing(reports/runbooks/roadmap/plans) = 한국어**, **machine-readable(specs/DDL/식별자/프롬프트) = 영어**.
- 문서 **≤400줄** 권장. 초과 시 분할(파일명에 `-2` 등). 단, 기존 장문 문서는 **다음 대개정 시** 분할하며
  2026-09-14 기준 예외를 인정한다: `_archive/plans/day-night-split-handoff-plan.md`(814), `REFACTORING_PLAN.md`(564),
  `_archive/plans/slack-chatops-design.md`(559), `reports/etextbook-patch-analysis.md`(523), `runbooks/runbook-golden-image.md`(503),
  `plans/design-bash-to-python-migration.md`(456), `reports/opencode-roundrobin-failure-analysis.md`(444),
  `plans/etextbook-docker-deployment-plan.md`(433).
- 코드 블록 내 식별자·경로·명령은 영어.

## 5. SSOT·링크 규칙
- **1주제 = 정본 1개.** 다른 문서는 **링크만**(복사 금지).
- **INDEX.md가 진입점**: 워크스트림별 정본·상태·관련 기록을 등록.
- 라이프사이클: `plan(active)` → 완료 시 `report(record)`로 승격(계획은 `superseded`/`done`).

## 6. 이동·개명 규칙
1. **참조 선확인**: `grep -rnE "docs/<경로>" --include=*.py --include=*.yaml --include=*.md`로 참조 확인.
2. 이동은 `git mv`(이력 보존). 개명은 kebab-case.
3. 이동 후 **참조 갱신 + 재확인**(`CLAUDE.yaml entry_points`, 코드의 docs 경로).
4. 구조 SSOT(`architecture/*`, 동결)와 코드 참조 경로(`domain-glossary.yaml`, `specs/*`)는 **이동 금지**.
