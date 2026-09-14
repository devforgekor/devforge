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
| 설계 결정 기록(ADR) | `adr/0001-config-priority.md` ~ `adr/0004-alembic-migrate.md` | record |
| 시스템 전체 구조 | `system-architecture.md` (§3.5 코드 레이어) | active |
| 이전 구조 정리(완료) | `plans/code-size-refactoring.md`, `reports/handover-refactoring.md` | record |

## 최근 변경 (2026-09-14)
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
