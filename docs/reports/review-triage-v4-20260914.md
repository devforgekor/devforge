# 리뷰 트리아지 v4 — 메타리뷰 L1·L2 최종 조정, Phase 0 Kickoff 확정

> **Status:** record · **Date:** 2026-09-14 · **Owner:** devforge
> **선행:** v1 https://d.pr/97DxrE · v2 https://d.pr/ihZ81J · v3 https://d.pr/koCTH7
> **입력:** v3에 대한 2건(이하 **L1** 상세 비판 / **L2** 종결 선언) + 자체 재분석 + 실측 추가
> **목적:** L1의 12개 갭(G1~G12)을 판정하고, L2의 "종결"을 조건부로 수용하며, 모든 수정을 **Phase 0 Kickoff 스펙으로 통합**한다.

---

## 0. 요약

- **L1(상세 비판):** v3의 방향 수용하나 12개 갭 지적. **전부 타당** → 판정 수용 12 / 부분 2.
- **L2(종결 선언):** 설계 종결·Phase 0 착수 승인. **의도는 수용하나, L1의 갭을 무시하고 "지금 착수"한 것은 시기상조** → 종결은 "L1 수정을 Kickoff에 접은 후"로 조정.
- **핵심 수정(4건):** ① 산출물 6종에 **담당(who)** ② 12툴의 **annotation↔처리방침 분리** + "서버 내부화" 정의 ③ **"diff 0" → 정규화 후 허용오차 내 diff** ④ MCP 순서 **근거 명시** (계약 동결 → [M2 ∥ M1] → M3 → M0/M4).
- **실측 확인:** `/tmp`는 root 디스크(95%) 소재 — G9(로그 경로) 수용. MCP 표준 annotation 4종(`readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint`)은 코드베이스에 미사용 — G2 수용. ADR-0006은 **Proposed** 상태 — G12는 부분 수용.
- **최종: Phase 0 실행 승인.** 단 §4의 Kickoff 스펙으로 착수하고, §5 게이트 통과 전 Phase A/MCP 착수 금지.

---

## 1. L1 12개 갭 판정

| 갭 | 내용 | 판정 | 근거/조치 |
|---|---|---|---|
| G1 | 산출물 6종 who 미지정 → self-approval | **수용** | §4.1 담당 열 신설 |
| G2 | "annotation" 열이 MCP 표준(4종)과 불일치 | **수용** | §4.2 분리. 코드베이스에 annotation 미사용 확인 |
| G3 | "서버 내부화" 모호(계약 제거 vs 내부 흡수) | **수용** | §4.2 정의: Phase A엔 유지, M1·M0 후 제거 |
| G4 | "데이터 diff 0"이 P4의 비결정론 문제 미해결 | **수용** | §4.3 정규화+허용오차 |
| G5 | Observability 임계값 예시일 뿐 | **수용** | §4.4 baseline 후 확정 |
| G6 | H0 diff가 env/볼륨/네트워크/포트 미커버 | **수용** | §4.5 범위 확장 + "차이 목록화" |
| G7 | R7 축별 측정법·합격기준 없음 | **수용** | §4.6 축별 측정법 |
| G8 | 훅 오버헤드 절대값(p95<50ms) | **수용** | §4.7 baseline+상대값 |
| G9 | `/tmp/devforge-hook-errors.log` 재부팅 소실 | **수용** | `/tmp`=root 디스크(95%). `~/.local/state/devforge/`로 |
| G10 | RTO/RPO 소유자 미지정 | **수용** | §4.8 소유자=서비스 소유자(사용자), Phase 0 중 확정 |
| G11 | MCP 순서(M2→M1→M3) 근거 없음 | **수용** | §4.9 근거 명시 |
| G12 | "ADR-0006 반영"이 ADR 불변성과 충돌 | **부분수용** | ADR-0006은 **Proposed**(미수락) → 수정 허용. **수락 후** 변경은 supersede 규칙 |

> L1의 12개 갭은 방향성 오류가 아니라 **정의 정밀도 문제** — 별도 차단 단계가 아니라 Phase 0 Kickoff의 요구사항으로 흡수한다.

