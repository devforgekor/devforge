# 감시·기록 → (수정 | 미실행 실행) 분리 아키텍처 — 종합 보고서

> Status: proposed · Date: 2026-09-23 · Owner: devforge
> Related: `plans/error-record-analysis-design.md`(기록 D2), `reports/incident-issue-pr-loop-audit-20260923.md`(C 트랙 D3), `plans/detection-remediation-implementation-guide.md`, `plans/dataimpulse-watchdog-delegation.md`, `plans/watchdog-standard-compliance.md`, `plans/control-plane-roadmap.md`, `plans/2026-standard-gap-remediation.md`
> **통합 범위**: 본 문서가 **상위(라우팅·거버넌스)**. 기록= D2, 인간/에이전트 조치(C: 이슈→PR)= D3. 한 파이프라인의 3구간.
> 방법: 업계 표준·최근 동향(web) 조사 → 서버 실측 대조 → 아키텍처 제안.

---

## 0. 요약 (TL;DR)

**설계(표준 채택)**: 와치독 = **감시(detect) + 기록(record) 전담**(= 표준의 event producer + incident store). 그 **기록을 트리거**로 **두 컨트롤러**를 둔다 — (A) **오류 수정(fix)**: 실패 복구, (B) **미실행 실행(catch-up)**: 안 돈 작업 실행.
구현은 **업계 표준 패턴**을 따른다: **subject(계층) 라우팅** + **level-based reconciliation** + **governance(backoff/terminal·staleness·escalation·impact)**.

**판정**: **표준과 일치**. 2026 표준(이벤트 기반 self-healing, agentic self-healing 참조아키텍처, K8s controller, runbook automation)은 모두 **"감지/기록 → 라우팅 → 조치"를 분리**하고, 조치를 **오류 복구**와 **상태 수렴(미실행/드리프트 교정)**으로 나눈다.

**핵심 3가지**:
1. **기록 구조화**(`context_jsonb`/`observations` 3계층 구현) + 라우팅 키(`component:event_type`).
2. **"미실행" 판정에는 desired-state 필요** — systemd `Persistent=true`가 일부 커버하나 **조건/assert 실패 oneshot은 재시도 안 함**(표준 갭) → B가 보완.
3. **거버넌스**: 멱등·backoff/terminal·staleness check·escalation·impact 게이트·불변 감사.

> (참고) "prefix로 A/B를 나눈다"는 초기 아이디어는 **subject 계층 라우팅 표준의 한 사례**로 흡수됨(§5.1). 설계 정본은 **표준 패턴**이다.

---

## 1. 설계 요지 (표준 채택)

| 요소 | 내용 | 표준 대응 |
|---|---|---|
| 역할 경계 | 와치독 = **감시 + 기록 전담**(복구/실행 안 함) | event producer + incident store |
| 산출 | 오류 발생 시 **기록**(incident) | CloudEvents `type`(routing 키) |
| 트리거 | 와치독 **기록을 근거**로 발화 | event → rule → routing |
| 컨트롤러 A | **오류 수정**(fix) — 기록된 실패 복구 | remediation(복구) |
| 컨트롤러 B | **미실행 실행**(catch-up) — 안 돈 작업 실행 | desired-state reconcile |
| 컨트롤러 C | **에스컬레이션**(escalate) — **반복 incident → GitHub 이슈 → PR**(인간/에이전트) | runbook/HITL |

> 초기 아이디어("prefix로 A/B 분리")는 **subject 계층 라우팅(§5.1)**으로 흡수. 아래는 **표준 기반 설계**다.
> **C 트랙은 이미 존재**(legacy `scripts/lib/watchdog/incidents.py` → `dev_pipeline`) — 실측·정체는 D3(`reports/incident-issue-pr-loop-audit-20260923.md`). 본 설계는 이를 **라우팅의 한 분기로 편입**한다.

---

## 2. 업계 표준 / 최근 동향 (2026)

### 2.1 이벤트 기반 self-healing (Red Hat)
4요소: **event producer(모니터링)** → **messaging** → **intelligent routing(규칙/메타데이터)** → **remediation playbook(자동화 플랫폼)**. **traceability/observability 필수**.
→ 제안의 "와치독=producer+기록, 트리거=기록, 조치=별도 로직"과 **정확히 대응**.

