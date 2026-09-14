# 외부 리뷰 3종 + 자체 분석 — 수용/미수용 트리아지 보고서

> Status: record · Date: 2026-09-14 · Owner: devforge
> 대상: `docs/plans/final-plan.md`, `docs/reports/review-brief-20260914.md`, 근거 부록(업계표준 + MCP 감사)
> 입력 리뷰: deepseek(1차), qwen(1차), qwen(2차 정밀) + 자체 문서 정합 분석
> 방법: 모든 주장을 **시스템 실측**(config/소스/DB/systemd)과 대조. 판정 = 수용 / 부분수용 / 미수용.

---

## 0. 검증에 사용한 시스템 스냅샷 (근거)

| 사실 | 실측값 | 근거 |
|---|---|---|
| opencode 로드 툴(allowlist) | devforge **12** + lsp **13** + yggdrasil **4** + opencode-db **3**(무필터) = **32** | `~/.config/opencode/opencode.json` `tools` |
| 서버 노출 툴 | devforge-mcp 25, lsp 65 | 문서/감사 |
| 리팩토링 MCP 툴 | **5** (`knowledge_search`, `pipeline_status`, `extract_turn`, `deepdive`, `store_observation`) | `src/devforge/adapters/driving/mcp/server.py` |
| domain/adapters stub | `domain/{watchdog,turn_collection,model_management,pipeline}`, `adapters/driven/{notification,research,proxy_utils}` = **0줄** | `wc -l` |
| day_cycle.sh | **448줄** (문서 표기 455) | `wc -l scripts/day_cycle.sh` |
| turns.source | **7152 / 7152 = `unknown`** (문서 표기 7,131) | `SELECT source,count(*) FROM turns` |
| `ingest` | 레거시 `scripts/mcp_server.py:486`에만 존재, 리팩토링 API/MCP에 **없음** | grep |
| shadow DB | `devforge_shadow` 존재, 컷오버용 **diff 도구 없음** | psql / grep |
| PostToolUse 훅 | `scripts/hooks/auto_log.py` **이미 운영 중** | hooks/settings |
| systemd user 생존 | `Linger=yes` | `loginctl show-user opc` |
| Observability | `netdata` **active**, watchdog 3종 운영 | systemctl |
| ADR-0006 | "12툴 계약" 언급하나 **목록 미열거** | `docs/adr/0006-mcp-tool-surface.md` |
| 감사표 | `deep_planning` **중복 기재**(devforge-mcp행 + yggdrasil행), 합계 587 vs 표기 475 | `docs/reports/mcp-tool-audit-20260914.md` §2 |

**실측 로드 툴 32** 기준 재계산: 0회 9개 제거 → 23, 근접중복 merge −8(deepdive 4→1, memory 4→2, search 2→1, schema 2→1, plan 2→1) → **목표 ≈ 15**. 문서의 `16`은 33 기준, `12`는 **ADR-0006 서버 계약 수와 혼동**.

---

## 1. 수용 (Accept) — 즉시 반영

