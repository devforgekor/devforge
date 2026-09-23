# 2026 표준 대비 서버 로직 개선 계획

> Status: proposed · Date: 2026-09-23 · Owner: devforge
> Related: `REFACTORING_PLAN.md`, `plans/final-plan.md`, `plans/watchdog-standard-compliance.md`, `plans/error-record-analysis-design.md`, `plans/dataimpulse-watchdog-delegation.md`, `reports/industry-standard-comparison-20260914.md`
> Deep Dive: `dp-20260923-2026-standard-gap-server-logic` (Yggdrasil)
> 방법: 서버 실측(코드/CI/설정/DB) → 2026 표준·동향(web) → **context7 검증** → 항목별 갭·조치. 기존 계획에 **항목 추가** 방식(중복 최소화).

---

## 0. 요약 (12항목)

| # | 항목 | 서버 현황(As-Is) | 갭 | 우선 |
|---|------|------------------|----|------|
| 1 | SBOM/서명/SLSA | CI build→GHCR **무서명**, SBOM 0 | SBOM·서명·provenance | **P0** |
| 2 | 이미지/의존성 스캔 | trivy/pip-audit 0 | 취약점·의존성 스캔 | **P0** |
| 3 | secretless identity | KV **SP client_secret**(장수명) | managed identity/federation | **P0** |
| 4 | secret rotation | rotation 타이머 0(주간 kv-backup만) | 자동 rotation | **P0** |
| 5 | MCP audit/telemetry | MCP 툴호출 감사 0(`observations` 28k는 별개) | OWASP **MCP08** | **P1** |
| 6 | shadow MCP 인벤토리 | opencode **15개 중 4 enabled**, 감사·드리프트 0 | OWASP **MCP09** | **P1** |
| 7 | tool-poisoning | 내부 툴, 정의 스캔·승인 0(`mcp-contract.json` frozen) | OWASP **MCP03** | **P1** |
| 8 | progressive discovery | opencode allowlist(~33) 프루닝 | 임계(1–5%)·programmatic calling | P2 |
| 9 | OpenTelemetry | structlog JSON + LLM `/metrics` | OTel + **GenAI conv** | **P1** |
| 10 | SLO/error budget | watchdog alert-only 임계만 | SLO + burn-rate | **P1** |
| 11 | policy-as-code | 문서+import-linter+lint_rules(부분) | OPA/Conftest 기계강제 | P2 |
| 12 | canary/feature flag | shadow/병렬+digest 롤백 | canary·flag·DORA | P2 |

---

## 1. SBOM / 서명 / SLSA (P0)

- **As-Is**: `.github/workflows/ci.yml` build 잡이 `docker/build-push-action`으로 GHCR push(서명·SBOM 없음). `Dockerfile` 멀티스테이지 `python:3.12-slim`.
- **표준(2026)**: SBOM(CycloneDX/SPDX) + **cosign/sigstore keyless 서명** + **SLSA/in-toto provenance**가 사실상 의무(EO 14028·CISA attestation·EU CRA). 2025–26 Shai-Hulud npm/PyPI 웜(1,000+ 패키지), 77% 조직이 공급망 사고 경험(Omdia/Palo Alto).
- **context7 검증**: `cosign` keyless sign/verify, `verify-attestation`(identity+OIDC issuer), `syft`가 CycloneDX/SPDX + in-toto 서명 attestation 지원.
- **조치**: CI build 잡에 `anchore/sbom-action`(CycloneDX) → `cosign sign`(keyless, GH OIDC) → provenance(`actions/attest-build-provenance`) 추가. `final-plan` Phase H에 "immutable tag(P4)+서명/attestation" 명시.

## 2. 이미지 / 의존성 스캔 (P0)

- **As-Is**: CI에 스캔 단계 0.
- **표준**: 컨테이너 이미지 스캔(`trivy image`)+SBOM 소스(`--sbom-sources oci/rekor`), Python 의존성(`pip-audit`), GitHub dependency review.
- **context7 검증**: `trivy image --sbom-sources rekor/oci`(SBOM 기반 스캔, CycloneDX 감지).
- **조치**: CI에 `pip-audit`(의존성) + `trivy image`(GHCR 이미지) 추가. 실패 임계(Critical/High) 정책 1p.

