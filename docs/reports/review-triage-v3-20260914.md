# 리뷰 트리아지 v3 — 메타리뷰 반영, 실행 단계 확정

> **Status:** record · **Date:** 2026-09-14 · **Owner:** devforge
> **선행:** v1 `review-triage-package-20260914.md` (https://d.pr/97DxrE) → v2 `review-triage-v2-20260914.md` (https://d.pr/ihZ81J)
> **입력:** v2에 대한 메타리뷰 2건(이하 **M1**, **M2**) + 자체 재분석 + 실측 추가
> **목적:** 메타리뷰의 "판정은 옳으나 실행이 불가능한 게이트" 비판을 수용하여, **Phase 0 산출물을 구체화**하고 **12툴 계약을 열거**하여 설계→운영 단계로 전환한다.

---

## 0. 요약

- **M1(조건부 승인)**·**M2(실행 승인)** 모두 v2의 contract-first·H0/H 분리를 정석으로 인정.
- M1의 핵심 비판: *"N1~N7은 '무엇을' 수준이고 '언제·누가·어떻게'가 없다 — 미완성 체크리스트가 될 위험."* → **수용**. v3에서 Phase 0 산출물을 정의.
- M1·M2가 공통 지적한 **12툴 계약 미열거** → **이 보고서에서 즉시 해결(§3.1 열거)**.
- M2의 수치 제안(RTO 4h·RPO 1d·추론 12Gi 하드리밋)은 **근거 없는 기본값** → 후보로만 수용, 실측 후 결정(§4).
- **최종: 조건부 실행 승인.** Phase 0 게이트 산출물이 v3에서 정의되었으므로 착수 가능. 컷오버 A/MCP 착수는 게이트 통과 후.

---

## 1. 메타리뷰 요약

### 1.1 M1 (조건부 승인 — 실행 갭 지적)
- contract-first는 v1·V2 입장을 통합한 가장 값진 추가. V1-Q3 오류 정정 정확.
- **갭:** ① 계약 동결의 **산출물(포맷)·시점·변경 관리·alias 폐기 일정** 미정의 ② H0의 **stub baking 회피 검증**(마운트↔이미지 동작 차이) 부재 ③ N1~N7에 when/who/how 부재 ④ §7에서 **수치 정합이 Phase 0이 아님** ⑤ **12툴 목록 여전히 미열거** ⑥ F9는 "운영 중"만 확인, **오버헤드 미측정** ⑦ R7 비교 축 미정의.
- 실행 조건 3: Phase 0 산출물 구체화 / 12툴 열거 / H0 수락기준.

### 1.2 M2 (실행 승인 — 실행 가이드 제공)
- contract-first·H0/H 분리를 "Architecture Mediation"으로 평가. 검증자 오류 반박 정확.
- §8 남은 9건을 실행 과제로 승격하고 가이드 제시: legacy 마커 no-op 처리, 비즈니스 지표(turns/시간, MCP p95), OCI Budgets+OpenRouter hard limit, **RTO 4h/RPO 1d**, **추론 메모리 12Gi 하드리밋+oom_score_adj**.
- 실행 조건 4: MCP 순서 명시 / Phase 0 게이트 / 용어 정합(32·12·25) / 리스크 문서화.

---

## 2. 메타리뷰 주장 판정

| # | 주장(출처) | 판정 | 근거 |
|---|---|---|---|
| T1 | contract-first = API 마이그레이션 정석 (M1·M2) | **수용** | §2.3 재분석과 일치 |
| T2 | 계약 동결 산출물·변경관리·alias 폐기 미정의 (M1) | **수용** | §3.2로 해결 |
| T3 | H0 마운트↔이미지 동작 차이 검증 부재 (M1) | **수용** | §3.3 수락기준에 diff 도구 포함 |
| T4 | N1~N7 when/who/how 부재 (M1) | **수용** | §3.2 산출물 테이블로 해결 |
| T5 | 수치 정합은 Phase 0 전제 (M1) | **수용** | §7 순서 수정: 수치 정합 → Phase 0 게이트 |
| T6 | 12툴 목록 열거 필요 (M1) | **수용+실행** | §3.1 |
| T7 | F9는 오버헤드 미측정 (M1) | **수용** | §3.5 Phase 0 측정 항목 |
| T8 | R7 비교 축 정의 필요 (M1) | **수용** | §3.4 |
| T9 | SBOM은 H에서 필수 (M2) | **수용** | H 교체 시 필수로 승격 |
| T10 | RTO 4h/RPO 1d (M2) | **부분수용(후보)** | §4 — 소유자 확정 필요 |
| T11 | 추론 12Gi 하드리밋 (M2) | **보류(후보)** | §4 — 실측과 상충 가능 |
| T12 | oom_score_adj 조정 (M2) | **수용** | 일반 원칙, 실측 후 수치화 |
| T13 | "더 이상 메타리뷰 불필요" (M2) | **수용** | 수익 체감 — 남은 건 실행 |

---

## 3. 지적 갭 → 실행 산출물 (v3에서 정의)

### 3.1 12툴 계약 목록 (이제 열거 — ADR-0006/계약 파일에 반영할 정본)

opencode가 devforge-mcp에서 로드하는 유효 계약 12툴 (`~/.config/opencode/opencode.json` 기준):

| # | 툴 | 성격 | annotation(제안) |
|---|---|---|---|
| 1 | `deepdive_step_enter` | 라이프사이클 | → 훅 전환 대상(M1) |
| 2 | `deepdive_step_exit` | 라이프사이클 | → 훅 전환 대상(M1) |
| 3 | `deepdive_session_heartbeat` | 라이프사이클 | → 서버 내부화 |
| 4 | `deepdive_session_status` | 라이프사이클 | → 서버 내부화 |
| 5 | `deepdive_verify_sandbox` | 격리 검증 | keep + `destructiveHint` |
| 6 | `obs_write` | 관찰 기록 | → `memory(action=save,kind=obs)` merge(M2·M3) |
| 7 | `obs_search` | 관찰 검색 | → `memory(action=search,kind=obs)` merge(M3) |
| 8 | `search_turns` | 대화 검색(FTS) | → `search_turns(mode=fts)` merge(M3) |
| 9 | `search_similarity` | 의미 검색(RRF) | → `search_turns(mode=hybrid)` merge(M3) |
| 10 | `mem_save` | 기억 저장 | → `memory(action=save,kind=mem)` merge(M3) |
| 11 | `mem_search` | 기억 검색 | → `memory(action=search,kind=mem)` merge(M3) |
| 12 | `get_conversation` | 대화 조회 | keep |

> 이 목록이 없으면 contract-first도, Phase A 구현 범위도 확정 불가. **Phase 0 첫 산출물로 확정.**

### 3.2 Phase 0 산출물 정의 (산출물·포맷·합격기준)

| 산출물 | 포맷 | 내용 | 합격기준(언제 완료인가) |
|---|---|---|---|
| **MCP 계약 파일** | `specs/mcp-contract.json` | 12툴 + 기존 5툴 + alias 매핑 + readOnly/destructive/idempotent annotation + **변경 관리 절차** + **alias 폐기 일정(최소 1 사이클/2주)** | machine-parseable, `opencode.json` allowlist와 이름 diff=0, 컷오버 전 금고화 |
| **Observability** | 로그 집계 + 대시보드 | 지표: (a) `turns` 삽입/시간 (b) MCP 툴 p95 지연 (c) day_cycle 단계 소요 (d) **훅 오버헤드(호출당 ms)** (e) 재시작/롤백 횟수. 임계값 예: p95 지연 +30% 이상 10분 지속 → 경보 | 지표 5종이 1주간 수집, 경보 1건 정상 발동 |
| **Rollback Matrix** | `docs/ops/rollback-matrix.md` | Phase별 롤백 자산: 유닛·이미지 digest·볼륨·`opencode.json`·DB, **리허설 합격기준 = 롤백 후 health 10분 + 데이터 diff 0** | 각 Phase 전환 전 리허설 1회 통과 |
| **Provenance 정책** | `docs/specs/ingest-provenance.yaml` | 신규 유입 100% 기록, 기존 7152건 `legacy:pre-2026-09`, ADR-0005에서 legacy 마커는 **추출 no-op+통계만** | 신규 유입 100%, legacy 분리 쿼리 동작 |
| **D5 보안 lifecycle** | config + 스크립트 | bearer 저장(`~/.config/devforge/secrets.env`)·회전(월간/교체 스크립트)·폐기(무효화) | 저장·회전·폐기 3단계 스크립트 동작 |
| **수치·용어 정합** | 문서 일괄 수정 | 로드 **32** / 유효계약 **12** / 서버 노출 **25** / 목표 **≈15** / 0회 **9** / merge **−8** / day_cycle **448** / turns **7152** | final-plan·브리프·부록·ADR-0006 전 문서 일치 |

### 3.3 H0 수락기준 (M1 T3 해소)

- **H0(병행) 완료 =** ① stub 없는 **베이스 이미지**(의존성 pinning 포함) 빌드 성공 ② 로컬 마운트로 A~G 코드 테스트 통과 ③ **마운트↔이미지 동작 diff 도구**(Python path·의존성 버전·권한 체크)로 차이 0 확인.
- **H(후행) 전환 전 =** 단일 이미지 교체 **롤백 리허설 1회** 필수(§3.2 Rollback Matrix).

### 3.4 R7 비교 축 (M1 T8 해소)

Native Messaging vs 폴링 파서 확장, 다음 축으로 비교 후 결정: **보안(relay 노출면)** · **유지보수(브라우저 업데이트/설치)** · **롤백 용이성** · **로그인/2FA 커버리지** · **지연/리소스**. 웹수집 편입(D3 승인 시) 이전에 축별 점수표 1장.

### 3.5 P3 훅 오버헤드 측정 (M1 T7 해소)

- F9는 "운영 중"만 확인. **Phase 0에서 `auto_log.py` 호출당 오버헤드(ms)와 DB write 빈도를 측정**하고, 그 수치로 M2 수락기준(예: p95 < 50ms)을 설정. `/tmp/devforge-hook-errors.log`(현재 없음)를 오류 집계로 활성화.

---

## 4. M2 수치 제안 검증 (§2 T10~T12)

| 제안 | 판정 | 근거/검증 필요 |
|---|---|---|
| **RTO 4h / RPO 1d** | **후보 수용 — 소유자 확정 필요** | 백업은 OCI Object Storage(동일 리전 ap-tokyo) 일일 pg_dump + 30d/56d 보존 + restore-test 존재. RPO 1d는 일일 백업 기준 성립. RTO 4h는 VM 재생성(골든이미지)+복원으로 가능성 있으나 **미검증** |
| **추론 12Gi 하드리밋** | **보류 — 실측 후 결정** | 현재 추론 모델(Pod B, MemoryMax=23G 설정 이력, reranker RSS 4.6GB 등)과 상충 가능. 12Gi는 여러 모델 병렬 로드에 부족. **cgroups 한도는 실측 사용량 기반으로 설정** |
| **oom_score_adj 조정** | **수용** | 추론(cgroups) 외 서비스(Postgres·MCP·Caddy) 보호 원칙 타당. 수치는 실측 후 |
| **SBOM** | **수용(필수 승격)** | H(단일 이미지 전환) 시 이미지 SBOM + 의존성 pinning 필수 |

---

## 5. 최종 계획서(final-plan.md) 반영 사항 (diff)

1. **MCP 순서 확정**: contract-first (계약 동결 → M2 → M1 → M3 구현 → M0/M4). §4.4에 명시.
2. **Phase 0 신설**: §5 앞에 "Phase 0 — 게이트 산출물" 절. §3.2의 6개 산출물.
3. **용어·수치 정합**: 32 / 12(유효계약) / 25(노출) / ≈15 / 9 / −8 / 448 / 7152. 문서 전역.
4. **컷오버 이원화**: (a) 구현+shadow, (b) 전환. A는 구현/전환 분리 + 12툴 계약(§3.1) 열거.
5. **리스크 매트릭스**: ARM cgroups/nice/OOM, 비용 하드캡(OCI Budgets+OpenRouter hard limit), SPOF RTO/RPO 목표, SBOM, 훅 파손→폴링 폴백, pgvector 재색인.
6. **H 분리**: H0(병행 빌드 파이프라인)/H(후행 교체) + 수락기준(§3.3).

---

## 6. 최종 판정

**조건부 실행 승인 (Approved with Phase-0 gate).**

- v1(수용 20/부분 8/미수용 6) → v2(미수용 3, 부분 11, 조건부 1) → **v3(메타리뷰 수용 12/부분 1/보류 1, 12툴 열거, Phase 0 산출물 6종 정의)**.
- M1의 "미완성 체크리스트" 비판은 타당했으며, v3의 §3이 이를 **실행 산출물**로 전환.
- 남은 불확실성(RTO/RPO, 12Gi, 훅 오버헤드, escalation rate)은 **리뷰가 아니라 Phase 0의 실측 과제**.
- **다음 행동:** §3.2 산출물 6종 생성(시작: `specs/mcp-contract.json` + 12툴 목록 ADR-0006 반영) → 게이트 통과 후 Phase A/MCP 착수.

*End of v3. 이 문서는 설계 검토의 종착점이며, 이후는 실행(Phase 0) 단계다.*