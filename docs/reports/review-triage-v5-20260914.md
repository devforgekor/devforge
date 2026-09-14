# 리뷰 트리아지 v5 — 최종 실행 스펙 (Phase 0) · 리뷰 루프 종결

> **Status:** record · **Date:** 2026-09-14 · **Owner:** devforge
> **선행:** v1 https://d.pr/97DxrE · v2 https://d.pr/ihZ81J · v3 https://d.pr/koCTH7 · v4 https://d.pr/RZygjB
> **입력:** v4에 대한 2건(이하 **P1** 상세 비판 / **P2** 승인+주차계획) + 자체 재분석
> **목적:** P1의 H1~H12를 판정하고 P2의 실행 계획(JSON 스키마·주차 계획)을 채택하여, **Phase 0 실행 스펙을 확정**한다. 본 문서가 설계 검토의 최종본이다.

---

## 0. 요약

- **P1(상세 비판):** H1~H12 지적. **11개 수용, 1개 수정(H8은 순환 아님·선형 체인)**, 5개 권고 전부 채택.
- **P2(승인):** Phase 0 승인 + Week-1 계획 + `mcp-contract.json` JSON 예시. **채택**(스키마로 정식화). 단 "게이트 7종 모두 객관적"은 과잉 — 게이트 6의 로프홀(미확정 명시)은 P1이 지적한 대로 수정.
- **실측 반영:** `/tmp`=root 디스크(95%) · ADR-0006=Proposed · MCP annotation 4종 미사용 — 유지.
- **핵심 확정:** ① Phase 0 **2주** 일정 ② 계약 JSON **스키마** 정의 ③ 게이트 **선형 체인 + 예외 경로** ④ R7 **대칭 지표** ⑤ 게이트 6 **로프홀 제거**.
- **최종: Phase 0 실행 승인.** 본 문서로 리뷰 루프 종결. 이후는 게이트 판정 + 예외 경로만.

---

## 1. P1·P2 판정

### 1.1 P1 — H1~H12

| 갭 | v5 판정 | 조치 |
|---|---|---|
| H1 Phase 0 기간 미명시 | **수용** | §2.1 2주 마일스톤 |
| H2 mcp-contract.json 스키마 미정의 | **수용** | §2.2 JSON Schema |
| H3 §4.3 허용오차 확정 절차 부재 | **수용** | §2.3 baseline 후 확정 |
| H4 R7 측정이 양 대안 모두에 비대칭 | **수용** | §2.4 대칭 지표 |
| H5 게이트6 "미확정 명시" 로프홀 | **수용** | §2.5 RTO/RPO 필수 확정 |
| H6 M2∥M1 훅 스크립트 충돌 | **수용** | §2.6 순차 커밋·파일 소유 |
| H7 외부 리뷰 에이전트 정체·일정 | **수용** | §2.7 본 루프 에이전트, SLA |
| H8 게이트2·3 순환 의존 | **수정** | **선형 체인** — 공통 선행(baseline)이며 순환이 아님(§2.10) |
| H9 RTO/RPO 단일서버 현실성 | **수용** | §2.5 복원속도 테스트로 검증 |
| H10 H0 영향평가 판정 기준 부재 | **수용** | §2.8 무해/주의/차단 정의 |
| H11 "메타리뷰 종결"과 게이트 판정 혼동 | **수용** | §2.11 "설계 리뷰 종결, 실행 게이트+예외 경로로 전환" |
| H12 §4.4·4.7 baseline 중복 | **수용** | §2.9 단일 baseline(Week 1 공유) |

### 1.2 P2 — 평가와 채택

| P2 주장 | 판정 |
|---|---|
| v4 = 실행 직전 final polish | 수용 |
| Week-1 계획(Day별) | **채택** — §2.1로 정식화 |
| `mcp-contract.json` JSON 예시 | **채택** — §2.2 스키마로 정식화 |
| 게이트 7종 "모두 객관적" | **기각(부분)** — 게이트 6 로프홀 간과(→ §2.5), 게이트 2·3 선행 의존 미표시 |
| 설계 리뷰 세션 종료 | 수용 — 단 "실행 게이트+예외 경로로 전환"으로 명명(§2.11) |

> P2가 P1의 H1~H12를 무시한 것은 갭이지만, 실용적 실행 계획(스키마·일정)은 그대로 채택.

