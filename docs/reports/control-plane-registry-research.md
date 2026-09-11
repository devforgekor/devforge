# 보고서 — 서버 컴포넌트 레지스트리 / 컨트롤 플레인 설계 조사

> Status: record · Date: 2026-09-11 · Owner: devforge · Related: `docs/plans/control-plane-roadmap.md`
> 작성: 2026-09-11 · 목적: "watchdog이 전체를 컨트롤"이라는 원 의도를 어떻게 구현하는 것이 표준인지 웹 조사·검증.
> 후속 로드맵: `docs/plans/control-plane-roadmap.md` (Stage 1 하이브리드 → Stage 2 컨트롤 플레인, 최소 조건·구현 계획).
> 관련: `docs/watchdog-comprehensive-audit.md`(원 계획), `docs/system-architecture.md` §4, `lib/watchdog/*`

---

## 1. 문제 정의
- 원 계획(`watchdog-comprehensive-audit.md`, 2026-09-08): watchdog이 서비스·컨테이너·타이머·LLM·파이프라인 **전체를 감시·복구**.
- 현실: 감시 대상이 `config.py`의 **수동 목록**(SERVICE/ALERT/TIMER/ONESHOT/SYSTEM)에 흩어져 있어 **신규 계층(Vercel 뷰어·OCI·portal)이 등록에서 누락** → "전체 컨트롤"이 아니라 "일부만".
- 본 보고서가 답하는 것: (a) 내 제안(SSOT 레지스트리 + 정책 + 컨트롤 플레인)이 표준인가, (b) 더 나은 방법이 있나, (c) 이 서버에 맞는 구현은?

## 2. 내 원 제안(요약)
`{name, type, expected, health_check, control_policy(restart|kick|alert|none), owner, slo}` **컴포넌트 레지스트리(SSOT)** 를 두고, watchdog/CLI/뷰어가 이를 읽어 감시·복구·표시. 외부(Vercel/OCI)는 관측만.

## 3. 웹 조사 — 지배적 패턴과 대표 구현