## 3. Secretless Workload Identity (P0)

- **As-Is**: `scripts/deploy/kv-fetch-env.py`가 `~/.config/devforge/azure-client-secret`(SP client_secret)로 OAuth 토큰 획득 → **장수명 secret**.
- **표준**: IETF **WIMSE**(workload identity practices, 2026-08), SPIFFE/SPIRE, **workload identity federation**(GitHub OIDC)·**managed identity**. "remove→replace→rotate".
- **조치**: (단기) SP secret 만료 감시+회전(§4). (장기) GitHub Actions→Azure는 **OIDC federation**, 서버 런타임은 **managed identity** 전환 검토. `handover-secrets-kv.md`에 경로 추가.

## 4. Secret Rotation 자동화 (P0)

- **As-Is**: rotation 타이머 0. `kv-backup.timer`(주간)만. SP secret `.bak` 수동 보관.
- **표준**: 만료 **전** 갱신(WIMSE), 회전 트리거(만료 임박), 감사 로그.
- **조치**: SP secret 만료일 기록 + 만료 30/7일 전 알림 타이머(또는 federation 전환 시 소멸). KV 시크릿 회전 절차를 `handover-secrets-kv.md`에 명문화.

## 5. MCP Audit / Telemetry (P1)

- **As-Is**: MCP 툴 호출의 체계적 감사 없음. `observations`(28k)·`activity_log`는 별도 목적.
- **표준**: OWASP **MCP08(Lack of Audit and Telemetry)** — 툴 호출/컨텍스트 변경을 **불변 audit trail**로. NSA MCP 지침(2026-05)도 감사 강조.
- **조치**: MCP 툴 호출을 `observations`(`source='mcp'`, `category='mcp_audit'`)로 기록(error-record §1 L3 재사용 — 단, L3 규정 `source='watchdog'`/`category='watchdog_error'`와 **category로 구분**해 혼선 방지). `ports`/어댑터로 분리(와치독 결합 0). 감사 필드: tool, args_hash, result, ts.

## 6. Shadow MCP 서버 인벤토리 (P1)

- **As-Is**: opencode에 **15개 MCP 서버 설정**(filesystem, search-proxy, devforge-mcp, exa-search, fetch, github, context7, time, shrimp-task-manager, yggdrasil, lsp, token-savior, opencode-db, git, postgres) 중 **4개만 enabled**. 정기 감사·드리프트 점검 없음.
- **표준**: OWASP **MCP09(Shadow MCP Servers)** — 미관리/미승인 서버가 공격면. 인벤토리+승인+주기 감사.
- **조치**: MCP 서버 **승인 인벤토리**(enabled/disabled·소유·목적)를 **신규 `specs/mcp-inventory.yaml`**로 등록(`specs/mcp-contract.json`은 **툴 계약**으로 별개 — 혼용 금지), 주기 드리프트 점검(watchdog oneshot 재사용).

## 7. Tool-poisoning 스캔 (P1)

- **As-Is**: 내부 툴이라 tool 정의 스캔/승인 단계 없음. `specs/mcp-contract.json` frozen(12툴)로 계약은 있음.
- **표준**: OWASP **MCP03(Tool Poisoning)** — 툴 description/schema를 **신뢰하지 않는 입력**으로 취급, 정의 스캔·사람 승인. Microsoft control-plane(툴 정의 스캔) 참조.
- **조치**: `mcp-contract.json` 기반 **정의 diff/계약 테스트**를 CI에 추가(툴 추가·변경 시 승인 필요). 외부 MCP 서버 도입 시 정의 스캔 단계.

## 8. Progressive Discovery / Programmatic Tool Calling (P2)

