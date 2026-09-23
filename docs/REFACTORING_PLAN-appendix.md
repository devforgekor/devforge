# REFACTORING_PLAN 부록 — 실행 계획 후반(Phase 4~8) + 업계 표준 비교
> 상위: [REFACTORING_PLAN.md](./REFACTORING_PLAN.md) (정본: [plans/final-plan.md](./plans/final-plan.md))
> 400줄 규칙(CONVENTIONS)에 따라 분리됨(2026-09-20).

### Phase 4: Turn Collection + MCP 재구성 (Week 10)

| 작업 | 산출물 | 검증 |
|------|--------|------|
| `TurnWatcher` 클래스화 | `domain/turn_collection/` | 3초 폴링, 체크포인트 보존 |
| MCP 도구 18개 → 8개 네임스페이스 | `adapters/driving/mcp/tools/{knowledge,pipeline,inference,actions,watchdog,deepdive}/` | `devforge mcp tools` 계층적 탐색 |
| `AgentInterface` 추상화 | `application/agent_interface.py` | Web/CLI/Scheduled 공통 인터페이스 |
| 배치 리뷰 시스템 | `application/issue_collector.py` + tools | P0/P1 자동 수집 → 주간 리포트 |

### Phase 5: 잔여 도메인 + 인터페이스 (Week 11)

| 작업 | 산출물 | 검증 |
|------|--------|------|
| `storage/` (OCI SDK + FileRegistry) | `adapters/driven/storage/` | OCI 업로드/목록 조회 성공 |
| `file_exchange/` (blob_explorer) | `adapters/driven/file_exchange/` | FastAPI 라우터 분리 |

**조정**: `notification/`, `research/`, `proxy_utils/`는 **Phase 8(안정화)**로 이동 → Week 11에 2개 도메인만 처리

### Phase 6: 컨테이너화 + CI/CD (Week 12-13)

| 작업 | 산출물 |
|------|--------|
| 멀티스테이지 Dockerfile | 단일 이미지, 다중 진입점 |
| Quadlet (`Image=localhost/devforge:latest`**digest 고정**) | systemd 재시작 + 5분 롤백 가능 |
| GitHub Actions CI (ruff, mypy, pytest, import-linter) | PR 검증 |
| `docs/ARCHITECTURE.md` | 새 구조 반영 |
| `docs/MIGRATION_GUIDE.md` | 팀 온보딩용 |
| `docs/adr/0001-config-priority.md` | ConfigRegistry 우선순위 |
| `docs/adr/0002-llm-provider-flag.md` | Provider 추상화 결정 (Track B는 별도) |

### Phase 7: Final Cutover (Week 14)

| 작업 | 검증 |
|------|------|
| 기존 `scripts/` 임포트 루트 제거 | `python -c "import lib"` → ImportError |
| 첫 주간 리포트 (collected_issues) | 리포트 생성 테스트 |
| 문서 동기화 (ARCHITECTURE, API_REFERENCE) | 최신 구조 반영 |
| 롤백 훈련 (Quadlet digest 고정) | **5분 이내** 롤백 검증 |
| `day_cycle.sh` → `devforge pipeline orchestrate` | 래퍼 10줄 검증 |

### Phase 8: Stabilization (Week 15-16)

| 작업 | 검증 |
|------|------|
| `notification/` (Slack/Telegram/Apprise) | `adapters/driven/notification/` | 알림 API 호출 테스트 |
| `research/` (exa/context7/web) | `adapters/driven/research/` | ResearchFacade 동작 |
| `proxy_utils/` (게이트웨이) | `adapters/driven/proxy_utils/` | 게이트웨이 라우팅 |
| Bug triage | GitHub 이슈 0건 (Critical) |
| 팀 적응 | Onboarding 완료 (2일 목표) |

> **안정화 기간**: 리팩토링 후 버그 수정, 잔여 도메인 마무리, 팀 온보딩. **KPI 측정 시작**.
>
> **최종 통합 계획(컷오버 + MCP 최적화 + 의사결정)**: [`docs/plans/final-plan.md`](./plans/final-plan.md) — 라이브 systemd 유닛 22개/Quadlet 3개의 `scripts/*` → `devforge` 전환 + MCP 툴 최적화 + 결정 D1~D9.

---

## 5. 업계 표준 비교