---

## 2. Phase 0 실행 스펙 (v5)

### 2.1 기간·마일스톤 (H1)

**Phase 0 = 2주.** 주차별 마일스톤:

| 주차 | 마일스톤 |
|---|---|
| W1 D1-2 | `mcp-contract.json` 초안 + ADR-0006 개정안 |
| W1 D3-7 | **공유 baseline 측정**(§2.9: turns률·MCP p95·훅 오버헤드·day_cycle 단계·백업 복원속도) |
| W2 D1-2 | 임계값·허용오차 확정(baseline 기반) + **RTO/RPO 확정** |
| W2 D3 | Observability 경보 발동 테스트 |
| W2 D4 | 롤백 리허설 1회(정규화 diff + health 10분) |
| W2 D5 | 수치·용어 정합 + 외부 리뷰 1라운드 |
| W2 D6-7 | 승인·게이트 판정 |

### 2.2 `specs/mcp-contract.json` JSON Schema (H2)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["version", "contract_id", "status", "tools", "alias_retention_days", "change_request"],
  "properties": {
    "version": { "type": "string", "pattern": "^[0-9]+\\.[0-9]+$" },
    "contract_id": { "type": "string" },
    "status": { "enum": ["draft", "frozen", "superseded"] },
    "tools": {
      "type": "array",
      "minItems": 12,
      "items": {
        "type": "object",
        "required": ["name", "disposition", "lifecycle"],
        "properties": {
          "name": { "type": "string" },
          "disposition": { "enum": ["keep", "merge", "hook", "internalize", "remove"] },
          "merge_target": { "type": "string" },
          "arg_mapping": { "type": "object" },
          "lifecycle": { "type": "string", "description": "예: active -> deprecated@M1 -> removed@M0" },
          "annotations": {
            "type": "object",
            "properties": {
              "readOnlyHint":    { "type": "boolean" },
              "destructiveHint": { "type": "boolean" },
              "idempotentHint":  { "type": "boolean" },
              "openWorldHint":   { "type": "boolean" }
            }
          }
        }
      }
    },
    "alias_retention_days": { "type": "integer", "minimum": 14 },
    "change_request": {
      "type": "object",
      "required": ["procedure", "notice_days"],
      "properties": {
        "procedure": { "enum": ["impact_analysis -> approval -> notice"] },
        "notice_days": { "type": "integer", "minimum": 7 }
      }
    }
  }
}
```

- 검증: JSON Schema validator 통과 + `opencode.json` allowlist와 이름 diff=0 + annotation 4종 외 필드 금지(disposition·lifecycle은 별도).
- 12툴 + disposition은 v3 §3.1 확정본 사용.
- **ADR-0006(Proposed) 개정 → Accepted.** 이후 변경은 supersede 신규 ADR. (G12 = 조건부 수용 확정)

### 2.3 허용오차 확정 절차 (H3)

- §4.4와 동일: **W1 baseline 1주 측정 후 W2 D1-2에 확정**. 허용오차 N·M은 baseline 기반 산출(예: 신규 행 수의 1% + 정적 상수). 절대 임의값 금지.

### 2.4 R7 대칭 지표 (H4)

| 축 | 대칭 지표 (양 대안 모두 적용) | 합격 기준 |
|---|---|---|
| 보안 | **외부 접근 가능 경로 수** (폴링=로컬 파일 0, Native=relay 포트 N) | 비교 후 명시, 인증 없는 경로 0 |
| 유지보수 | 연간 예상 유지보수 시간(추정) | 폴링이 절반 이하 |
| 롤백 | 롤백 단계 수 + 실행 시간 | 양쪽 명시(폴링=1단계, Native=N단계) |
| 로그인/2FA | 커버 가능 **대상 사이트 목록**(양쪽 작성) | Native 우위 여부 판정 |
| 지연/리소스 | CPU%(폴링) vs RSS MB(Native) 실측 | 임계 미만 |

### 2.5 게이트 6 로프홀 제거 + RTO/RPO (H5·H9)

- **"미확정 명시" 허용 삭제.** RTO/RPO는 W2 D1-2까지 **확정 필수**. 미확정 시 Phase A 보류.
- 제안값 RTO 4h/RPO 1d는 W1 **백업 복원 속도 테스트**(실데이터 복원 시간 실측)로 검증 후 확정. 소유자=사용자.

### 2.6 M2∥M1 훅 충돌 조정 (H6)

- M1(deepdive lifecycle = SessionStart/PromptSubmit 훅)과 M2(obs = PostToolUse 훅)는 **훅 타입이 다름**. 단 공통 기반(`lib/observation`)은 공유.
- **조정:** 단일 브랜치에서 **순차 커밋**(공통 기반 먼저, 훅 스크립트는 파일별 소유자 명시), 병렬은 검증/측정 업무로만.

### 2.7 외부 리뷰 에이전트 = 본 루프 (H7)

- **정체:** 이 검증 루프에서 사용한 AI 에이전트(deepseek·qwen 계열 및 후속) — 별도 도입 없음.
- **SLA:** 산출물 1건당 1라운드, 48시간 내 응답, 수정권 1회. W2 D5에 예약.

### 2.8 H0 영향평가 판정 기준 (H10)

- **무해** = 동작 동등(차이 있으나 기능·결과 동일) · **주의** = 동작 차이·기능 동등 · **차단** = 기능 상실 또는 보안 영향.
- **차단 1건 이상 = H0 불통과.** 판정자 = 사용자.

### 2.9 baseline 단일화 (H12)

- §4.4(Observability)·§4.7(훅 오버헤드)·§2.3(허용오차)·§2.5(복원속도)를 **W1 D3-7의 단일 측정 기간**으로 통합. 중복 측정 없음.

### 2.10 게이트 = 선형 체인 + 예외 경로 (H8·H11)

- **선형 체인** (순환 아님): 계약 승인 → baseline 완료(임계값·허용오차·RTO/RPO 확정) → 경보 발동 테스트 → 롤백 리허설 → 수치 정합 → provenance 동작.
- **예외 경로:** 실측이 예상과 달라 기준 조정이 필요하면 "기준 조정 요청" 제출 → 외부 리뷰 1회 → 사용자 승인 → 재측정. (게이트 판정이 새 리뷰임을 인정하되 경량화)

### 2.11 리뷰 루프 종결 정의 (H11)

- **"설계 리뷰 종결, 실행 게이트 + 예외 경로로 전환."** 메타리뷰(전체 계획 재검토)는 종결. 게이트 판정과 예외 경로는 유지.

### 2.12 ADR-0006 (G12 확정)

- **조건부 수용:** Proposed 상태에서 12툴+disposition 추가 개정 → **Accepted** 전환. 이후 변경은 supersede.

---

## 3. Go/No-Go 게이트 v5 (6개, 선형)

1. **계약 승인** — JSON Schema 검증 + allowlist diff=0 + ADR-0006 Accepted.
2. **baseline 완료** — W1 측정 + 임계값·허용오차·RTO/RPO 확정.
3. **경보 발동 테스트 성공** — baseline 기반 임계값에서 1건 발동.
4. **롤백 리허설 1회 통과** — 정규화 diff(허용오차 내) + health 10분.
5. **수치·용어 정합 완료** — 32/12/25/≈15/9/−8/448/7152 전 문서.
6. **provenance 동작** — 신규 100% + `legacy:pre-2026-09` no-op 확인.

전부 통과 시에만 Phase A/MCP 착수. 불통과 항목은 예외 경로(§2.10)로 조정.

---

## 4. 최종 판정 및 종결 선언

**Phase 0 실행 승인 — v5 스펙으로 착수.**

- v1(문제 제기) → v2(판정 정교화) → v3(실행 산출물) → v4(L1 12갭) → **v5(H1~H12 + P2 스펙 채택, 실행 스펙 확정)**.
- P1의 H1~H12가 **전부 실행 스펙에 반영**되어, 게이트는 6개 선형 체인 + 예외 경로로 운영 가능.
- **리뷰 루프 종결.** 이후 추가 문서/요청은 (a) Phase 0 실행 산출물, (b) 게이트 판정, (c) 예외 경로 기준 조정 요청 — 셋 중 하나여야 한다.
- **다음 행동:** W1 D1부터 §2.1 마일스톤에 따라 `specs/mcp-contract.json` 초안 작성(스키마 §2.2) → ADR-0006 개정 → baseline 측정.

*End of v5 — 설계 검토 최종본. 실행(Phase 0)은 이 스펙으로 진행한다.*