- **As-Is**: opencode `tools` allowlist로 ~33툴 프루닝(스프롤 해소). 계획은 점진공개 "옵션".
- **표준**: MCP Client Best Practices — 툴 정의가 컨텍스트 **1–5%** 초과 시 **progressive discovery**(`search_tools`), 그리고 **programmatic tool calling**(샌드박스에서 코드로 툴 호출).
- **조치**: 무필터 클라이언트(Claude Code 등) 지원 시 임계(1–5%) 기반 점진공개 도입. programmatic calling은 샌드박스 필요 → 조건부.

## 9. OpenTelemetry + GenAI Semantic Conventions (P1)

- **As-Is**: `structlog` JSON + LLM `/metrics` probe. OTel 0.
- **표준**: OTel 표준화가 2026 핵심. **GenAI semantic conventions**(`gen_ai.usage.*_tokens` 등; semantic-conventions-genai repo로 이동)로 LLM/에이전트 span·token·tool call 추적. agentic observability(감사 trail).
- **context7 검증**: `opentelemetry-semantic-conventions`의 GenAI token 속성 상수 확인.
- **조치**: `trace_id`/`run_id`를 `context_jsonb`·incident에 상관(error-record에 이미 "선택" → 승격). deepdive/extract에 OTel GenAI span 도입(경량, 선택적 exporter).

## 10. SLO / Error Budget Burn-rate (P1)

- **As-Is**: watchdog alert-only 임계(고정). SLO·error budget 0.
- **표준**: SLO + **multi-window burn-rate(MWMBR)** 경보, error budget 정책. "두 번의 나쁜 롤아웃이 30일 예산 88% 소진"(Elastic 2026).
- **조치**: 핵심 서비스(turn-watcher/day-cycle/postgres/mcp) **SLI/SLO** 정의(예: 가용 99%). **전제: 메트릭 백엔드 필요**(Prometheus/OTel collector 등 — 단일 호스트 경량 도입 또는 로컬 집계). 백엔드 도입 전에는 SLI를 watchdog 로컬 집계(가용/실패 카운트)로 근사하고, burn-rate는 백엔드 확보 후. `final-plan`/`watchdog-standard`에 항목 추가.

## 11. Policy-as-code (P2)

- **As-Is**: AGENTS 문서 + `import-linter`(4계약) + `lint_rules` + `code-structure` 양방향 테스트(부분 기계강제).
- **표준**: **policy-as-code**(OPA/Conftest/Kyverno)로 guardrail 자동 강제(identity/RBAC/deploy pipeline).
- **context7 검증**: Conftest Rego 정책 예(비-root·label 강제).
- **조치**: unit/Quadlet 정합·금지 패턴(예: `--privileged`, host publish)을 **Conftest/OPA**로 CI 강제. AGENTS §0/§2 규칙 중 기계화 가능한 것부터.

## 12. Canary / Feature Flag + DORA (P2)

- **As-Is**: shadow/병렬+digest 롤백(<5분). canary·feature flag 0. DORA 미측정.
- **표준**: golden path + **canary/feature flag(FeatureOps)** + release SLO. DORA 4지표(CFR/MTTR/rollback frequency/detection time).
- **조치**: 컷오버 Phase A~I에 "컴포넌트별 canary + feature flag + release SLO gate" 명시. DORA 4지표를 `state.yaml`/리포트에 기록(훅/스크립트).

---

## 13. 우선순위 로드맵

| 단계 | 항목 | 비고 |
|------|------|------|
| **P0** | 1,2,3,4 | 공급망·시크릿. CI/키 관리 중심, 서비스 재기동 최소 |
| **P1** | 5,6,7,9,10 | MCP 거버넌스·관측성·SLO. error-record/watchdog과 연결 |
| **P2** | 8,11,12 | 플랫폼·릴리스. 컷오버 이후 |

> 배치 제약: shadow-run 창(09-24 13:32 UTC) 중 서비스 재기동 금지.
> - **비파괴 선행 가능**: 1·2(CI SBOM/서명/스캔 — CI만 변경), 4의 만료 감시(알림 타이머), 11(policy-as-code CI).
> - **서비스 재기동/키 재구성 수반(창 이후)**: 3(secretless 전환), 4의 실제 rotation, 5~7(MCP 계측/감사), 9(OTel), 10(SLO/메트릭).