---

## 2. L2(종결 선언) 평가

| L2 주장 | 판정 | 근거 |
|---|---|---|
| 설계 단계 종결, 수익 체감 | **수용** | v1→v4 4차 교차검증 완료, 남은 것은 실행 |
| Phase 0 착수 승인 | **조건부 수용** | L1의 G1~G12를 Kickoff에 접은 뒤 착수 |
| Go/No-Go 게이트 3종(경보/리허설/정합) | **수용** | §5에 통합 |
| **L1 갭을 무시하고 즉시 착수** | **기각** | G2·G3·G6·G9는 "실행 중 수정" 시 재작업·재산출 발생 |

> L2의 결론(실행으로)은 옳다. 단 "지금 즉시"가 아니라 **"이 Kickoff 스펙으로"** 옳다.

---

## 3. L1 × L2 조정 — 종결 조건

- **설계 검토 루프 종결.** 더 이상의 메타리뷰는 수용하지 않는다.
- **단, Phase 0 Kickoff(§4)에 L1의 12개 수정이 반영된 산출물을 제출**해야 한다. 이것이 종결 조건.
- 이후부터는 리뷰가 아니라 **Go/No-Go 게이트(§5) 통과 여부**만 판단한다.

---

## 4. Phase 0 Kickoff 스펙 (v4 수정본)

### 4.1 산출물 6종 + 담당(who)

> 단일 소유자(사용자/opc) 시스템이므로 3역할 분리를 이렇게 정의: **작성**=devforge(AI 에이전트) · **검토**=외부 리뷰 에이전트 · **승인**=사용자(서비스 소유자).

| 산출물 | 작성 | 검토 | 승인 | 합격기준 |
|---|---|---|---|---|
| `specs/mcp-contract.json` | devforge | 외부 리뷰 | 사용자 | §4.2 |
| Observability(지표 5종+대시보드) | devforge | 사용자 | 사용자 | §4.4 |
| `docs/ops/rollback-matrix.md` | devforge | 외부 리뷰 | 사용자 | §4.3 |
| provenance 정책 | devforge | 외부 리뷰 | 사용자 | legacy no-op 동작 |
| D5 보안 lifecycle | devforge | 사용자 | 사용자 | 저장·회전·폐기 스크립트 |
| 수치·용어 정합 | devforge | 사용자 | 사용자 | 전 문서 일치 |

### 4.2 mcp-contract.json 스펙 (G2·G3 해소)

- **열 분리:** (a) **처리방침**(disposition: keep / merge→`memory(action,kind)` / 훅전환 M1 / internalize M1 / remove M0)과 (b) **MCP 표준 annotation**(`readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint` 4종만)을 별도 필드로.
- **"서버 내부화" 정의:** `deepdive_session_heartbeat/status`는 **Phase A 시점에는 계약 유지**(클라이언트 무변경 충족). M1(훅) 도입 후 M0에서 allowlist 제거. 계약 파일에 `status: "active → deprecated@M1 → removed@M0"` 명시.
- **alias 라우팅 명시:** merge되는 툴의 라우팅 규칙을 JSON에 포함(예: `obs_write` → `memory` action=save, kind=obs / 인자 매핑표). "alias 매핑"이라는 문구 금지.
- **변경 관리:** 동결 후 변경은 change-request 절차(영향·승인·전파 기간). alias 폐기는 최소 1 사이클(2주) 유예.
- **ADR 반영:** ADR-0006(Proposed)에 12툴 목록과 disposition을 추가해 **수락** 처리. 이후 변경은 supersede 신규 ADR.

### 4.3 Rollback Matrix (G4 해소)

- **"데이터 diff 0" 폐기.** 대신: ① **정규화 규칙 정의**(timestamp·시퀀스·자동생성 컬럼 제외 목록 명시) ② **허용오차 설정**(예: 예상 신규 행 N, 컬럼별 오차 M) ③ **diff 리포트**가 정규화·오차 내 통과 → 합격.
- 리허설 합격 = 롤백 후 health 10분 + 정규화 diff 통과.

