# DevForge 시스템 Reference Architecture (right-sized plane + loop)

> Status: proposed · Date: 2026-09-24 · Owner: devforge
> Related: `plans/detection-remediation-architecture.md`(D1), `plans/error-record-analysis-design.md`(D2), `reports/incident-issue-pr-loop-audit-20260923.md`(D3), `plans/fitness-functions-heartbeat-drift-guide.md`, `reports/systemic-wiring-gap-analysis-20260924.md`
> 목적: 업계 reference architecture(평면+폐루프)를 **이 단일 호스트 서버에 right-size**해, 흩어진 문서들을 **하나의 뼈대**로 정렬. **구조 변경·도구 도입 없음.**

---

## 0. 요약

- 업계 표준 뼈대 = **평면(plane) + 폐루프(closed control loop)**. 우리는 이를 **systemd/podman/postgres 위에** 구현한다(도구 0 추가).
- 평면: **Data · Intelligence · Control · Execution · Experience/Governance** (+ 보안·관측 cross-cutting).
- 루프: **detect → record → route → act → verify → learn**.

---

## 1. 업계 Reference Architecture (2026)

| 출처 | 뼈대 |
|---|---|
| **CPE**(arXiv 2601.17542, 2026-01) | 4 planes — **Data·Intelligence·Control·Experience** + **Sense–Reason–Act** 폐루프; Control=정책 오케스트레이션, Experience=고위험 human oversight |
| **Agentic Self-Healing**(arXiv 2608.01955) | 8단계 — detect→triage→diagnose→plan→**approve**→remediate→verify→**learn**; deterministic policy engine + **guarded execution**; "human executes via PR" |
| **AIOps**(Gartner/Splunk) | 5 layers — ingestion/normalize → storage → **analytics** → **automation/orchestration** → visualization |
| **3GPP CCL**(2026-08) | Closed Control Loop — 동적 구성·**triggered CCL**·historical CCL·**decision escalation** |
| **Agentic SRE**(Zylos 2026) | 특화 에이전트 — anomaly detection·RCA·remediation·verification |
| **IDP**(Platform Eng./Red Hat) | 5 planes — Developer Control·Integration&Delivery·Resource·Security·Observability; **GitOps-first("code as truth")** |
| **MAPE-K**(autonomic) | Monitor→Analyze→Plan→Execute + **Knowledge** |

**context7 검증**: NATS(계층 subject+wildcard=`*`/`>`) = 라우팅 근거 · Kubernetes("controllers = control loops that move current state closer to **desired state**") = level-based reconcile 근거.

**공통 원칙**: ① 폐루프(sense→decide→act→verify→learn) ② 평면 분리 ③ **deterministic-first, agent-second** ④ **guarded execution**(승인·HITL) ⑤ **학습 루프**(반복 패턴→규칙 승격) ⑥ **점진 채택**("안전한 반복작업 먼저 → closed-loop").

---

## 2. 우리 시스템 매핑 (plane ↔ 문서/구성)

| 업계 plane | 우리 문서/구성 | 상태 |
|---|---|---|
| **Data / Context** | watchdog 기록(D2 §1: `context_jsonb`·3계층), `observations`, `watchdog_incidents` | §1 구현(마이그레이션 미적용) |
| **Intelligence** | D2 §2 **분석 로직**(decision packet·RCA), `model_score` | 후속(error-analysis-01) |
| **Control** | D1 **라우팅(A/B/C)** + **fitness functions** + heartbeat + drift | 설계 |
| **Execution / Act** | **A**(fix)·**B**(catch-up)·**C**(agent issue→PR) | A/B 설계, C legacy(반쪽) |
| **Experience / Governance** | HITL·승인·`INDEX`·`handover`·ADR | 운영 중 |
| cross-cutting | 관측성(OTel 계획)·policy-as-code(계획)·**fitness 검증** | 계획 |
| **루프** | **detect→record→route→act→verify→learn** = D1 §5 5계층 | 설계 |

---

## 3. Right-sized 구현 (엔터프라이즈 → 이 서버)

| 업계 요소 | 엔터프라이즈 | **이 서버(대체)** |
|---|---|---|
| 이벤트 버스 | Kafka / NATS | **Postgres(`watchdog_incidents`) + journald** |
| 컨트롤러 | K8s operators | **systemd 유닛 + 경량 reconciler**(watchdog·`sync-units`) |
| 정책 엔진 | OPA / Kyverno | **fitness 테스트 + AGENTS 규칙** |
| 상태 영속 | Redis / Kafka | **Postgres + JSON state**(`dev_pipeline_state.json` 등) |
| 에이전트 오케스트레이션 | LangGraph / Temporal | **opencode/Claude + `auto_tasks`** |
| 관측성 | OTel collector + backend | structlog + (후속 OTel 경량) |
| 배포 | GitOps(Argo/Flux) | **git + `sync-units.sh`(desired↔live 대조)** |

**원칙**: 표준을 **이 시스템에 맞춰 축소(right-size)**, 시스템을 표준에 맞추지 않는다.

---

## 4. 최근 동향 반영

- **3GPP CCL decision escalation** → D1 **C(escalate)** 트랙과 동일.
- **Agentic SRE**(특화 에이전트) → A/B/C 컨트롤러.
- **Checkpointing(single-node: SQLite/Postgres)** → 우리 Postgres + `pr_created` state(재개 시 완료분 skip).
- **Evaluation as Architecture(LLM-as-judge)** → D2 §2 분석 + `model_score`.
- **Incremental adoption** → fitness guide **F1~F4**.

---

## 5. 비목표 (중요)

- **Kafka/NATS·K8s/Argo/Flux·OPA 도입 금지**(단일 호스트에 과설계).
- **구조 변경 금지**(이미 src-layout+Hexagonal+Quadlet=표준). 적용은 **검증 계층 추가**.
- 시스템을 표준에 끼워 맞추지 않는다.

---

## 6. 채택 로드맵 (점진, 업계 권고 정합)

| 단계 | 내용 | 게이트 |
|---|---|---|
| F1 | 배선·문서 **fitness**(검증 계층) | CI green |
| F2 | **heartbeat**(dead-man's switch) | silent no-op 탐지 |
| F3 | **drift**(desired↔live) + 이식·완결·계약 fitness | OutOfSync 보고 |
| F4 | **A/B/C 실행**(A fix·B catch-up·C agent PR) + aging WIP | 루프 완결 |

> F1은 즉시(창 무관), F2~F4는 shadow-run 창 이후.

---

## 7. 출처

- CPE(arXiv 2601.17542, 2026-01), Agentic Self-Healing(arXiv 2608.01955), AIOps(Gartner/Splunk), 3GPP CCL(2026-08), Agentic SRE(Zylos 2026), IDP(Platform Engineering/Red Hat), MAPE-K
- context7: NATS(subjects/wildcards), Kubernetes(controller/reconcile)
- 내부: D1/D2/D3, `fitness-functions-heartbeat-drift-guide`, `systemic-wiring-gap-analysis`