## 14. 측정 지표

- 공급망: SBOM 생성률 100%, 서명 커버리지 100%, Critical 취약점 0.
- 시크릿: 장수명 secret 수(목표 0), rotation 자동화율.
- MCP: 툴 정의 토큰 비율 <5%, 감사 커버리지 100%, enabled 서버 인벤토리 일치.
- 관측성/신뢰성: trace 커버리지, SLO 달성률, error budget 소진율, DORA 4지표.

## 15. 검증 방법

- **context7**(`scripts/deploy/kv-fetch-env.py python3 scripts/cli.py research docs "<lib>" "<q>" --keys CONTEXT7-*`): cosign/sigstore, syft, trivy, OTel(GenAI), OPA/Conftest — **검증 완료**.
- **web**: 공급망(EO 14028/CISA/EU CRA, Shai-Hulud), OWASP MCP Top 10, MCP client best practices, platform engineering golden path, IETF WIMSE/workload identity.
- **서버 실측**: `ci.yml`, `Dockerfile`, `kv-fetch-env.py`, `~/.config/opencode/opencode.json`, `systemctl list-timers`.

## 16. 미해결 / 승인 필요

1. 항목별 실제 구현은 각 계획(REFACTORING/final-plan/watchdog-standard)에 **반영 승인** 필요.
2. managed identity/federation 전환은 Azure 권한·SP 재구성 필요(사용자 승인).
3. OTel 도입은 exporter/백엔드 선택 필요(경량 self-host 또는 OTLP endpoint).
4. `specs/mcp-inventory.yaml` 등 **신규 파일** 생성 승인 필요.

---

## 17. 검토·수정 이력 (2026-09-23)

초안 검토에서 발견한 논리·정합성 이슈 6건을 아래와 같이 수정했다(코드=SSOT, 문서를 코드/표준에 정합).

| # | 이슈 | 유형 | 수정 |
|---|------|------|------|
| 1 | §6이 `mcp-contract.json`(툴 계약)을 MCP **서버 인벤토리**로 혼용 | 정합성 | 인벤토리=**신규 `specs/mcp-inventory.yaml`**, contract=툴 계약으로 **명시 분리** |
| 2 | §13 "P0는 비파괴" ← secretless/rotation은 키 재구성 수반 | 논리 | **비파괴(1·2·4감시·11) vs 재기동 수반(3·4회전·5~7·9·10)** 분리 |
| 3 | §10 SLO burn-rate에 메트릭 백엔드 전제 미명시 | 논리(전제) | Prometheus/OTel collector 전제 + 백엔드 전 `로컬 집계 근사` 명시 |
| 4 | §5 MCP audit이 error-record L3(`source='watchdog'`)와 혼선 | 정합성 | `category='mcp_audit'`로 구분 |
| 5 | di-delegation **top-level 폴백**이 toki31 정체를 놓칠 수 있음 | 논리(false negative) | "loop 생존 ≠ toki31 수집" 한계 명시(설계 §8-1 확정 필요) |
| 6 | error-record 코드가 마이그레이션보다 먼저 배포되면 컬럼 부재 오류 | 논리(순서) | **마이그레이션 → 비-dry-run 배포** 순서 게이트 명시 |

---

## 18. minor 항목 심층 (웹 조사·비교)

### 18.1 best-effort 캡처 시 도구 부재 → `command`만 기록 (무해)

