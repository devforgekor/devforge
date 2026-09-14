# DevForge 계획 리뷰 트리아지 — 외부 에이전트 검증 패키지

> **Status:** record · **Date:** 2026-09-14 · **Owner:** devforge
> **목적:** DevForge 최종 계획서에 대한 외부 AI 리뷰 3종(deepseek·qwen1·qwen2)과 자체 문서 분석을 **시스템 실측과 대조**하여 수용/미수용을 판정한 결과를, **이 서버를 전혀 모르는 다른 에이전트가 독립적으로 검증**할 수 있도록 자가완결로 정리.
> **이 문서를 받은 에이전트에게:** §8의 검증 질문에 답하라. §4(실측)와 §5(판정)가 검증 대상이다.

---

## 0. 읽는 순서 / 이 패키지의 구성

1. **§1 시스템 배경** — DevForge가 무엇인지(자가완결).
2. **§2 검토 대상 문서 3종** — 리뷰어들이 본 원문 요약 + 원문 URL.
3. **§3 외부 리뷰 3종 주장** — deepseek / qwen1 / qwen2의 주장을 구조화.
4. **§4 시스템 실측 근거** — 판정의 유일한 근거(설정·소스·DB·systemd).
5. **§5 트리아지 판정** — 수용 20 / 부분 8 / 미수용 6 (근거 포함).
6. **§6 리뷰별 정확도 평가.**
7. **§7 계획 수정 권고(우선순위).**
8. **§8 검증 질문** — 다른 에이전트가 답할 것.

---

## 1. 시스템 배경 (자가완결)

**DevForge** = Oracle Cloud(OCI, 도쿄, ARM Ampere A1 **4코어/22Gi, GPU 없음**) **단일 서버**에서 돌아가는 LLM 추론 + 데이터 파이프라인 + 웹앱 + 파일교환 통합 시스템. AI 에이전트(Claude Code / opencode 등)의 대화·관찰·지식을 수집·가공해 MCP로 다시 노출하는 "개인 AI 개발 워크스테이션".

- **OS/런타임:** Oracle Linux 9.7 (aarch64), Podman rootless(user `opc`), Caddy(rootful, host net).
- **DB:** PostgreSQL 16(`devforge_app`) + pgvector + pg_trgm, 앱 소유 16테이블(`turns`, `review_facts`, `observations`, `embeddings` …).
- **컨테이너(svc pod):** `postgres`, `devforge-mcp`(FastMCP :8000), `devforge-worker`, `devforge-fastapi`(:8002 hub), `flaresolverr`; 추론은 동적 `devforge-inference`(llama.cpp :8080~8084).
- **systemd user 서비스:** 대표 22개 이상(watchdog 3, turn-watcher, day-cycle, system-sync, backup, dev-poll, activity-summarizer, weekly-enrich-rebuild, proxies 5종, golden-image 2 등). `Linger=yes`.
- **데이터 흐름:** `turn_watcher`(3초 폴링, 로컬 에이전트 로그 5종 파싱) → `turns`(raw) → `raw_consumer`(worker) → `pending` → `day_cycle.sh`(clean→scan→extract→verify→enrich→embed) → MCP 노출.
- **코드 이중 구조:**
  - **레거시(라이브):** `scripts/` 평면 배치(진입점 50+, `scripts/lib/` 28 서브모듈). systemd/Quadlet가 실행.
  - **신규(리팩토링):** `src/devforge/` — src-layout + DDD + Ports & Adapters. 단일 CLI `devforge`.
- **핵심 전제:** 라이브는 아직 레거시 `scripts/*`가 담당하고, 신규 패키지는 **골격 + 추출 파이프라인까지만 구현**됨. 따라서 "컷오버(scripts→devforge)"가 최대 주제다.

---

## 2. 검토 대상 문서 3종 (리뷰어가 본 원문)

