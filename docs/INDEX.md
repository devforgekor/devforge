# 문서 인덱스 (INDEX)

> Status: active · Date: 2026-09-14 · Owner: devforge · Related: `docs/CONVENTIONS.md`
> `docs/`의 진입점. 워크스트림별 **정본(canonical)·기록·런북·스펙**을 등록한다. 폴더 자체가 전체 목록이다.

---

## 상태 범례
`proposed`(제안) · `active`(진행) · `done`(완료) · `record`(시점기록) · `superseded`(대체)

## 폴더 안내
| 폴더 | 내용 | 예 |
|---|---|---|
| `architecture/` | [AUTO] 구조 SSOT (편집 금지) | `infrastructure.md`, `software.yaml`, `code-structure.yaml` |
| `specs/` | 계약·스키마 | `schema.sql`, `timer-registry.yaml`, `references.yaml`, `proxy-operations.yaml` |
| `plans/` | 계획·로드맵·설계 | `control-plane-roadmap.md` |
| `reports/` | 조사·검증·분석 기록 | `mcp-cost-baseline.md` |
| `runbooks/` | 실행 절차 | `claude-code-mcp-cleanup.md` |
| 루트 | 최상위 SSOT 참조 | `domain-glossary.yaml`, `system-architecture.md`, `object-storage.md` |

## 워크스트림별 정본

### 1) MCP 통합 / Deep Dive 도구
| 구분 | 문서 | 상태 |
|---|---|---|
| 적용 기록(정본) | `reports/mcp-consolidation-applied-20260911.md` | record |
| rf-triage-07 패치 노트 | `reports/rf-triage-07-patchnote-20260914.md` | record |
| 계획(v1~v3) | `plans/mcp-consolidation-server-side.md` | superseded |
| 분석 | `reports/deepdive-mcp-analysis.md` | record |
| 비용 실측 | `reports/mcp-cost-baseline.md` | record |
| P7 게이팅 조사 | `reports/p7-gating-design-research.md` | record |
| 런북(Claude 정리) | `runbooks/claude-code-mcp-cleanup.md` | proposed |
| 규칙 | `/home/opc/llm-agent-rule.md` → `AGENTS.md`(자동) | active |
| EXA 직접 사용 (MCP 비활성) | `specs/exa-direct-usage.yaml`, `runbooks/exa-direct-usage.md` | active |

### 2) 컨트롤 플레인 / watchdog
| 구분 | 문서 | 상태 |
|---|---|---|
| 로드맵(정본) | `plans/control-plane-roadmap.md` | proposed |
| 조사·검증 | `reports/control-plane-registry-research.md` | record |
| 감사 | `reports/watchdog-comprehensive-audit.md` | record |
| 갭 분석 | `reports/watchdog-port-conflict-gap-analysis.md` | record |
| svc pod 포트포워딩 복구·재발방지 | `reports/svcpod-portforwarding-recovery-20260912.md` | record |

### 3) OCI / 백엔드 / 인프라
| 구분 | 문서 | 상태 |
|---|---|---|
| 스토리지 | `object-storage.md` | active |
| 아키텍처(수동) | `system-architecture.md` | active |
| 스키마/레지스트리 | `specs/schema.sql`, `specs/timer-registry.yaml` | active |
| Azure Golden Image(런북) | `runbooks/runbook-golden-image.md` | active |
| Azure Qwen 엔드포인트 | `runbooks/azure-qwen-deepdive-endpoint.md` | active |
| Azure 재빌드 핸드오버 | `plans/azure-golden-image-rebuild-handover.md` | active |
| Azure Deep Dive E2E 검증 | `reports/azure-deepdive-e2e-verification-20260914.md` | record |
| **Azure Key Vault 시크릿 전환(핸드오버)** | `handover-secrets-kv.md` | **done** |
| **Key Vault 마이그레이션 분석** | `_archive/kv-migration-analysis.md` | archived |
| DataImpulse 대시보드 모니터(계약) | `specs/dataimpulse-monitor.yaml` | active |
| DataImpulse 대시보드 모니터(런북) | `runbooks/dataimpulse-monitor.md` | active |