- **현재 로직**: `_capture_context_jsonb`가 systemctl/journalctl/podman 부재(예: v2 컨테이너) 시 해당 섹션을 **생략**하고, 시도한 `command`만 남긴다. 오류로 처리하지 않음(best-effort).
- **표준 비교**:
  - CNCF *"You can't debug what you can't see"*(2026-08): 에이전트 관측성 원칙 — **trace backend 불통 시 텔레메트리를 잃을지언정 가용성은 잃지 않는다**. 우리 best-effort와 일치.
  - Graceful degradation 패턴: **"degraded must be visible"** — 부분 결과는 **명시적으로 표시**해야 한다(무표시 partial은 downstream 오해 유발).
  - Panorama(OSDI'18): 컴포넌트가 에러를 *처리만* 하고 *보고하지 않으면* 관측성 저하 → "보고"가 원칙.
- **판단**: 방향은 표준과 일치. 다만 **"어떤 섹션이 누락됐는지"를 명시**하는 편이 표준에 더 부합.
- **권고**: `context_jsonb`에 **`capture_status`(예: `{"systemd":"absent","journal":"ok","container":"absent"}`)** 또는 `degraded: ["systemd","journal"]` 필드 추가 → 무표시 partial 제거. (구현은 별도 승인)

### 18.2 `context`(text) 미기록 → 기존 NULL (무해)

- **현재 로직**: `record_detect`가 신규 행에 `context_jsonb`만 쓰고 text `context`는 NULL. src에 reader 0, legacy watchdog은 자체 기록.
- **표준 비교**(PostgreSQL 공식):
  - PG 오류 필드 표준: **`DETAIL` / `HINT` / `CONTEXT` / `QUERY`**(프로토콜 필드 S/C/M/D/H/W…), `log_error_verbosity`(TERSE/DEFAULT/VERBOSE), **JSON 로그 출력(`jsonlog`)**.
  - 즉 PG 자체가 "text 요약 + 구조화 필드"를 병행하며, **구조화(jsonb) 우선**이 현행.
  - JSONB 운용: GIN 인덱스는 **`jsonb_path_ops`가 더 작고 빠름**(등가성 질의 중심일 때). 현재 `idx_watchdog_incidents_context`는 기본 `jsonb_ops`(GIN) — 경로 질의가 많으면 `jsonb_path_ops` 검토.
- **판단**: text→jsonb 승격은 표준과 일치. 명칭 주의: PG의 `context`(오류 컨텍스트)와 우리 `context`(레거시 text)가 **의미 충돌**하므로 제거가 오히려 명확.
- **권고**: 설계 §3-2대로 **reader 0 확인 후 text `context` 컬럼 제거**(deprecated 마킹 유지). 인덱스는 질의 패턴 확정 후 `jsonb_path_ops` 재검토.

### 18.3 DORA 기록 위치 미정 → 구체화

- **현재 로직**: §12가 "state.yaml/리포트에 기록"으로 모호.
- **표준**(DORA 2026, `dora.dev`·Datadog·IBM):
  - 4 keys: **Deployment frequency / Change lead time / Change failure rate(CFR) / Failed deployment recovery time(MTTR)**.
  - 데이터는 **이벤트 기반**: `Deployment`(service/env/version, `change_failure` bool, `recovery_time`, `remediation_type` rollback/rollforward), `Pull Request`(commit→merge→deploy = lead time), `Incident`.
  - 소규모: **서비스 1개 단위**로 측정, **batch 크기 축소**가 개선 레버.
- **우리 서버 매핑(구체화)**:
  - **CFR·recovery time** ← `watchdog_incidents`(`action_result`/`action_at`/`resolved_at`, `reopen_count`)에서 파생 — 이미 데이터 존재.
  - **Deployment frequency·lead time** ← git/CI(커밋 시각→배포=유닛/이미지 갱신 시각).
  - **기록 위치**: 일일 `state.yaml`의 `dora` 섹션(비파괴) 또는 신규 `dora_metrics` 테이블(additive). 1차는 `state.yaml` 권장.
- **판단**: 이벤트 소스(incidents/git)가 이미 있어 **파생 계산**만 추가하면 됨. 별도 SaaS 불요.
- **권고**: §12를 "`state.yaml.dora` 일일 기록 + `watchdog_incidents`/git에서 파생"으로 구체화.

> **출처(§18)**: CNCF observability for AI agents(2026-08), Panorama OSDI'18, PostgreSQL Error Reporting/JSON log(16/19), PostgreSQL error fields, DORA `dora.dev`(2026-01 갱신)/Datadog DORA data-collected/IBM(2026-06).