| 문서 | 내용 | 원문 URL |
|---|---|---|
| **리뷰 브리프** | 외부 검토용 자가완결 컨텍스트 + 리뷰 질문 5개 + 붙여넣기 프롬프트 | https://objectstorage.ap-tokyo-1.oraclecloud.com/p/xm2eHQZt92ZL1kZU0XiDEIkkSfzLyfIE6M_p9Tjgiaz_jRMWdFcMkZxQEMK55TMn/n/nrhe1zafhd0v/b/devforge-standard/o/uploads/2026-09-14/085145_review-brief.html |
| **최종 계획서** | 컷오버 A~I + MCP 최적화 M0~M4 + 통합 의사결정 D1~D9 | https://objectstorage.ap-tokyo-1.oraclecloud.com/p/ucl6MsIZNOQLHJLFI3Wbggvas4cibuuIzsuUuOuZTdnkHAnvkYDhJHZpXy74iy9H/n/nrhe1zafhd0v/b/devforge-standard/o/uploads/2026-09-14/085147_final-plan.html |
| **근거 부록** | 업계 표준 5계층 대조 + MCP 툴 30일 실사용 감사 | https://objectstorage.ap-tokyo-1.oraclecloud.com/p/_TxxAwZ5xMZrE1mUUdhrFKzS9_25V02HA-uuqjlHeqNrpJ5DO6XwpK0a6Y65CjvF/n/nrhe1zafhd0v/b/devforge-standard/o/uploads/2026-09-14/085149_evidence-appendix.html |

### 2.1 최종 계획서 핵심 (요약)
- **컷오버 A~I:** A MCP 컷오버(12툴 계약+ingest) → B turn-watcher+웹수집 → C day-cycle+worker(orchestrator, ADR-0005) → D watchdog 3종 → E inference → F notification/research/proxy → G backup/restore → H 단일 `devforge:latest` 이미지 → I scripts 이관·문서.
- **MCP 최적화 M0~M4:** M0 0회 툴 allowlist off → M1 deepdive lifecycle 훅 → M2 obs 자동캡처(PostToolUse) → M3 Merge 파사드(memory/search/schema/plan) → M4 annotation+재측정.
- **의사결정 D1~D9:** D1 추출 아키텍처(ADR-0005) / D2 MCP 전략(ADR-0006) / D3 웹수집 / D4 첫 착수 / D5 ingest 보안 / D6 범위·일정 / D7 MCP 옵션 / D8 훅 범위 / D9 Merge 범위. **상태: 전부 "대기"**.

### 2.2 근거 부록 핵심 (요약)
- 5계층(Capture/Ingestion/Storage·Retrieval/Extraction/Serving) 대조 → 이탈 3점: (a) 웹 수집 미연결, (b) 리팩토링 MCP 계약 불일치(5 vs 허용 12), (c) 로컬 8B 배치 추출.
- MCP 30일 감사: 로드 33툴, 0회 9개, 총 호출 475. 목표 `33→약 16`.
- 권고: `ingest` 복원 + 12툴 계약 보존 → 웹 수집 편입·provenance → 추출 라우팅 재설계.

---

## 3. 외부 리뷰 3종 — 구조화된 주장

### 3.1 deepseek (1차)
- **판정:** 조건부 승인. 방향(비파괴/표준/저위험)은 타당.
- **컷오버:** A~I가 "개발·검증·배포·문서"를 한 이름에 뒤섞음 → (a)도메인 구현 (b)shadow 검증 (c)프로덕션 전환 (d)정리 4트랙 분리 필요. **A 선행조건 불충족**(리팩토링 MCP 5툴 → 12툴 계약은 미구현 도메인 필요). **I에 데이터 마이그레이션 검증 누락**(turns.source가 라이브에 없던 컬럼). **H**: digest 복원은 이미지·유닛·볼륨·포트를 복원 못 함.
- **MCP 순서:** M2(obs)가 M1(deepdive)보다 사용량 많고 수집 기여 직접적 → **M2→M1**. M0의 "2주 재측정"은 무의미. M4 final 컷은 M0와 중복.
- **우선순위:** D4가 M0 착수를 권장하나 M0는 효과 제한적(opencode는 이미 프루닝). **실제 병목은 ingest 복원** → `ingest+provenance` 먼저.
- **롤백 갭:** 서버·클라이언트(`opencode.json`) 동기 롤백 부재, 데이터 마이그레이션은 git revert 불가.
- **놓친 실패모드:** PostToolUse 훅 성능, systemd 의존성 순환, pgvector 재색인 지연, Caddy↔rootless 포트, turns.source 중복/누락, 무필터 클라이언트.
- **D-프레이밍:** D2 12툴 미열거, D3 "재가동"은 비용 미추정 낙관, D4 M0 권장 모순, D5 bearer 절차 없음, D6 모호, D7 폴백 없음, D8 성능 정량화 없음, D9 schema merge 호출자 파괴 위험.
- **반론:** (1) 툴 스프롤은 이미 해결, 진짜 문제는 무필터 클라이언트. (2) Native Messaging보다 **기존 폴링 파서 확장**이 저비용. (3) D1 "서버 역할 재배치"는 비용/지연/프라이버시 트레이드오프 무시.