### 5.1 프로젝트 구조
| 측면 | 목표 | 업계 표준 |
|------|------|-----------|
| 패키지 레이아웃 | src-layout | [Python Packaging Guide](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/) |
| 진입점 | `pyproject.toml` entry_points | Typer CLI 모범 |
| 설정 | ConfigRegistry (BaseSettings) | [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) |
| 아키텍처 | Ports & Adapters | [Cockburn Hexagonal (2005)](https://alistair.cockburn.us/hexagonal-architecture/) |
| 테스트 | Characterization + replay | [Feathers *WELAC*](https://www.goodreads.com/book/show/398787.Working_Effectively_with_Legacy_Code) |
| 컨테이너 | Quadlet + digest 고정 | [Podman Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html) |
| 의존성 | import-linter 게이트 | [ import-linter  — architecture enforcement](https://github.com/import-linter/import-linter) |

### 5.2 LLM 공급자 아키텍처 (Track A — 포트만 정의)

| 패턴 | 구현 |
|------|------|
| **Protocol** | `LLMPort` (ports/extract.py) — `chat()`, `verify_claim()`, `enrich_fact()`, `rerank()` |
| **Local** | `LocalLLMAdapter` (adapters/driven/llm/local_adapter.py) — 포트 기반 |
| **Factory** | 미구현 — `DEVFORGE_LLM_PROVIDER` 선택으로 대체 (Track B에서 factory 도입 예정) |
| **Feature Flag** | `DEVFORGE_LLM_PROVIDER` env var (Track A: `local`) |
| **DI** | `ExtractPipeline(llm=...)` | 설정 → Adapter 생성 → 주입 |

**API 키**: OpenAI와 Anthropic은 **별도 키**가 필요합니다. 동일 키를 사용하는 것은 OpenRouter/LiteLLM 같은 게이트웨이를 거쳤을 때 가능하며, 이는 `docs/LLM_PROVIDER_PLAN.md`(Track B 별도 문서)에서 논의 예정입니다.

> **Track B(Cloud 공급자)**는 별도 문서(`docs/LLM_PROVIDER_PLAN.md`)에서 계획 및 검토 예정입니다. v1.4 시점에서는 Track A의 LocalProvider 인터페이스만 확정하고 구현합니다.

### 5.3 테스트 전략
| 테스트 종류 | 목적 | 도구 |
|-------------|------|------|
| Characterization | 기존 동작 보존 | pytest + replay 하네스 |
| Replay Fixture | LLM 응답 결정론화 | `tests/fixtures/llm_recordings/` |
| Unit | 단위 검증 | pytest |
| Integration | 통합 검증 | pytest + Docker |
| Architecture | 의존성 방향 | import-linter |
| Shadow E2E | 구/신 대조 | day_cycle_shadow.sh + devforge_shadow DB |

---

## 6. 리스크 (v1.1 표 복원 + 신규 추가)

| 리스크 | 가능성 | 영향 | 완화 | Status |
|--------|--------|------|------|--------|
| **프로덕션 DB 경합** | High | Critical | **섀도 DB 분리 (Phase 1.5)** + replay 하네스 | Partial — replay 적용, 섀도 DB 미구현 |
| **LLM 응답 비재현성** | High | High | **record/replay 하네스 (Phase −1)** | Mitigated (Phase −1) |
| **day_cycle.sh 로직 누락 (455줄)** | Medium | High | **행위 명세 (Phase −1)** + 2주 병렬 검증 | In Progress (Phase 3.5) |
| **데이터 손실 (pipeline_state)** | Low | Critical | **Alembic + expand/contract** (ADR로 규정) | To Do (Phase 0) |
| **기존 시스템 중단** | Medium | Critical | **Feature flag + 섀도 DB + 단계적 전환** | Partial (Phase −1 ~ 3.5) |
| **순환 참조 재발** | Medium | High | **import-linter CI 게이트** | Mitigated (Phase 0) |
| **하드코딩 경로 미해결** | Medium | High | **`Paths` 추상화 (Phase 0)** + 검증 | In Progress (Phase 0) |
| **LLM 공급자 전환 (Track B)** | Medium | Medium | **Feature Flag + 별도 문서화** | Planning (docs/LLM_PROVIDER_PLAN.md) |
| **podman-py rootless** | High | Medium | **비도입 확정** | Mitigated (결정됨) |
| **MCP 도구 인터페이스 변경** | Low | Medium | **도구명 별칭 + 에이전트 회귀 테스트** | To Do (Phase 4) |
| **팀 학습 곡선** | High | Medium | **문서화 + 페어 프로그래밍 + 2인 팀** | Monitoring (전체) |
| **OS root 볼륨 용량 고갈** | Medium | High | **`/opt/ai_data/system-savings` bind offload + `root-volume-daily-clean.timer`** (일일 정리·85% 경고) | Mitigated (2026-09-23, ops — 계획서 범위 외) |

> **v1.1에서 삭제된 Critical/High 리스크 4건 재추가**:
> - 프로덕션 DB 경합 → **replay 하네스**는 적용, **섀도 DB는 미구현** (Status: Partial)
> - 데이터 손실 → **Alembic + expand/contract**으로 부분 해결 (Status: To Do)
> - 기존 시스템 중단 → **Feature flag**으로 완화 (Status: Partial)
> - 순환 참조 → **import-linter**으로 해결 (Status: Mitigated)

---

## 7. KPI

| 지표 | 현재 | 목표 | 측정 방법 |
|------|------|------|---------|
| AI 에이전트 진입점 탐색 시간 | >5분 (50개 파일 중 추측) | <10초 (`devforge --help`) | **측정 프로토콜**: Claude Code 세션 3회, 프롬프트 "이 프로젝트에서 파이프라인 실행 파일을 찾아줘", 10초 이내 파일 1개 특정 |
| LSP 심볼 해결 성공률 | ~60% (순환 참조) | >95% | `pyright --outputjson` + import-linter 0 violations |
| 컨테이너 빌드 시간 | ~3분 (스크립트 복사) | <1분 (wheel 캐시) | CI 로그 |
| 롤백 시간 | 30분 (수동) | **<5분 (Quadlet digest 고정)** | Quadlet `Image=`에 digest 고정 → 재시작 실패 시 자동 롤백 |
| 테스트 커버리지 (신규) | 0% | >80% (unit + integration + characterization) | `pytest --cov` (신규 코드 기준) |
| 타입 힌트 커버리지 | ~20% | >90% | `mypy --strict` |
| 하드코딩 경로 | 40+ | 0 | `grep "/opt/" src/` → 0 (`core/paths.py` 예외) |
| 섀도 검증 diff | N/A | 결정론 0% | Week 8-9 대조 (n≥14 샘플) |

---

## 8. 문서화 계획

| 문서 | 시점 |
|------|------|
| `REFACTORING_PLAN.md` (v1.4) | ✅ 작성 완료 |
| `ARCHITECTURE.md` | ✅ 작성 (2026-09-14) |
| `MIGRATION_GUIDE.md` | ✅ 작성 (2026-09-14) |
| `LLM_PROVIDER_PLAN.md` | ✅ 작성 (Track B 계획, proposed) |
| `API_REFERENCE.md` | Phase 4 완료 |
| `OPERATIONS_GUIDE.md` | Phase 6 완료 |
| `adr/0001-config-priority.md` | Phase 0 완료 (Accepted) |
| `adr/0002-llm-provider-flag.md` | Phase 1 완료 (Accepted) |
| `adr/0003-shadow-db.md` | Phase 1.5 (Proposed) |
| `adr/0004-alembic-migrate.md` | Phase 0 완료 (Accepted) |

---

## 9. 용어 정의

| 용어 | 정의 |
|------|------|
| **Ports & Adapters** | 핵심 로직(Port = Protocol)과 외부 기술(Adapter) 분리 — Cockburn (2005) |
| **Track A / Track B** | Track A: 리팩토링 (핵심), Track B: LLM 공급자 (별도 문서화). 주의: `plans/track-b-migration.md`의 "Track B"는 svc.pod 이관으로 무관 |
| **Characterization Test** | 리팩토링 전 기존 동작 고정 — Feathers, *Working Effectively with Legacy Code* |
| **Shadow DB** | 프로덕션 DB와 분리된 검증 전용 스키마 |
| **record/replay 하네스** | LLM 응답 캡처 → 재생으로 결정론화 |
| **expand/contract** | DB 마이그레이션 전략 — 새 컬럼 추가(additive) 후 단계적 삭제 |

---

## 10. 승인

| 역할 | 이름 | 서명 | 날짜 |
|------|------|------|------|
| Technical Lead | | | |
| DevOps Lead | | | |
| Backend Engineer | | | |

---

> **참고**: 이 문서는 살아있는 문서입니다. 각 Phase 완료 시 실제 구현 내용에 맞춰 업데이트하며, `docs/adr/`에 주요 결정 사항을 별도 기록합니다. **Track B(LLM 공급자)**는 `docs/LLM_PROVIDER_PLAN.md`로 분리하여 별도 검토 및 계획 수립 예정입니다.