| # | 주장(출처) | 시스템 근거 | 조치 |
|---|---|---|---|
| A1 | MCP 목표 수 `33→12`(계획·브리프) vs `33→16`(감사) 불일치 (자체, qwen2) | 계획 §4.2/4.3 vs 감사 §4 | 단일 숫자(≈15)로 확정. "서버 계약 12"와 "클라이언트 로드 목표" **분리 표기** |
| A2 | "0회 9개" 리스트 오류 — `detect_lsp_servers`(실측 2회) 포함, `suggest_fixes`·`rename_symbol`(0회) 누락 (자체, qwen2) | 감사 §2 | 0회 = proxy_artifact3+find/inspect/get_source3+suggest/rename2+list_plans1 = **9**, detect는 별도 |
| A3 | 로드 툴 **33이 아니라 32**; 감사표 `deep_planning` 중복, 합계 587 vs 475 (자체) | opencode.json, 감사표 | 표 중복행 제거, 32로 정정 |
| A4 | `Merge −6` 산술 오류(실제 −8) (자체) | merge 항목 합 | −8로 정정 |
| A5 | `turns.source` 전부 unknown, 백필 단계 부재 (deepseek A/I, qwen2) | DB 7152/7152 unknown | **Phase 0(또는 I)에 provenance 백필** 신설 |
| A6 | Phase A 선행조건 불충족: 리팩토링 MCP 5툴 → 12툴 계약 재현은 대규모 신규 구현 (deepseek, qwen2) | server.py 5툴, §0 "구현/수락 전 전환 금지" | A를 "구현"과 "전환" 2단계로 분리, 12툴 목록 명시 |
| A7 | Phase B에 웹수집 포함 ↔ D3 미결정 = 순환 (deepseek, qwen1, qwen2) | D3 status=대기 | B1(로컬 provenance) / B2(웹, D3 후) 분리 |
| A8 | D2: "12툴 계약" 목록 미열거 (deepseek, qwen1) | ADR-0006 미열거 | 12툴 명시(실제: deepdive_* 5 + obs_* 2 + search_* 2 + mem_* 2 + get_conversation) |
| A9 | D3 프레이밍 "재가동" 오류 → 실은 신규 파이프라인 도입, 비용 추정 없음 (deepseek, qwen1) | chrome-web-llm 아카이브·미연결 | "도입/은퇴"로 재프레이밍 + 공수 추정 |
| A10 | D5 bearer 저장·회전·폐기 절차 없음, loopback만으로 충분할 수 있음 (deepseek) | — | bearer 필요성 재판정, 채택 시 lifecycle 명시 |
| A11 | D6 모호(흡수·축소·병렬) — 기준·worker 격리 트레이드오프 없음 (deepseek, qwen2) | — | 판단 기준 + 리소스 트레이드오프 추가 |
| A12 | D7 B안 완화가 "훅 비활성"뿐 → 수동 obs_write 폴백 필요 (deepseek) | — | 폴백 절차 추가 |
| A13 | D8 선택지가 §6.4와 불일치(`start_lsp` 등), obs 성능 정량화 없음 (qwen1) | — | 선택지 정합 + 성능 영향 |
| A14 | D9 schema merge 호출자 파괴 위험, 30일 호출패턴 검증 필요 (deepseek, qwen1) | opencode-db 10/3회 | merge 보류 또는 alias 검증 후 결정 |
| A15 | 롤백이 실패모드별이 아님, 서버·클라이언트(`opencode.json`) 동기 롤백 부재 (deepseek) | — | Phase별 롤백 매트릭스 + config 상태 포함 |
| A16 | 클라이언트 훅 API 변경 시 캡처 중단 → 폴링 폴백 필요 (qwen1) | — | 폴백(3s 폴링 재가동) 명시 |
| A17 | ARM 리소스 기아 대비(cgroups/nice/OOM) 부재 (deepseek, qwen1, qwen2) | 4코어/22Gi, 리스크표 없음 | 리스크표에 리소스 격리 추가 |
| A18 | 툴 포이즈닝 대비(allowlist + description hash pinning) M0~M4 미반영 (qwen1) | 감사 §5 권고 | M4에 포함 |
| A19 | D1 클라우드 escalation 비용·거버넌스·`review_facts` 의미 변화 미분석 (deepseek, qwen2) | 클라우드 경로 존재 | 비용/거버넌스 분석 추가 |
| A20 | 추출은 critical path에서 deterministic/cache만, LLM 추출은 비동기 한정 (qwen2) | — | 수락기준에 명시 |

---

## 2. 부분수용 (Partial) — 조건·범위 한정

| # | 주장(출처) | 유효 부분 | 기각/한정 부분 |
|---|---|---|---|
| P1 | M0 "2주 재측정" 무의미 (deepseek) | 확정 0회 툴은 즉시 제거 타당 | 재측정은 M4(후보군)로 이동, M0에서 삭제 |
| P2 | M4 "잔여 0회 final 컷"이 M0와 중복 (deepseek) | 중복 제거 | M4는 `find_symbol`류 **후보군** 대상으로 한정 |
| P3 | PostToolUse 훅 성능 오버헤드 (deepseek) | 모든 툴 호출당 DB write 리스크 유효 | **이미 `auto_log.py`로 운영 중** — greenfield 아님. 문서에 기존 훅·오버헤드 실측 인용 |
| P4 | shadow `diff=0` 달성 불가 (qwen2) | timestamp/sequence/trigger 노이즈 유효 | "불가"는 과장 → **정규화 + 허용오차 + diff 도구 정의**로 해결 |
| P5 | Phase C는 이관이 아니라 재설계, "2주" 불가 (qwen2) | ADR-0005 라우팅 재설계 유효, 448줄(≠455) | "2주 불가"는 미검증 → 일정은 **행위명세 후 추정** |
| P6 | Phase H 롤백이 digest 복원뿐 (deepseek) | 볼륨/네트워크/포트/Quadlet 유닛 복원 필요 유효 | H를 A~G 앞으로 옮기자는 주장은 **미수용(§3 R3)** |
| P7 | Observability 문제 (qwen2) | 컷오버용 지표·diff·롤백 트리거 미정의는 사실 | "완전 부재"는 **오류** — netdata active, watchdog 3종 |
| P8 | pgvector/FTS 재색인 지연 (qwen2) | 대량 추출 시 검색 지연 가능 | 발생 조건·임계 미검증 → 리스크에 조건부 등재 |

---

## 3. 미수용 (Reject) — 사실 오류이거나 부적절