### 4.4 Observability (G5 해소)

- Phase 0에서 **baseline 1주 측정** 후 임계값 확정(예: MCP p95 = baseline×1.3, 절대값 금지). 지표 5종은 유지.

### 4.5 H0 diff 도구 (G6 해소)

- 체크 항목 확장: **환경변수·볼륨 마운트 경로·네트워크 모드·포트 바인딩** + 기존(Python path·의존성·권한).
- 수락기준을 **"차이 0" → "차이 목록화 + 영향 평가(무해/주의/차단) 후 수용 판정"** 으로 완화.
- 의존성 pinning = **requirements.txt 버전 + 베이스 이미지 digest** 둘 다.

### 4.6 R7 비교 축 (G7 해소)

| 축 | 측정 방법 | 합격 기준(예) |
|---|---|---|
| 보안 | relay 노출 포트/인증 방식 | 인증 없이 열린 포트 0 |
| 유지보수 | 예상 연간 유지보수 시간 | 폴링 파서가 절반 이하 |
| 롤백 용이성 | 롤백 절차 단계 수 | 폴링: 1단계(파서 제거) |
| 로그인/2FA 커버리지 | 커버 가능 사이트 수 | 폴링 대비 Native 우위 확인 |
| 지연/리소스 | CPU(폴링) vs 메모리(Native) 실측 | 임계 미만 |

### 4.7 훅 오버헤드 측정 (G8·G9 해소)

- 로그 경로: `/tmp` → `~/.local/state/devforge/hook-errors.log` (또는 systemd journal).
- 합격기준: **baseline(현행) 대비 +20% 이하**(절대 ms 금지). baseline을 Phase 0 1주차에 기록.

### 4.8 RTO/RPO (G10 해소)

- **소유자 = 사용자(서비스 소유자).** Phase 0 2주차까지 확정(제안: RTO 4h / RPO 1d). 미확정 시 리스크표에 "미확정" 상태로 명시.

### 4.9 MCP 순서 근거 (G11 해소)

- **계약 동결 → [M2 ∥ M1] → M3 구현 → M0/M4.**
- 근거: M2(obs)와 M1(deepdive)은 **독립 툴 집합** → 병렬 가능. M2가 수집 커버리지 최고가치(자동캡처, 기존 auto_log 확장). **M3(고churn merge −8)는 마지막**에 두어 표면 churn을 최소화. M2는 M3의 **동결된 계약**을 대상으로 구현(재작업 방지).

---

## 5. Go/No-Go 게이트 (L1 4 + L2 3 통합)

Phase A(컷오버) 및 MCP 최적화 착수는 **전부 충족 시에만**:

1. `mcp-contract.json` 생성·검토·승인 + ADR-0006 수락 (§4.2).
2. Observability **경보 발동 테스트** 성공(임계값 baseline 기반).
3. **롤백 리허설 1회 통과**(정규화 diff + health 10분).
4. **훅 오버헤드 baseline 측정** 완료(+20% 기준 확보).
5. **수치·용어 정합 완료**(32/12/25/≈15/9/−8/448/7152 전 문서).
6. RTO/RPO 소유자 확정(또는 미확정 명시).
7. provenance: 신규 100% + legacy 마커(`legacy:pre-2026-09`) no-op 동작 확인.

---

## 6. 최종 판정

**Phase 0 실행 승인 (Kickoff 스펙 첨부).**

- v1(수용 20/부분 8/미수용 6) → v2(미수용 3) → v3(산출물 6종·12툴 열거) → **v4(L1 12갭 반영 + L2 종결 조건 + Kickoff 스펙 확정)**.
- 설계 검토 루프 종결. 이후는 실행과 Go/No-Go 게이트만.
- **다음 행동:** §4.1 표에 따라 `specs/mcp-contract.json`(작성=devforge) → 외부 리뷰 → 사용자 승인 → ADR-0006 수락. 이어 Observability baseline 1주.

*End of v4. 설계 검토의 최종본이며, 이후 리뷰는 Go/No-Go 게이트 판정으로 대체된다.*