### 2.2 Agentic self-healing 참조 아키텍처 (arXiv 2608.01955, 2026)
- **detection / RCA / remediation autonomy / governance**를 분리. **결정론 규칙 우선**, 규칙이 못 푸는 모호·신규·교차 장애만 **LLM 에이전트로 escalation**(에이전트별 책임 분리, reason-and-act).
- 오픈 표준 인터페이스(OTel/Prometheus/OpenLineage). "자율성은 lock-in과 상관".
- "detect-only" 플랫폼과 "closed-loop self-healing" 플랫폼을 구분 — **복구는 감지와 별개 역량**.
→ 제안의 A(fix)/B(catch-up) 분리 + 규칙 우선이 표준.

### 2.3 Runbook automation / event-driven automation (Rundeck, StackStorm, Ansible EDA)
- **event → rules engine → workflow/actions**, 콘텐츠=코드, **audit**, **human-in-the-loop**(문제 시 workflow freeze + 인간 에스컬레이션).
- StackStorm: "sensor(이벤트) → trigger → rule → action"; 복구 실패 시 **freeze + PagerDuty**.
→ 제안의 "기록 트리거 → 로직"의 성숙 패턴. 감사·가드레일 내장.

### 2.4 미실행(catch-up) / desired-state 수렴
- systemd `Persistent=true`: 다운타임 중 놓친 실행을 **부팅/복귀 시 따라잡음**. 단 **OnCalendar 한정**.
- **표준 갭**: 조건/assert 실패한 **oneshot은 재시도 안 함**(systemd issue #23696) → "백업이 며칠 못 돎".
- K8s controller 패턴: **desired-state 대비 actual을 reconcile**(drift 교정).
→ 제안의 "미실행 실행"은 **desired-state reconcile + schedule catch-up**에 해당하며, systemd 갭을 보완하는 위치.

### 2.5 감지/결정/복구 분리 (거버넌스)
- OWASP Agentic A03·NIST RMF: **감지·결정·복구 분리**, 분석 로직은 **intent-only**(제안만, 실행권 없음).
→ 제안은 이 분리를 **구조로 강제**하는 형태(권장).

---

## 3. 표준 ↔ 설계 대응

| 표준 | 설계(본 문서) |
|---|---|
| event producer + 기록 | 와치독(감시+기록) |
| incident/event store + CloudEvents `type` | `watchdog_incidents` + `context_jsonb`/`observations`(3계층), `component:event_type` |
| subject/rule 라우팅(NATS) | `prefix.>` 구독(A/B 서로소) — §5.1 |
| level-based reconcile(K8s) | 조치 전 현재 상태 재확인 — §5.2 |
| remediation playbook | **A 컨트롤러(오류 수정)** |
| desired-state reconcile / catch-up | **B 컨트롤러(미실행 실행)** |
| governance(backoff/terminal·staleness·escalation·impact·audit) | §5.3~5.5, §8 |

---

## 4. 서버 현재 상태 매핑

| 구성 | 현재 | 제안에서의 위치 |
|---|---|---|
| 와치독 v2.1(hex) | shadow-run(컨테이너, **P2 대기**) | 감시/기록 계층(단, P2 후 정확) |
| error-record §1 | **구현**(context_jsonb/action_error/3계층) | **기록 계층**의 기반 |
| di-delegation | 구현(감지·기록·알림, 복구 없음) | **detect+record+alert 전담**의 실제 예 |
| control-plane-roadmap Stage1 | 미착수(registry+discovery+reconciler) | **B(catch-up)의 desired-state 기반** |
| watchdog-standard P1~P4 | P1 완료 | 신뢰성/호스트 유닛(감지 정확도) |
| incident→이슈→PR(legacy) | **동작 중**(PR 정체) | **C(escalate) 트랙** — v2 미이식(`reports/incident-issue-pr-loop-audit-20260923.md`) |

> **기록 계약 단일화**: A/B/C 모두 **`context_jsonb`(D2 §1)를 SSOT**로 소비해야 한다. 현재 C(이슈 본문)는 legacy `context`(text)만 사용 → D2 §1 이식 시 통일.

> **중요**: 와치독이 이미 `timer:...:delay "never triggered"`, `oneshot ... failed`를 **기록**한다(실측). 즉 제안의 트리거 신호는 **이미 생성 중**이다.

---

## 5. 아키텍처 제안 (5계층)

```
[1 Detect]   와치독: 상태/스케줄/프로세스/포트 감시 (읽기전용, fails-open)
      │  incident(기록)
[2 Record]   watchdog_incidents(L1) + context_jsonb(L2) + observations(L3)  ← D2(error-record §1)
      │  분류(규칙): {fix | catch-up | escalate | alert-only} + dedup/severity
[3 Route]    prefix(종류) × repeat_count(빈도) → logic   (모호하면 후속 분석: D2 §2)
      │
[4 Act]      A. 오류 수정(fix): 복구 액션(재시작/재기동/재시도) — bounded·멱등
             B. 미실행 실행(catch-up): desired-state 대비 미실행 → 실행
             C. 에스컬레이션(escalate): 반복 incident → GitHub 이슈 → claim → PR   ← D3
      │  검증(verify) + 결과 기록
[5 Govern]   감사(불변 로그) · human-in-the-loop(위험/불명) · 롤백 · 중복방지
```

- **트리거**: [2]의 기록(incident) → [3] 라우팅. 이벤트 기반.
- **역할 분리**: [1]은 절대 조치 안 함(제안대로). [4]는 [1]과 코드 결합 0(데이터 계약만).
- **A/B 분리**: 같은 트리거를 쓰되 **분류로 목적지 분기**.

---

### 5.1 prefix 라우팅 = **subject 계층 패턴** (NATS)

incident의 `dedup_key`(`component:event_type`)를 **계층 subject**로 취급한다: `svc.ebook-watcher.down`, `oneshot.backup.failed`, `timer.sync.delay`.
- **NATS 표준**: `.` 토큰 계층 + 와일드카드(`*`=1토큰, `>`=하위 전체). "고유 subject가 많아지면 **와일드카드+계층 네이밍**"이 정답. **publisher는 목적지를 지정하지 않고**, subscriber가 패턴으로 구독.
- **적용**: A 컨트롤러는 `svc.>`, `llm.>`, `pipeline.>`, `infra.>` 구독. B 컨트롤러는 `oneshot.>`, `timer.>` 구독. **서로소 구독 → A/B 중복 처리 0.**
- **C(escalate)는 두 번째 축**: 종류(prefix)가 아니라 **빈도**(동일 `dedup_key` N회/기간)로 발화. 기존 `TASK_THRESHOLD=3`/7일(legacy)과 정합. 즉 **라우팅 = `prefix → {A|B|alert}` + `repeat_count → C`**(한 표에 두 축).
- **CloudEvents**: `type` 속성이 "routing·observability·policy에 쓰인다" → incident `type` = `prefix.event`가 라우팅 키.

### 5.2 level-based reconciliation (K8s controller 패턴)

- **K8s 표준**: 컨트롤러는 **이벤트가 아니라 desired vs observed 상태를 reconcile**(level-based). 각 컨트롤러는 **하나의 리소스 Kind**를 담당.
- **적용**: A/B 각각 **prefix Kind를 담당**. 조치 전 **실제 상태를 다시 읽어** 판정 → **B의 "이미 실행됨" 문제가 자연 해소**(이벤트가 아니라 현재 상태를 봄).
- `reconcile(Request)`가 재시도 여부를 반환. **retryable → requeue(지수 backoff)**, **terminal → requeue 안 함**(controller-runtime `TerminalError`).

### 5.3 staleness check (K8s v1.36)

- 컨트롤러가 **오래된 캐시로 조치하지 않음**(resource version 비교). 
- **적용**: incident가 오래됐거나 이미 resolve됐으면 **조치 skip** → 중복/엉뚱한 실행 방지.

### 5.4 remediation counter + escalation (Red Hat systemd+Ansible)

- 표준 패턴: **재조치 횟수 카운트**로 단계 상승 — 1회=복구, 3회=추가 검증, **≥4회=인간 티켓**(HITL). systemd path unit/서비스가 콜백으로 자동화 컨트롤러 트리거.
- **적용**: A/B 각 조치에 **attempt counter**(기존 backoff/circuit 재사용) → 임계 초과 시 **에스컬레이션**(자동 조치 중단 + 인간).

### 5.5 impact 분류 (AWS SSM)

- SSM은 조치를 **Mutating / Non-mutating / Undetermined**로 분류해 **preview·승인**에 사용.
- **적용**: 조치에 `impact` 태그 → **Mutating은 승인 게이트**(읽기전용 진단은 무승인). `Undetermined`(외부 스크립트)는 인간 검토.

### 5.6 구현 매핑 (서버 코드, 제안)

기존 자산을 확장(신규 최소):

| 표준 요소 | 서버 구현 |
|---|---|
| subject 라우팅 | `domain/watchdog/routing.py`(신규): `route(component, event_type) -> RouteDecision{logic: fix\|catchup\|alert, kind, impact, terminal}` — 기존 `strategies._PREFIX`를 **라우팅 표**로 승격 |
| A/B 컨트롤러 | `application/`의 2 구독자: A=기존 `RecoveryPort`(systemd_recovery), B=**신규 `CatchupPort`**(oneshot/timer 재실행) |
| level-based | 조치 전 **health checker 재호출**로 현재 상태 확인(이벤트 신뢰 금지) |
| requeue/backoff/terminal | 기존 tracker(`schedule_next_attempt`/circuit) 재사용 + `terminal`(재시도 불가) 표시 |
| staleness check | 조치 전 incident `last_seen_at`/`resolved_at` 확인(오래·해소면 skip) |
| counter/escalation | tracker `fail_count` 재사용 → 임계 초과 시 에스컬레이션(자동 중단 + 알림) |
| impact 게이트 | `RouteDecision.impact`로 Mutating 승인 게이트 |

> **최소 침습**: 신규는 `routing.py` + `CatchupPort` 정도. A는 기존 복구 재사용, B는 **"미실행 판정 + 재실행"**만 추가.

---

## 6. A(오류 수정) vs B(미실행 실행) 경계

| 구분 | A. 오류 수정(fix) | B. 미실행 실행(catch-up) |
|---|---|---|
| **구독(subject)** | `svc.> llm.> pipeline.> infra.>` | `oneshot.> timer.>` |
| 대상 | **실패한 것** 복구(서비스 down, crash) | **안 돈 것** 실행(스케줄 미발화, 조건 실패로 skip) |
| 판정 방식 | **level-based**(조치 전 현재 상태 재확인) | level-based + **실행 이력 확인**(중복 방지) |
| 판정 근거 | incident(down/failed) | **desired-state 대비 미실행**(never-triggered/delay/skip) |
| 예 | `svc.ebook-watcher.down` → restart | `oneshot.backup.failed` → 재실행, `timer.sync.delay` → kick |
| 재시도 | requeue(지수 backoff), **terminal=중단** | 동일 + **counter 임계 시 에스컬레이션** |
| 위험 | 재시작 폭주(backoff/circuit) | **중복 실행**(이미 돈 걸 또 실행) |
| 선행 | incident 기록 | **desired-state registry**(roadmap Stage1) |
| 공통 | 멱등·bounded·verify·audit·staleness check | 동일 |

**C(escalate) 트랙** — A/B와 **직교**: prefix가 아니라 **반복 빈도**(`repeat_count ≥ TASK_THRESHOLD`)로 발화. 조치 = GitHub 이슈 생성(멱등, `auto-safe`) → `dev_pipeline` claim → PR. 소비 기록 = `context_jsonb`(D2). 실측·정체 = `reports/incident-issue-pr-loop-audit-20260923.md`(현재 PR 단계 정체, v2 미이식).

> **겹침 주의**: `oneshot ... failed`는 A(복구=재실행)일 수도 B(미실행 실행)일 수도 있다 → **분류 규칙**으로 단일 목적지 지정(중복 실행 방지). **"이미 실행됐는지" 확인이 B의 필수 전제**.

---

## 7. 미실행 감지의 핵심 (B의 난제)

1. **desired-state 필요**: "무엇이 언제 실행돼야 하는가"의 SSOT(roadmap Stage1 `component_registry` + `{expected, policy}`) 없이는 "미실행"을 판정할 수 없다.
2. **systemd 갭**: `Persistent=true`(OnCalendar)는 다운타임 catch-up 제공, 그러나 **조건/assert 실패 oneshot은 재시도 없음**(#23696) → B가 이 갭을 담당.
3. **중복 방지**: 실행 이력(run id/타임스탬프)으로 "이미 실행" 확인 후에만 catch-up.
4. **정상 skip 구분**: 한도 초과/큐 소진 등 **의도적 skip**은 미실행이 아님(di-delegation의 fails-open 교훈).

---

## 8. 가드레일 (표준 필수)

- **감지=읽기전용**: 와치독은 조치하지 않음(제안 그대로).
- **intent-only 분석**: 분석/라우팅은 제안만, 실행은 승인 게이트 후(OWASP A03).
- **멱등 + bounded**: 재시도 횟수/backoff, circuit breaker(기존 자산 재사용).
- **human-in-the-loop**: 위험/불명/반복 실패 → 인간 에스컬레이션(StackStorm freeze 패턴).
- **불변 감사**: 기록·결정·실행을 append-only(관측성·사후분석).
- **중복 실행 방지**: B의 실행 이력 확인.
- **권한 분리**: MCP=쓰기, 와치독=실행(기존 경계 유지).

---

## 9. 측정 지표

- 감지: 오탐/미탐률(P2 후 재측정), 기록 커버리지 100%.
- A: 자동 복구 성공률, MTTR, 재시작 폭주 0.
- B: catch-up 성공률, **중복 실행 0**, 미실행 잔존 시간.
- 거버넌스: 감사 커버리지, HITL 에스컬레이션 비율.

---

## 10. 단계적 도입

| 단계 | 내용 | 선행 |
|---|---|---|
| S0 | 와치독 감지 정확도 확보 | **P2(호스트 유닛)**, shadow-run 창 만료 |
| S1 | 기록 구조화·분류 태그 | error-record §1(구현됨) + 분류 규칙 |
| S2 | **A(fix)** 일반화 | 기존 recovery 자산 + 승인 게이트 |
| S3 | **B(catch-up)** | **desired-state registry**(roadmap Stage1) |
| S4 | 분석 로직(모호 케이스) | error-analysis-01(후속) |

> S0 없이는 A/B 모두 오탐 위에 세워짐 → **P2가 최우선 선행**.

---

## 11. 출처

- Red Hat — Building practical self-healing IT(event-driven automation 4요소)
- arXiv 2608.01955(2026) — Agentic Self-Healing for Data & AI Pipelines(참조 아키텍처, detection/RCA/remediation/governance 분리)
- CROSS(Springer 2026) — cloud-native automated remediation(계층 분리: 분류/임계/디스패치)
- StackStorm / Rundeck / Red Hat Ansible EDA — event → rule → workflow, audit, HITL
- systemd `systemd.timer(5)`(Persistent=true), systemd issue #23696(조건 실패 oneshot 재시도 갭)
- OWASP Agentic A03 · NIST AI RMF — 감지/결정/복구 분리, intent-only
- NATS — Subjects & wildcards / subject hierarchy / best practices("고유 subject↑ → 와일드카드+계층 네이밍")
- CloudEvents spec — `type` 속성의 routing/observability/policy 용도
- Kubernetes controller · controller-runtime `reconcile` — **level-based reconciliation**, per-Kind 컨트롤러, requeue(지수 backoff)/`TerminalError`
- Kubernetes v1.36(2026-04) — **staleness mitigation**(오래된 캐시로 조치 금지)
- Red Hat — Event-driven remediation with systemd + Ansible(서비스/path unit 트리거, **remediation counter 에스컬레이션**)
- AWS SSM — Remediation impact types(**Mutating / Non-mutating / Undetermined**, preview·승인)