### 4) 뷰어 / 프론트
| 문서 | 상태 |
|---|---|
| `plans/vercel-viewer-plan.md` | active |
| `reports/plan-review-feasibility.md` | record |

### 5) 데이터 파이프라인
| 문서 | 상태 |
|---|---|
| `reports/14b-5model-10axis-report.md`, `reports/pipeline-e2e-20260621.md`, `reports/architecture-validation.md` | record |
| `specs/14b-comparison-*.yaml/txt` | active |
| `plans/14b-comparison-test-plan.md`, `plans/day-night-*.md` | active |

### 6) 기타
- 이동된 과거 문서는 `reports/`(기록) 또는 `runbooks/`(절차)에 있다.
- 폐기/일회성은 `_archive/`.

### 7) 코드 리팩토링 / devforge 패키지
| 구분 | 문서 | 상태 |
|---|---|---|
| 종합 계획(정본) | `REFACTORING_PLAN.md` | active |
| 코드 아키텍처 | `ARCHITECTURE.md` | active |
| 온보딩/전환 | `MIGRATION_GUIDE.md` | active |
| Track B 계획 | `LLM_PROVIDER_PLAN.md` | proposed |
| CLI/HTTP API 레퍼런스 | `API_REFERENCE.md` | active |
| 운영 가이드 | `OPERATIONS_GUIDE.md` | active |
| 설계 결정 기록(ADR) | `adr/0001-config-priority.md` ~ `adr/0006-mcp-tool-surface.md` | record |
| 시스템 전체 구조 | `system-architecture.md` (§3.5 코드 레이어) | active |
| **최종 통합 계획(Cutover+MCP+결정)** | `plans/final-plan.md` | active |
| 업계 표준 대조 진단 | `reports/industry-standard-comparison-20260914.md` | record |
| MCP 툴 사용 감사(30일) | `reports/mcp-tool-audit-20260914.md` | record |
| 외부 검토 브리프 | `reports/review-brief-20260914.md` | record |
| Ingest/Provenance 계약 | `specs/ingest-provenance.yaml` | proposed |
| 이전 구조 정리(완료) | `plans/code-size-refactoring.md`, `reports/handover-refactoring.md` | record |