| # | 주장(출처) | 기각 사유(시스템 근거) |
|---|---|---|
| R1 | `systemd --user`는 세션 종료 시 서비스 사망 (qwen2) | `loginctl show-user opc` → **`Linger=yes`**. 세션/SSH 종료와 무관하게 생존 |
| R2 | Observability "완전 부재" (qwen2) | `netdata` **active**, watchdog 3종(`devforge-watchdog*`) 운영 중 |
| R3 | Phase H(단일 이미지)를 A~G보다 **선행**해야 함 (qwen2) | 미구현 도메인을 이미지에 굽게 됨. 비파괴 컷오버 원칙과 충돌. H는 A~G 후가 맞음(롤백 상세 보강은 수용) |
| R4 | M3(merge)를 M1/M2(훅)보다 **먼저** (qwen1) | 최고가치(수집 커버리지)를 지연시키고 표면을 두 번 churn. 단 "merge가 이름/인자를 바꾼다"는 경고는 유효 → M1/M2 착수 시 merge 목표 네이밍을 **선결** |
| R5 | Phase A는 "7개 툴 신규 구현" (qwen2) | 리팩토링 5툴(knowledge_search 등)은 목표 12툴과 **1:1 대응 아님** → 정확히 +7이 아님. 방향(대규모 구현)은 수용(A6) |
| R6 | MCP 서버 4→1~2 통합 (qwen2) | opencode는 allowlist로 이미 프루닝(32). 통합은 대규모·고위험, 현재 편익 낮음 → **장기 검토**로만 |
| R7 | Native Messaging은 부적합, 폴링 파서 확장이 정답 (deepseek) | 폴링 확장은 **유력 대안(검토 수용)**이나, Native Messaging의 로그인/2FA 우위를 배제할 근거 없음 → **양안 비교 후 결정** |
| R8 | `day_cycle 455줄` 그대로 인용 (qwen2) | 실측 **448줄**. 수치 정정 |

---

## 4. 리뷰별 정확도 평가

| 리뷰 | 강점 | 약점/오류 |
|---|---|---|
| **deepseek** | 실패모드·롤백 갭·D-프레이밍 비판이 날카로움. A/I 데이터 정합 지적 정확 | "PostToolUse 미대응"은 이미 운영 중(`auto_log.py`)을 간과 |
| **qwen 1차** | 보안(pinning)·B 범위·D3/D6/D8 프레이밍 지적 유효 | M3→M1/M2 순서 제안은 **미수용(R4)** |
| **qwen 2차** | 수치 모순(12 vs 16)·0회 리스트·의존성 DAG 지적 정확 | **R1·R2·R3·R5·R8** 사실/판단 오류. "완전 부재"·"불가" 등 과장 |
| **자체 분석** | 32(≠33)·중복행·합계 587·0회 리스트·12↔16 혼동 규명 | 일부 항목은 리뷰와 중복 |

**공통 합의(3+1)**: Phase A 선행 미충족, B↔D3 순환, I의 provenance 누락, 롤백 불충분, D3/D6/D9 프레이밍 약함.

---

## 5. 계획 반영 권고 체크리스트 (우선순위)

1. **Phase 0 신설**: `ingest`(HTTP+MCP) 복원 + `turns.source`/`agent` provenance 백필(7152건) + 수신 보안(D5).
2. **컷오버 이원화**: (a) 도메인 구현 + shadow 검증, (b) 프로덕션 전환. A는 구현/전환 분리, 12툴 계약 명시.
3. **수치 정합 일괄 수정**: 32툴, 0회 9개 정정, 목표 ≈15, merge −8, day_cycle 448, turns 7152, 12↔16 분리.
4. **MCP 순서**: M2(obs)→M1(deepdive)→M3(merge, 네이밍 선결)→M0/M4(allowlist·annotation·pinning).
5. **롤백 매트릭스**: Phase별 실패모드·롤백 자산(유닛/볼륨/네트/config) + **롤백 리허설** 단계.
6. **리스크 보강**: ARM cgroups/nice/OOM, 훅 API 파손→폴링 폴백, pgvector 재색인, Caddy↔rootless 포트.
7. **D1~D9 근거 보강**: D1 비용/거버넌스, D3 재프레이밍+공수, D6 기준, D9 보류/검증.

---

## 6. 결론

- 리뷰 3종의 **구조적 비판(의존성·롤백·프레이밍)은 대체로 타당** → 수용 20건, 부분 8건.
- 반면 **사실 오류 6건**(R1·R2·R3·R5·R8 등)은 시스템 실측으로 기각. 특히 qwen2의 systemd/observability 지적은 무효.
- 자체 분석이 규명한 **정량 불일치(32 vs 33, 12 vs 16, 합계 587 vs 475)** 가 계획 신뢰도의 최대 리스크 → 배포 전 수치 정합이 최우선.
- **최종 판정: 조건부 승인.** §5의 1~3(provenance/컷오버 이원화/수치 정합) 선행 시 실행 가능.