### 3.2 qwen 1차
- **판정:** 조건부 승인. D3 프레이밍 수정, M1/M3 순서 조정, 리소스·훅 롤백 보완 필요.
- **순서:** **M3(merge)를 M1/M2(훅)보다 먼저**(인터페이스 안정 후 훅 연결). 보안(description hash pinning)이 M0~M4 어디에도 없음. **B단계에서 웹수집을 분리**(D3 결정 후 별도).
- **우선순위:** `M0 → A(ingest+로컬 provenance 한정) → D3 재평가 → B(웹수집)`.
- **롤백/실패모드:** 클라이언트 훅 API 변경 파손→폴링 폴백, ARM 리소스 기아(cgroups/nice), merge 하위호환 실패(에이전트 컨텍스트 동기화).
- **D-프레이밍:** D3 "재가동"→"신규 파이프라인 도입"으로 재프레이밍, D6 worker 흡수/별도 트레이드오프 누락, D8이 §6.4의 `start_lsp` 자동/제거와 불일치.
- **반론:** 감사 결론 "스프롤은 문제 아님"은 낙관 편향 → "특정 클라이언트에서만 우회". 대안: 동적 툴 로딩/툴 그룹화, LLM 추출은 비동기 잡으로만.

### 3.3 qwen 2차 (정밀)
- **판정:** **보류 및 재설계 요구.** 1차는 구조, 2차는 내부 일관성·숨은 의존성·리소스 실현성·근거 강도.
- **내부 일관성:** "0회 9개" 리스트가 실제 8개(후보 포함해야 9) / `start_lsp` 13회를 0회 프레이밍과 혼동 / ADR-0006(12) vs 감사(16) 모순.
- **숨은 의존성:** Phase A = 5툴 → 12툴 **7개 신규 구현**. B↔D3 순환. C는 455줄 재설계(2주 불가). **H가 A~G보다 먼저**여야 중복 방지.
- **실현성:** 4코어 ARM 리소스 경합(cgroups/nice/OOM), Postgres+pgvector+FTS5 동시 부하, **shadow diff=0 불가**.
- **근거 강도:** "12툴 계약"의 지위 불명(설정? 표준?), "서버당 5~15" 출처가 클라이언트 컨텍스트 기준인지 불명, D1 비용 절감 근거가 ARM 특수성 미반영.
- **대안:** MCP 서버 4→1~2 통합, observability 부재, systemd user 스케일 한계(세션 타임아웃).
- **재설계 요구:** 의존성 DAG 명시, Phase A 구현 범위 확정, H 우선순위 재조정, observability 선행, ARM LLM 사용 정책.

---

## 4. 시스템 실측 근거 (판정의 근거 — 전부 재현 가능)

| # | 사실 | 실측값 | 재현 방법 |
|---|---|---|---|
| F1 | opencode 로드 툴 | devforge **12** + lsp **13** + yggdrasil **4** + opencode-db **3**(무필터) = **32** | `cat ~/.config/opencode/opencode.json` → `tools` |
| F2 | 서버 노출 툴 | devforge-mcp 25, lsp 65 | MCP `tools/list` / 문서 |
| F3 | 리팩토링 MCP 툴 | **5** (`knowledge_search`, `pipeline_status`, `extract_turn`, `deepdive`, `store_observation`) | `src/devforge/adapters/driving/mcp/server.py` |
| F4 | 도메인/어댑터 stub | `domain/{watchdog,turn_collection,model_management,pipeline}`·`adapters/driven/{notification,research,proxy_utils}` = **0줄** | `wc -l` |
| F5 | day_cycle.sh | **448줄** (문서 표기 455) | `wc -l scripts/day_cycle.sh` |
| F6 | turns.source | **7152/7152 = `unknown`** (문서 표기 7,131) | `SELECT source,count(*) FROM turns GROUP BY source` |
| F7 | `ingest` | 레거시 `scripts/mcp_server.py:486`만, 리팩토링 API/MCP **없음** | grep |
| F8 | shadow DB | `devforge_shadow` 존재, 컷오버 diff 도구 **없음** | psql / grep |
| F9 | PostToolUse 훅 | `scripts/hooks/auto_log.py` **이미 운영 중** | hooks/settings.json |
| F10 | systemd user 생존 | `Linger=yes` | `loginctl show-user opc` |
| F11 | Observability | `netdata` **active**, watchdog 3종 | `systemctl is-active netdata` |
| F12 | ADR-0006 | "12툴 계약" 언급, **목록 미열거** | `docs/adr/0006-mcp-tool-surface.md` |
| F13 | 감사표 결함 | `deep_planning` **중복 기재**(devforge-mcp행+yggdrasil행), 표 합계 **587** vs 표기 **475** | `docs/reports/mcp-tool-audit-20260914.md` §2 |