## 최근 변경 (2026-09-18)
- **Azure Key Vault 시크릿 전환 완료**: 평문 시크릿 제거 완료 (`secrets.env` + `.env.local` 삭제), 10개 systemd 서비스 Key Vault 전환, 6개 Python 파일 리팩터링 (환경변수 우선 패턴), kuhwa 워크플로우 수정. `handover-secrets-kv.md` 완료.
- **GitHub 조직**: devforgekor (https://github.com/devforgekor) — 8개 저장소 (devforge, kuhwa, timetable, cashbook, ebook, azure, pdf-converter, oci-arm-grabber).

## 최근 변경 (2026-09-14)
- **MCP 툴 사용 감사(2026-09-14)**: opencode `part` DB 30일 실사용 분석 → 로드 33툴 중 **0회 9개**. keep/merge/remove 확정(`reports/mcp-tool-audit-20260914.md`): Remove(lsp proxy_artifact 3·detect_lsp_servers·find_symbol·inspect_symbol·get_symbol_source·list_plans 등), Merge(deepdive 4→1, mem+obs→2, search 2→1, list_tables+schema→1), 목표 33→약 16.
- **외부 검토 브리프(2026-09-14)**: 서버 무지(無知) 에이전트의 리뷰용 자가완결 브리프 작성 — `reports/review-brief-20260914.md` (구조·변화·근거·리뷰질문·용어집). Droplr: 브리프 `d.pr/IFoWKE`, 최종계획 `d.pr/14w4o6`, 근거부록 `d.pr/KXEiPa`.
- **최종 계획서 통합(2026-09-14)**: 컷오버 계획 + MCP 툴 최적화 + 의사결정(D1~D9)을 [`plans/final-plan.md`](./plans/final-plan.md)로 통합. 구 `plans/cutover-remaining-plan.md`·`plans/open-decisions.md`는 `docs/_archive/plans/`로 superseded.
- **업계 표준 대조 문서화(2026-09-14)**: 웹 조사(5계층)와 서버 실측 대조 → `reports/industry-standard-comparison-20260914.md`. 결정 기록 `adr/0005-extraction-routing.md`(추출 하이브리드 라우팅·후보정)·`adr/0006-mcp-tool-surface.md`(계약 보존·ingest·provenance; 점진공개 옵션). 계약 스펙 `specs/ingest-provenance.yaml`.
- **은퇴/적용(2026-09-14)**: Shadow DB(`devforge_shadow`) 라이브 적용. 문서생성기 `gen_architecture.py` 은퇴 → `_archive/` (호출부 `system_sync.sh`·`day_cycle.sh`·`daily-structure.service` 제거; `architecture/*`는 수동/동결). Gemini 에이전트 세션 로직 은퇴 → `_archive/gemini-agent/` (`gemini-session.service` disable).
- **Cutover 계획**: `scripts/*` → `devforge` 전환(단계 A~I)·MCP 최적화·의사결정을 [`plans/final-plan.md`](./plans/final-plan.md)에 통합. (라이브 유닛 22개+컨테이너 3개 의존, 다수 도메인 미구현)
- **SSOT 동기화**: `architecture/code-structure.yaml`이 신규 `src/devforge/` 패키지를 포함하도록 생성기(`gen_architecture.py`) 확장(62 그룹/377 파일). `system-architecture.md`에 §3.5 코드 레이어 추가(컷오버 미완료 명시). 리팩토링 문서(REFACTORING_PLAN/ARCHITECTURE/MIGRATION_GUIDE/LLM_PROVIDER_PLAN/API_REFERENCE/OPERATIONS_GUIDE/adr)를 INDEX에 등록. `CLAUDE.yaml`·`blueprint.yaml`·`handover.yaml`에 리팩토링 기준 반영. `docs/specs/schema.sql`을 ORM과 일치하도록 재생성(16 테이블).
- Azure `azureqwen` Deep Dive **E2E 검증 성공**(opencode → `deepdive_step_enter/exit` → DB `DONE`) — [`reports/azure-deepdive-e2e-verification-20260914.md`](./reports/azure-deepdive-e2e-verification-20260914.md).
- 골든 이미지 ctx 결함(8192 < opencode ~16.7k) 발견 → **SSOT `-c 32768`** 정정([`runbooks/runbook-golden-image.md`](./runbooks/runbook-golden-image.md), `azure:20137133/.../yearly_refresh.sh`). `-t 2` 무효(1 core/SMT, memory-bound).

## 최근 변경 (2026-09-12)
- svc pod 호스트 포트포워딩(rootlessport) 장애 복구 + **watchdog 재발방지**(`check_svcpod_ports`/`recover_svcpod_forwarding`, task#32) — [`reports/svcpod-portforwarding-recovery-20260912.md`](./reports/svcpod-portforwarding-recovery-20260912.md).
- Quadlet stub(`container-flaresolverr.container`) 제거, `activity_summarizer`/`checker`/`handover_db` 버그 수정.
- DataImpulse 모니터 IP Whitelist 모드 추가·버그 수정 — [`specs/dataimpulse-monitor.yaml`](./specs/dataimpulse-monitor.yaml), [`runbooks/dataimpulse-monitor.md`](./runbooks/dataimpulse-monitor.md).
- EXA MCP 비활성화 및 직접 사용 문서화 — [`specs/exa-direct-usage.yaml`](./specs/exa-direct-usage.yaml), [`runbooks/exa-direct-usage.md`](./runbooks/exa-direct-usage.md).

## 최근 변경 (2026-09-11)
- MCP 통합 완료: 활성 MCP 4종, 스키마 36,672→8,416 tok/turn(−77%).
- 문서 규칙·인덱스 도입(CONVENTIONS/INDEX), 루트 문서 정리.
