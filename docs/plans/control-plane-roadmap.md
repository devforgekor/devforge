# 로드맵 — watchdog → 하이브리드 → 컨트롤 플레인

> Status: **proposed — Stage 1(registry+discovery) 미착수, Stage 2(컨트롤 플레인) 미착수** · Date: 2026-09-11 (현황 2026-09-23) · Owner: devforge
> 전제 진행: watchdog **v2.1(hex 재작성)**은 Phase 2.5 shadow-run — 이는 Stage 1의 *토대*일 뿐 **Stage 1(registry화)이 아니다**.
> Related: `docs/reports/control-plane-registry-research.md`, `docs/reports/p7-gating-design-research.md`, `plans/watchdog-standard-compliance.md`(Gate4 P1–P4 정본), `plans/final-plan.md`(컷오버 정본)
> 관련: `docs/reports/control-plane-registry-research.md`(조사·검증), `docs/reports/mcp-consolidation-applied-20260911.md`(MCP 통합), `docs/reports/p7-gating-design-research.md`(게이팅 원칙)
> 전제: 현재는 **"신뢰성 컨트롤러(watchdog)"** 까지 구현됨. **"조율/거버넌스"는 미구현**.

---

## 0. 현재 상태 (기준선)
- `devforge-watchdog`: `lib/watchdog/config.py`의 **하드코딩 목록**(SERVICE/TIMER/ONESHOT/SYSTEM)을 감시 → self-heal, incident 기록, dead-man's switch, `action_queue` 실행(예: sandbox_verify).
- Deep Dive: 에이전트(7단계) + `deepdive_step_*` 체크포인트(devforge-mcp) + `cli.py research/task`.
- **한계**: desired-state **수렴 아님**, registry 없음, discovery 없음, orphan/SLO/owner 없음, 외부는 관측 밖.

## 1. 두 단계 개요
| | Stage 1 — 하이브리드 | Stage 2 — 컨트롤 플레인 |
|---|---|---|
| 목표 | 감시 목록을 **레지스트리(SSOT)+발견**으로 전환, level-triggered 수렴 | **전체 상태 수렴 + 거버넌스(owner/SLO) + 외부 관측** |
| 범위 | systemd/컨테이너/타이머/포트 (서버 내부) | + 리서치/배포 표현, 외부(Vercel/OCI) 관측 |
| 난이도 | 낮음~중 (기존 orchestrator 재사용) | 중~높음 (정책·경계·감사) |
| 가치 | **80%** (drift·orphan·신규등록·통합상태) | 거버넌스·확장성·외부 통제 |

핵심 원칙(조사 반영): **발견(facts, 기계) + 정책(intent, 사람)**, **level-triggered**, **멱등 조치**, **status는 변화 시에만 write**(hot loop 방지), **단위별 자가치유는 systemd/podman에 위임**.

---

## 2. Stage 1 — 하이브리드

### 2.1 목표
watchdog을 "하드코딩 목록 검사기"에서 **"발견 + 정책 reconciler"** 로 전환. 새 컴포넌트 등록이 **데이터**가 되고, **drift/고아**가 보이며, **통합 상태**가 한곳에 나온다.

### 2.2 최소 조건 (착수 게이트)
1. **사실 소스 접근**: `systemctl list-units/list-timers`, `podman ps`, quadlet 유닛, 리슨 포트 조회 가능(이미 가능).
2. **레지스트리 저장소 1곳**: DB 테이블 `component_registry`(또는 YAML) — SSOT.
3. **정책 스키마 확정**: `{name, type, expected(running|active|idle), policy(restart|kick|alert|none), owner, slo}`.
4. **통합 지점**: 기존 `lib/watchdog/orchestrator.py` 사이클에 discover→diff→act 삽입 가능.
5. **회귀 안전**: 레지스트리가 비면 **기존 config.py 로직 그대로** 동작(병행).
6. **멱등성·hot-loop 가드**: 조치는 멱등(systemctl restart/start), status는 **변화 시에만** 기록.

### 2.3 구현 계획
- **P1 — discovery**: `lib/watchdog/discovery.py` — systemd 서비스/타이머, podman 컨테이너/quadlet, 포트를 열거 → `facts[]`.
- **P2 — registry**: DDL `component_registry` + `docs/specs/schema.sql` 동기화, 정책 오버레이(YAML/DB). CLI `cli.py component list|register|show`.
- **P3 — reconciler**: `lib/watchdog/reconcile.py` — `facts × registry → diff → policy act`(멱등) → status 변화만 기록. orchestrator에 통합, incident 연계.
- **P4 — orphan 감지**: 활성인데 registry 미등록 → 경고(선택: benign 자동 등록 후보 제안).
- **P5 — 통합 상태**: `cli.py status`에 component matrix / 뷰어 `/status` 노출.