**F1에서 도출한 정정치:**
- 로드 툴 = **32**(문서 33은 F13 중복 기재 탓).
- 0회 = proxy_artifact_{get,info,list}(3) + find_symbol·inspect_symbol·get_symbol_source(3) + suggest_fixes·rename_symbol(2) + list_plans(1) = **9** (`detect_lsp_servers`는 실측 2회 → 0회 아님).
- 목표 = 32 − 9(remove) − 8(merge: deepdive 4→1, memory 4→2, search 2→1, schema 2→1, plan 2→1) = **≈15**.
- 문서 `16`(감사, 33 기준)과 `12`(계획, ADR-0006 서버 계약 수와 혼동)가 어긋남.

**리팩토링 MCP 계약 대조:** opencode가 쓰는 devforge 12툴 = `deepdive_step_enter/exit/session_heartbeat/session_status/verify_sandbox`(5) + `obs_write/obs_search`(2) + `search_turns/search_similarity`(2) + `mem_save/mem_search`(2) + `get_conversation`(1). 리팩토링의 5툴과 **1:1 대응 아님**(이름·의미 상이).

---

## 5. 트리아지 판정

### 5.1 수용 (Accept) — 20건

| # | 주장(출처) | 근거 | 조치 |
|---|---|---|---|
| A1 | MCP 목표 `12`↔`16` 불일치 (자체·qwen2) | 계획 §4.2/4.3 vs 감사 §4 | ≈15로 확정, "서버 계약 12"와 분리 표기 |
| A2 | "0회 9개" 리스트 오류(detect 포함, suggest/rename 누락) (자체·qwen2) | F1·F13 | 리스트 정정 |
| A3 | 로드 툴 32(≠33), 감사표 중복·합계 587 (자체) | F1·F13 | 표 정정 |
| A4 | `Merge −6` 산술 오류(실제 −8) (자체) | merge 항목 | −8 정정 |
| A5 | turns.source 전부 unknown, 백필 단계 부재 (deepseek·qwen2) | F6 | **Phase 0에 provenance 백필** |
| A6 | Phase A 선행조건 불충족(5툴→12툴 대규모 구현) (deepseek·qwen2) | F3 | A를 구현/전환 2단계로 분리 |
| A7 | Phase B의 웹수집 ↔ D3 미결정 순환 (deepseek·qwen1·qwen2) | D3 대기 | B1/B2 분리 |
| A8 | D2 "12툴 계약" 목록 미열거 (deepseek·qwen1) | F12 | 12툴 명시 |
| A9 | D3 "재가동" 프레이밍 오류·비용 미추정 (deepseek·qwen1) | chrome-web 미연결 | 도입/은퇴 재프레이밍+공수 |
| A10 | D5 bearer 저장·회전·폐기 절차 없음 (deepseek) | — | 필요성 재판정+lifecycle |
| A11 | D6 모호·worker 격리 트레이드오프 없음 (deepseek·qwen2) | — | 기준+트레이드오프 |
| A12 | D7 B안 완화 부족→수동 폴백 필요 (deepseek) | — | 폴백 절차 |
| A13 | D8 선택지 §6.4 불일치·obs 성능 미정량 (qwen1) | — | 정합+정량 |
| A14 | D9 schema merge 호출자 파괴 위험 (deepseek·qwen1) | opencode-db 10/3회 | 보류 또는 alias 검증 |
| A15 | 롤백 실패모드별 아님·서버↔클라이언트 동기 부재 (deepseek) | — | 롤백 매트릭스 |
| A16 | 클라이언트 훅 API 변경→폴링 폴백 (qwen1) | — | 폴백 명시 |
| A17 | ARM 리소스 기아 대비 부재 (3인) | 리스크표 없음 | cgroups/nice/OOM |
| A18 | 툴 포이즈닝(pinning) 미반영 (qwen1) | 감사 §5 | M4 포함 |
| A19 | D1 비용·거버넌스·review_facts 의미 변화 미분석 (deepseek·qwen2) | 클라우드 경로 존재 | 분석 추가 |
| A20 | LLM 추출은 비동기 한정, critical path는 deterministic/cache (qwen2) | — | 수락기준 명시 |