### P1. 제어 루프 / 조정(Reconciliation) — **업계 표준 1순위**
출처: [Kubernetes Controllers](https://kubernetes.io/docs/concepts/architecture/controller/)
- 컨트롤러 = **제어 루프**: `observe → diff(desired vs actual) → act → repeat`.
- **Level-triggered**(상태 기반) vs Edge-triggered(이벤트 기반): 이벤트를 놓쳐도 다음 reconcile이 교정 → **미스 이벤트·크래시·재시작에 안전**.
- `.spec`=desired(사용자), `.status`=observed(컨트롤러), **동작은 idempotent**, 상태는 관측에서 도출.
- 함정: **status hot loop**(매 reconcile마다 status write → watch → 재reconcile). 값이 바뀔 때만 write.
- 참고: [level-triggered 설명](https://www.golinuxcloud.com/desired-state-vs-actual-state-kubernetes/)

### P2. 선언적 desired-state + 외부 reconciler (GitOps / Crossplane / Operator)
출처: [Infrahub meets K8s](https://exa.ai/library/publication/p1zwrlxghhs), [Crossplane 리뷰](https://doi.org/10.21275/sr25515112736)
- 중앙 **인벤토리(SoT)** 에 desired를 정의하고, 컨트롤러가 **drift 자동 교정**.
- Crossplane/ArgoCD: 클러스터 **밖** 리소스까지 같은 선언 모델로 조정.
- 핵심: **drift detection + eventual consistency**.

### P3. 서비스 카탈로그 / CMDB (레지스트리 계층)
출처: [Cycloid 2026 비교](https://www.cycloid.io/blog/service-catalog-tools-...), [SRE School 서비스 카탈로그](https://sreschool.com/blog/service-catalog/)
- 카탈로그 = **owner·의존성·SLO·프로비저닝/정책 훅**의 거버넌스 레지스트리. CMDB=자산/구성항목(ITSM 관점).
- 구조 요소: Catalog API + **metadata store** + provisioner + **policy engine** + portal/CLI.
- 경고: **선언적 카탈로그는 drift/부패**(대략 1분기면 stale) — 최대 함정.

### P4. Live Ontology (관측 기반 레지스트리) — **신흥 대안**
출처: [Service Catalog vs Live Ontology (SixDegree)](https://sixdegree.ai/blog/service-catalog-vs-live-ontology)
- 카탈로그=사람이 선언(느리고 부패). **live ontology=인프라에서 지속 발견** → 현실을 반영(no YAML).
- **하이브리드가 최선**: 기계가 사실(fact)을 발견 + 사람이 **intent/정책/컴플라이언스**만 얹음.
- 부가: "backfill이 **소유자 없는 orphan**을 드러낸다."

### P5. 로컬 자가치유 기본기 (systemd / podman)
출처: [podman-container-unit](https://docs.podman.io/en/latest/markdown/podman-container.unit.5.html), [Red Hat podman healthcheck actions](https://www.redhat.com/en/blog/podman-edge-healthcheck), [podman-auto-update](https://docs.podman.io/en/latest/markdown/podman-auto-update.1.html)
- `Restart=always/on-failure`, `WatchdogSec`(+`sd_notify READY/WATCHDOG`), `HealthCmd`+`HealthOnFailure=kill|restart`+`Notify=healthy`, `podman auto-update`(실패 시 **롤백**).
- = 단위별 자가치유는 **이미 플랫폼에 내장**. 크로스-유닛 조정은 별도 컨트롤 플레인이 필요.

### P6. 통합 데이터 기반(SoT)
출처: [OpsDB Design](https://doi.org/10.5281/zenodo.20004908)
- "운영 현실의 **single source of truth**"(설정·관측 캐시·정책·이력) + 앞단 API가 인증/검증/변경/감사 경계.

## 4. 검증 (공식 문서 교차확인)
| 주장 | 검증 결과 |
|---|---|
| "watchdog처럼 주기 감시 = 제어 루프" | ✅ K8s 공식 문서의 controller 정의와 동형(observe→diff→act). 단 **idempotent + level-triggered** 규율 필요 |
| "단일 레지스트리(SSOT)에 정책을 담는다" | ✅ Crossplane/Infrahub/OpsDB 모두 "중앙 SoT + reconciler" 패턴 |
| "수동 카탈로그는 곧 stale" | ✅ SixDegree/Cycloid 분석: YAML 선언형은 drift 구조적 → **관측 기반 발견이 보완재** |
| "self-heal은 systemd/podman이 1차 담당" | ✅ `Restart`/`WatchdogSec`/`HealthOnFailure`/`auto-update rollback` 공식 문서 확인 |
| "status 매 cycle write는 위험" | ✅ K8s reconcile 문서의 **status hot loop** 안티패턴 |
| "레지스트리에 없는 활성 유닛 = 위험(orphan)" | ✅ service catalog 문서: backfill이 orphan을 드러냄 |

## 5. 결론 — 더 나은 방법 & 최종 권고
**내 원 제안은 방향이 맞다(중앙 SoT + 정책 + 컨트롤 플레인).** 다만 웹 조사상 **개선할 4가지**가 있습니다.

1. **정적 카탈로그가 아니라 "관측 기반 발견 + 정책 오버레이"** (Live Ontology + Catalog 하이브리드).
   - 사실(서비스/컨테이너/타이머/포트)은 `discover`로 **자동 도출**, 사람은 **intent(desired)·정책·소유자**만 지정.
   - `watchdog-comprehensive-audit`에서 "등록 누락"이 반복된 근본 원인 = 정적 목록. → **발견을 SSOT로**.
2. **K8s식 규율 채택**: 컨트롤러(루프)는 **level-triggered**, 동작 **idempotent**, status는 **변할 때만 write**, 주기적 resync.
3. **단위별 자가치유는 systemd/podman에 위임**(Restart/WatchdogSec/HealthOnFailure), watchdog은 **크로스-유닛 reconciler** 역할(이미 그렇게 진화 중: self-heal+incident).
4. **경계 명시**: 외부(Vercel/OCI 계정/예산)는 **관측 전용**(헬스 ping·예산 조회), 제어하지 않음. 그리고 **"활성인데 레지스트리에 없는 유닛" 경고**(orphan).

→ 즉 결론은 **"정적 SSOT 레지스트리"보다 "발견 기반 desired-state + reconciler(제어 루프)"** 가 더 낫고, 이는 표준(K8s/GitOps)과도 일치합니다.

## 6. 이 서버 적용 설계 (경량, systemd+podman 전제)
- **컴포넌트 레지스트리(2계층)**:
  - 사실(fact) 계층: `discover_daemon/containers/timers/ports` (자동, 재생성).
  - 정책(intent) 계층(수동 YAML): `{name, desired(running|active|idle), policy(restart|kick|alert|none), owner, slo}`.
  - 병합 뷰 = 레지스트리. **미등록 활성 유닛**은 자동 경고.
- **Reconciler = watchdog 루프**(기존 것 확장): level-triggered 폴링, 동작 idempotent(`systemctl restart`/`start`는 멱등), status는 change 시에만 기록.
- **상태 노출**: `cli.py status` + 뷰어 `/status`가 **레지스트리 매트릭스**. incident가 감사 이력.
- **외부 관측**: Vercel viewer 헬스 ping, OCI budget/usage 체크를 **alert-only 컴포넌트**로 등록.
- **경계 문서화**: 제어(in)/관측(mid)/범위밖(out).

## 7. 리스크 / 주의
- **status hot loop**: 매 cycle status write 시 재현 루프 → 변화 시에만.
- **drift**: 정책(수동)과 사실(자동)이 어긋날 수 있음 → 주기 resync + orphan 경고.
- **재발 방지(Prevent)**: 발견 계층이 있으면 신규 서비스도 **자동 노출**, "등록 누락" 구조 제거.
- **과설계 금지**: 단일 노드에 K8s/Crossplane 도입은 과함 → 위 경량 모델로 동일 원리 달성.

## 8. 출처
- Kubernetes Controllers (kubernetes.io) — 제어 루프/desired-actual/level-triggered
- golinuxcloud — reconcile loop, status hot loop, idempotency (2026)
- Infrahub meets K8s (exa) — 중앙 인벤토리 + Operator reconcile
- Crossplane white paper (2025) — 선언적 외부 리소스 조정
- Cycloid Service Catalog 2026 / SRE School Service Catalog — 카탈로그/CMDB/필수 요소·함정
- SixDegree — Service Catalog vs Live Ontology (관측 기반 발견·하이브리드)
- Podman container unit / podman-auto-update / Red Hat healthcheck actions — 로컬 자가치유 기본기
- OpsDB Design (Zenodo 2026) — 운영 현실의 단일 진실 소스