### 2.4 수락 기준
- 신규 서비스/컨테이너 추가가 **레지스트리 데이터만으로** 감시 대상이 됨.
- 의도적 중지(desired=idle)와 **장애**가 구분됨.
- **drift**(desired≠actual) 감지·수렴, **orphan 경고** 동작.
- 기존 self-heal·incident **회귀 없음**.

### 2.5 착수 트리거 (언제)
- 감시 대상이 늘어 `config.py` 수동 관리가 **버거워질 때**, 또는
- "등록 누락/ghost component/drift"가 **실제 문제**로 관측될 때.
- 그 전에는 현재 watchdog으로 충분.

---

## 3. Stage 2 — 컨트롤 플레인

### 3.1 목표
Stage 1을 **전 표면으로 확장**하고 **거버넌스(owner/SLO/escalation)** 와 **외부 관측**을 추가. desired-state를 한 곳에서 정의·감사.

### 3.2 최소 조건 (Stage 1 완료 + 아래)
1. **Stage 1 안정화**: registry SSOT가 신뢰 가능(불일치 없음).
2. **경계 정의 문서화**: 제어(in) / 관측(mid) / 범위밖(out). **외부(Vercel/OCI)는 관측만**.
3. **외부 관측 어댑터**: OCI budget/usage, Vercel health, 외부 ping(dead-man's switch).
4. **정책·검증·감사**: action 전 검증(sandbox/permission), 변경 감사 로그.
5. **거버넌스 메타**: owner, SLO, escalation(알림/에스컬레이션 단계).
6. **인터페이스**: CLI/뷰어에서 desired-state 변경 + 이력.

### 3.3 구현 계획
- **외부 관측 컴포넌트**(policy=alert only): OCI 예산/사용량, Vercel/엔드포인트 헬스, 외부 ping.
- **SLO 체크 + escalation**: SLO 위반 시 단계적 알림(minipark4u).
- **감사/이력**: registry 변경·조치 이력 테이블.
- **통합 매트릭스/대시보드**: 제어/관측 경계 표시.
- **오케스트레이션 경계 유지**: 배포/리서치 **파이프라인 자체를 control plane이 돌리지 않음** — registry로 **표현**하고 실행은 `action_queue`/에이전트에 **위임**(단일 실패점·경직 회피; P7 근거).

### 3.4 경계·안전
- 제어 vs 관측 **명시**(외부는 관측만).
- 위험 조치 **human-in-the-loop**, bounded action, rollback.
- `action_queue` 보안 경계 유지(MCP=쓰기, watchdog=실행).

### 3.5 착수 트리거 (언제)
- owner/SLO/감사 요구가 생길 때, 또는
- 외부 자원(Vercel/OCI)을 **통합 상태**로 보고 싶을 때.
- 단일 소규모 서버에선 **과설계 위험** — 명확한 요구 없으면 보류.

---

## 4. 내 의견
- **지금 당장은 둘 다 불필요**하다. 현재 watchdog은 안정·기능적이며, 안전 측면 이득은 미미.
- **Stage 1을 먼저, 데이터 기반으로** 하라: `config.py` → registry + discovery + orphan. 프레임워크 도입이 아니라 **"레지스트리화"** 로 접근(저비용, 되돌리기 쉬움).
- **Stage 2는 거버넌스·확장 요구가 실제로 생길 때만.** watchdog에 오케스트레이션을 얹지 말 것(단일 실패점·경직).
- **경계 원칙**: 계획/조율=에이전트·CLI, 체크포인트=devforge-mcp(`deepdive_step_*`), 신뢰성/실행=watchdog. **세 역할을 섞지 않는다.**
- 결정은 **시점(날짜)이 아니라 트리거(§2.5·§3.5)** 로.

## 5. 리스크
- **과설계**(단일 노드에 컨트롤 플레인) → 최소 조건·트리거로 방어.
- **registry↔현실 drift** → discovery가 SSOT, 정책만 수동(하이브리드).
- **status hot loop** → 변화 시에만 write.
- **역할 혼동**(watchdog에 조율까지) → §4 경계 준수.

## 6. 근거
- `docs/reports/control-plane-registry-research.md` — K8s reconcile, live ontology, service catalog drift, systemd 위임.
- `docs/reports/p7-gating-design-research.md` — 경직 오케스트레이션의 취약성.