### 5.2 부분수용 (Partial) — 8건

| # | 주장(출처) | 유효 부분 | 한정/기각 |
|---|---|---|---|
| P1 | M0 "2주 재측정" 무의미 (deepseek) | 확정 0회는 즉시 제거 | 재측정은 M4로 이동 |
| P2 | M4 final 컷 M0 중복 (deepseek) | 중복 제거 | M4는 후보군 한정 |
| P3 | PostToolUse 훅 성능 (deepseek) | 호출당 DB write 리스크 유효 | **F9: 이미 운영 중** → greenfield 아님, 문서에 실측 인용 |
| P4 | shadow diff=0 불가 (qwen2) | 노이즈 유효 | "불가" 과장 → 정규화+허용오차+diff 도구 |
| P5 | Phase C는 재설계, "2주" 불가 (qwen2) | ADR-0005 재설계 유효 | "2주 불가" 미검증 → 행위명세 후 추정 |
| P6 | Phase H 롤백=digest뿐 (deepseek) | 볼륨/네트/포트/유닛 복원 필요 | H 선행 주장은 기각(§5.3 R3) |
| P7 | Observability 문제 (qwen2) | 컷오버 지표·diff·롤백 트리거 미정의 | "완전 부재"는 오류(F11) |
| P8 | pgvector/FTS 재색인 지연 (qwen2) | 조건부 가능 | 임계 미검증 |

### 5.3 미수용 (Reject) — 6건 (사실/판단 오류)

| # | 주장(출처) | 기각 사유 |
|---|---|---|
| R1 | systemd --user는 세션 종료 시 사망 (qwen2) | **F10: `Linger=yes`** → 세션/SSH 종료와 무관 생존 |
| R2 | Observability "완전 부재" (qwen2) | **F11: netdata active + watchdog 3종** |
| R3 | Phase H를 A~G보다 선행 (qwen2) | 미구현 도메인을 이미지에 굽게 됨 → 비파괴 원칙 위배. H는 A~G 후 |
| R4 | M3를 M1/M2보다 먼저 (qwen1) | 최고가치(수집 커버리지) 지연 + 표면 2회 churn. "merge가 이름/인자를 바꾼다"는 경고만 수용(네이밍 선결) |
| R5 | Phase A = "7개 툴 신규 구현" (qwen2) | F3: 리팩토링 5툴은 12툴과 1:1 대응 아님 → 정확히 +7 아님. 방향(A6)만 수용 |
| R6 | MCP 서버 4→1~2 통합 (qwen2) | opencode는 이미 프루닝(32). 대규모·고위험, 편익 낮음 → 장기 검토 |
| R7 | Native Messaging 부적합, 폴링 확장이 정답 (deepseek) | 폴링 확장은 유력 대안이나 Native의 로그인/2FA 우위 배제 근거 없음 → 양안 비교 |
| R8 | day_cycle 455줄 (qwen2) | F5: 실측 448줄 |

> R7·R8은 "미수용"이라기보다 각각 "양안 비교 필요"·"수치 정정"에 가깝다.

---

## 6. 리뷰별 정확도 평가

| 리뷰 | 강점 | 약점/오류 |
|---|---|---|
| **deepseek** | 실패모드·롤백·D-프레이밍 비판 예리. A/I 데이터 정합 정확 | "PostToolUse 미대응"은 이미 운영(F9) 간과 |
| **qwen 1차** | 보안(pinning)·B 범위·D3/D6/D8 지적 유효 | M3→M1/M2 순서 기각(R4) |
| **qwen 2차** | 수치 모순·0회 리스트·의존성 지적 정확 | **R1·R2·R3·R5·R8** 사실/판단 오류, "완전/불가" 과장 |
| **자체** | 32≠33·중복행·587·0회 리스트·12↔16 혼동 규명 | 일부 리뷰와 중복 |

**3+1 공통 합의:** Phase A 선행 미충족, B↔D3 순환, I의 provenance 누락, 롤백 불충분, D3/D6/D9 프레이밍 약함.

---

## 7. 계획 수정 권고 (우선순위)

1. **Phase 0 신설:** `ingest`(HTTP `/api/v1/ingest` + MCP `ingest`) 복원 + `turns.source`/`agent` provenance 백필(7152건) + D5 수신 보안.
2. **컷오버 이원화:** (a) 도메인 구현 + shadow 검증, (b) 프로덕션 전환. A는 구현/전환 분리 + 12툴 계약 명시.
3. **수치 정합 일괄:** 32툴 / 0회 9개 정정 / 목표 ≈15 / merge −8 / day_cycle 448 / turns 7152 / 12↔16 분리.
4. **MCP 순서:** M2(obs) → M1(deepdive) → M3(merge, 네이밍 선결) → M0·M4(allowlist·annotation·pinning).
5. **롤백 매트릭스 + 리허설 단계.**
6. **리스크 보강:** ARM cgroups/nice/OOM, 훅 API 파손→폴링 폴백, pgvector 재색인, Caddy↔rootless 포트.
7. **D1~D9 근거 보강:** D1 비용/거버넌스, D3 재프레이밍+공수, D6 기준, D9 보류/검증.

---

## 8. 검증 질문 (다른 에이전트가 답할 것)

> 근거는 **§4 실측 + 원문 3종(§2 URL)** 만 사용하고, 추측·사전지식은 배제하라.

1. §5.3의 **미수용 6건** 중 잘못 기각한 항목이 있는가? 특히 R3(H 선행)·R4(M3 선행)·R7(Native Messaging)에 반론이 있는가?
2. §5.1의 **A6(Phase A 구현/전환 분리)** 이 과잉인가, 불가피인가? 대안(계약을 줄여 A를 먼저 전환)은 성립하는가?
3. §4의 **정정치(32툴, 목표 ≈15)** 가 문서의 `33→12/16`보다 옳은가? 다른 해석 여지(예: 계약 12를 목표로 삼는 정당성)는?
4. **A5(provenance 백필)** 은 기술적으로 가능한가? 원본 로그가 남아 있지 않다면 `unknown`을 어떻게 처리해야 하는가?
5. §5.2에서 "부분수용"으로 내린 **P3(PostToolUse)·P4(shadow diff)** 판정이 타당한가, 아니면 전면 수용/기각해야 하는가?
6. §7 우선순위(Phase 0 → 컷오버 이원화 → 수치 정합)에 **누락되거나 잘못된 순서**가 있는가?
7. 리뷰 3종이 **공통으로 놓친** 리스크는 무엇인가? (예: 비용, 규정, 단일 서버 SPOF)

**출력 형식:** (1) 종합 판정 (2) 질문별 답 + 근거 (3) §5 판정 중 뒤집을 항목 (4) 추가로 필요한 정보.

---

## 부록 A. 실측 명령 (재현용)

```bash
# F1 로드 툴
python3 -c "import json;c=json.load(open('$HOME/.config/opencode/opencode.json'));print([k for k,v in c['tools'].items() if v is True])"
# F3 리팩토링 MCP 툴
grep -nE "^async def |register_tool" /opt/projects/server/src/devforge/adapters/driving/mcp/server.py
# F4 stub
find /opt/projects/server/src/devforge/domain /opt/projects/server/src/devforge/adapters/driven -name "*.py" -exec wc -l {} +
# F5 day_cycle
wc -l /opt/projects/server/scripts/day_cycle.sh
# F6 provenance
podman exec -i postgres psql -U postgres -d devforge_app -c "SELECT source,count(*) FROM turns GROUP BY source"
# F7 ingest
grep -rn "ingest" /opt/projects/server/scripts/mcp_server.py /opt/projects/server/src/devforge
# F8 shadow
podman exec -i postgres psql -U postgres -d devforge_app -c "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE '%shadow%'"
# F10 linger
loginctl show-user opc | grep -i linger
# F11 observability
systemctl is-active netdata; systemctl --user list-units 'devforge-watchdog*'
# F13 감사표
sed -n '/## 2. 결과/,/## 3/p' /opt/projects/server/docs/reports/mcp-tool-audit-20260914.md
```

## 부록 B. 로컬 원문 경로

- `docs/plans/final-plan.md`
- `docs/reports/review-brief-20260914.md`
- `docs/reports/industry-standard-comparison-20260914.md`
- `docs/reports/mcp-tool-audit-20260914.md`
- `docs/adr/0005-extraction-routing.md`, `docs/adr/0006-mcp-tool-surface.md`
- `docs/reports/review-triage-20260914.md` (본 트리아지의 축약본)

*End of review package. 이 문서 전체를 임의의 AI에 제출하여 §8을 검증받을 수 있다.*
