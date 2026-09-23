# 오류 기록·분석 설계 (Deep Dive)

> Status: proposed · 2026-09-23 · Deep Dive `dp-20260923-dataimpulse-monitoring-delegation`
> 목적: 오류를 **구조화하여 자세히 기록**(§1)하고, **별도 분석 로직**이 그 구조를 읽어 전체 문제점을 파악·수정안 제시(§2).
> 관계: 감지·기록은 와치독(`dataimpulse-watchdog-delegation.md`). 본 문서는 **기록 스키마 + 분석 로직**.
> 근거: PostgreSQL `ereport`(severity+detail+hint+context), GCP Error Reporting(stack trace+trace ID), PG `log_error_verbosity`, Supabase/PG Build(over-logging·마스킹·보존), OWASP Agentic A03·NIST AI RMF(감지/결정/복구 분리).

---

## 1. 오류 상세 기록 설계 (감지 계층)

> 오류 발생 시 자세히 기록 → 추후 **분석 로직(§2)** 이 구조를 읽어 전체 문제를 파악할 수 있게 한다.

### 1.1 As-Is 결함 (검증)
| # | 결함 | 근거 |
|---|------|------|
| 1 | `watchdog_incidents`에 **jsonb 컬럼 없음**, `context`=text **500자 잘림** | `incident_pg.py:20`; 레거시는 8000자(`incidents.py:29`) |
| 2 | 실패가 `action_result="fail"` **문자열 하나** — 이유·stderr·exit·traceback 소실 | `incident_pg.py:75-78` |
| 3 | `_capture_context`가 **systemd 속성 4개만** | `incident_pg.py:102-121` (레거시: podman logs+journal 40줄+명령) |
| 4 | repeat/reopen 시 **context 미갱신** | `incident_pg.py:45-64` (레거시 reopen은 갱신) |
| 5 | traceback이 **DB에 안 남음**(journald만) | `orchestrator.py:1035` |
| 6 | 마스킹 **1패턴**(레거시 4패턴) | `incident_pg.py:21` |
| 7 | `observations`(jsonb+GIN 28k)·`activity_log`(jsonb+GIN) **와치독 미사용**, retention 없음 | task 조사 §7,12 |

### 1.2 3계층 기록 구조 (hot/cold 분리)
```
[L1 요약]   watchdog_incidents.symptom        한 줄, 사람·알림용          (유지)
[L2 상세]   watchdog_incidents.context_jsonb  구조화 진단(schema_version)  (text→jsonb 승격)
[L3 원시]   observations(context/tags jsonb)  traceback·journal 원문       (기존 자산 재사용)
```
- L1=자주 조회(요약), L3=가끔 조회(원문) → **hot/cold 분리**(over-logging 방지).
- L2가 L3의 `observations.id`를 참조(`raw_ref`)하여 상세 연결.

### 1.3 L2 구조 (PostgreSQL error field 차용)
```json
{
  "schema_version": 1,
  "captured_at": "2026-09-23T01:00:00Z",
  "component": "svc:ebook-watcher", "event_type": "down", "severity": "ERROR",
  "exit_code": 15,
  "systemd":   {"ActiveState":"failed","SubState":"failed","Result":"exit-code","ExecMainStatus":"15","NRestarts":0},
  "container": {"logs_tail":"...","exit_code":137},
  "journal_tail": ["…40줄…"],
  "exception": {"type":"TimeoutError","message":"…","traceback":"…","frames":["file:line:func"]},
  "command": ["systemctl","--user","show",…],
  "hint": "자격증명 만료 의심",      // PG hint 개념 (수정 로직의 단서)
  "raw_ref": "observations:<uuid>",  // L3 연결
  "truncated": false
}
```

### 1.4 캡처 로직 (표준 매핑)
| 단계 | 구현 | 표준 |
|------|------|------|
| 캡처 | 레거시 수준 복원: systemd 속성 + journal 40줄 + podman logs + **traceback(`format_exc`)** | PG detail/context |
| 분해 | 문자열 X → **구조화 dict**(systemd/container/exception/command 분리) | PG error fields |
| 마스킹 | **4패턴 복원**(bearer·`sk-*`·`password|token|key`·private key) | PG Build, Supabase |
| 크기 | 요약=jsonb(제한 없음), **원문 로그는 L3로 분리** | over-logging 방지 |
| 상관 | `dedup_key`+`detected_at` (+ `run_id`/`trace_id` 선택) | GCP/DD trace correlation |
| repeat | repeat/reopen 시 **context_jsonb 갱신** + `fail_count` 증가 | PG(최신 상태) |
| 실패사유 | `record_action`에 **`action_error` jsonb**(reason/exit/stderr) | PG detail/hint |
| 보존 | `observations` retention(90~180d), resolved incident 180d | Supabase, PG Build |

### 1.5 DB 변경 (additive, 안전)
```sql
ALTER TABLE watchdog_incidents ADD COLUMN context_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE watchdog_incidents ADD COLUMN action_error   JSONB;
CREATE INDEX idx_watchdog_incidents_context ON watchdog_incidents USING gin (context_jsonb);
COMMENT ON COLUMN watchdog_incidents.context_jsonb
  IS 'Structured diagnostics (schema_version, systemd, container, journal_tail, exception, hint). AI-readable.';
-- 기존 context text 는 deprecated → 마이그레이션 후 제거
```
- **신규 테이블 0개**(컬럼 2 + 인덱스 1), L3는 `observations` 재사용.

### 1.6 성공 기준
- [ ] `context_jsonb`에 systemd+container+journal+exception+hint가 **구조화**로 저장(문자열 뭉침 0)
- [ ] traceback이 **DB에 영속**(journald 의존 아님), L3 `raw_ref`로 원문 조회 가능
- [ ] 마스킹 4패턴, repeat/reopen 시 context 갱신, `action_error` 기록
- [ ] retention 정책 존재, GIN 인덱스로 조회 가능
- [ ] §2 분석 로직이 L1~L3만으로 **전체 문제 파악** 가능(추가 질의 없이)

---

## 2. 오류 분석·수정 로직 (별도 계층, 후속)

> 별도 로직(와치독 아님). §1의 **구조화 오류를 입력으로** 전체 문제점을 파악하고 수정 계획을 만든다.
> 근거: OWASP Agentic A03·NIST AI RMF(감지/결정/복구 분리), Debugger role(Kirill Shvakov: RCA + decision packet), NIST CSF 2.0(거버넌스).

### 2.1 책임 경계 (3역할 분리)
```
와치독(감지 계층)  = 감지 + 기록(L1~L3) + 알림      (읽기 전용)
오류 분석 로직     = 구조 분석 → 문제 파악 → 수정안   (읽기 + 계획 생성, 실행 X)
수정 실행          = 승인 게이트 후 실행              (별도, human/process)
```
- 표준: **감지·결정·복구 분리**(OWASP Agentic, NIST RMF). 분석 로직은 **intent-only**(제안만, 실행 권한 없음).

### 2.2 입력 (분석이 읽는 것)
- L1 `watchdog_incidents`(dedup_key, fail_count, reopen_count, status, 전이 이력)
- L2 `context_jsonb`(systemd/container/exception/hint)
- L3 `observations`(traceback·journal 원문)
- (보조) `catchdog_events`(상태 전이 67k행), `action_error`

### 2.3 분석 파이프라인
| 단계 | 내용 | 표준 매핑 |
|------|------|-----------|
| 1. 수집 | L1~L3 + 전이 이벤트로 **문제 후보** 목록화 | evidence 수집 |
| 2. 상관 | dedup_key·시각·component로 **군집화**(동일 근본원인 묶기), 반복/재개 빈도 | trace correlation |
| 3. 근본원인 | exception+exit_code+journal로 **RCA 후보 랭킹**(가설·확률) | Debugger role(RCA) |
| 4. 전체상 | **공통 원인**(예: 만료된 자격증명이 여러 서비스 동시 실패) 식별 → "전체 문제점" 뷰 | failure_class 라우팅 |
| 5. 수정안 | 최소 수정 + **검증 방법 + STOP 조건**(human gate) | runbook/decision packet |
| 6. 산출 | **decision packet**(구조화): evidence·근본원인·fix·verify·risk·confidence | Shvakov decision packet |
| 7. 에스컬레이션 | 불명·모호(가설 delta<20%)·보안·수정실패 → **인간** | escalation criteria |

### 2.4 산출물 (decision packet)
```json
{
  "schema_version": 1,
  "problem_id": "...", "severity": "high",
  "evidence": [{"incident_id": 23, "raw_ref": "observations:<uuid>"}],
  "cluster": {"components": ["svc:ebook-watcher","svc:day-cycle"], "shared_cause_confidence": 0.8},
  "root_cause": {"hypothesis": "프록시 자격증명 만료", "confidence": 0.7, "alternatives": [{"h":"네트워크","confidence":0.2}]},
  "fix_proposal": {"actions": ["KV 시크릿 갱신"], "verify": ["재시도 후 health OK"], "stop_conditions": ["2회 실패 시 중단"], "risk": "low"},
  "decision": "propose|escalate|insufficient_evidence"
}
```

### 2.5 가드레일 (표준 필수)
- **읽기 전용 진단 기본**, 생산 변경은 **승인 게이트** 후(OWASP Agentic A03).
- **근거 필수**: 모든 결론에 `raw_ref`(L3) 연결 — 추측 금지.
- **민감정보**: L3 원문도 마스킹(§1.4) 적용, 프롬프트/알림에 raw 노출 금지.
- **멱등/불변 로그**: 분석 결정·근거·실행 명령을 **불변 기록**(NIST RMF: audit).
- **불확실성 표기**: confidence·alternatives 필수, 모호하면 escalate.

### 2.6 구현 배치
- 별도 모듈(예: `application/error_analysis.py` + `ports`/어댑터), **와치독과 독립 실행**(CLI/타이머).
- 1차는 **읽기+decision packet 생성**만. 수정 실행은 별도 승인 로직.
- 와치독과 **공유 데이터 계약**은 §1 스키마(L1~L3)뿐 — 코드 결합 없음.

### 2.7 성공 기준
- [ ] L1~L3 구조만으로 **군집·근본원인 후보·전체 문제점** 산출
- [ ] 모든 결론에 `raw_ref` 근거, confidence·alternatives 명시
- [ ] decision packet에 **수정안+검증+STOP+risk** 포함, **실행 권한 없음**
- [ ] 불명/모호/보안/수정실패 시 **인간 에스컬레이션**
- [ ] 와치독과 코드 결합 0(데이터 계약만), 가드레일(읽기전용·마스킹·불변기록) 준수

---

## 3. 확정 사항

1. **L3 retention = 90일**. `category='watchdog_error'`, `source='watchdog'` 명명 확정.
2. 기존 `context` text는 **신규 코드가 `context_jsonb`만 쓰고, 기존 `context` 읽는 곳이 0이 된 뒤 제거**.
3. 분석 추론 = **규칙 1차 + LLM 2차(모르는 것만)**. LLM은 OpenRouter **분석용(reasoning) 모델** + **Qwen 8B(local) 폴백**. 상세는 §4.
4. `catchdog_events` 입력 포함 여부 = **§2(분석 로직) 1차 구현 직후 실측 후 결정**
   (L1~L3만으로 "다중 서비스 공통 원인" 커버율 측정 → 20% 미만이면 편입 검토. v2 컷오버 전 판단).

---

## 4. 분석용 모델 선정 개선 (추론 특화)

> 기존 `refresh_openrouter_free_models.py`는 **코딩용**(`coding_index`)만 선정한다. 분석 로직에는 **추론 품질** 기준의 별도 선정이 필요하다.

### 4.1 현황 (검증)
- 파일: `scripts/proxies/refresh_openrouter_free_models.py` (334줄, `devforge-openrouter-free-models.timer` 매일).
- 점수: `_coding_score()` — Artificial Analysis `coding_index` + 컨텍스트 보너스 단일 지표(`:84-108`).
- 선정: 상위 15 live-test(`_test_model`) → org 다양성 top 3 → `~/.config/opencode/opencode-rr.json` **하드코딩**.
- 한계: **추론 품질 무관**, 산출물이 코딩 CLI 설정에 고정.

### 4.2 설계: 역할(role) 프로파일 + 4계층 점수
```python
ROLE_BENCHMARK = {"coding": "coding_index", "reasoning": "intelligence_index"}
ROLE_FALLBACK_WEIGHTS = {
    "coding":    (("agentic_index", 0.6), ("intelligence_index", 0.4)),
    "reasoning": (("coding_index", 0.5), ("agentic_index", 0.5)),
}
```
점수 우선순위(실측 기반, 2026-09-23):
1. **primary index** (coding_index / intelligence_index) 있으면 최우선
2. **보조지표 합성** (primary 없을 때, ×0.9 페널티로 primary 보유 모델이 항상 상위)
3. **AA free API 보강** (누락 지표를 API에서 채움)
4. **키워드 휴리스틱** (캡 29.9, 최후)

- `model_score.py`(신규 공용): `score(model, role)`, `rank()`, `enrich_with_aa()`.
- 추론 역할은 **`intelligence_index`**(AA 블록에 `reasoning_index` 없음 — 실측 확인).

### 4.2b Artificial Analysis free API 연동 (Pro 제외)
- KV: `kv-common-prod-krc` / `ARTIFICIALANALYSIS-API-KEY` → env `ARTIFICIALANALYSIS_API_KEY`.
- 엔드포인트: `GET https://artificialanalysis.ai/api/v2/language/models/free` (`x-api-key`), Free **100 req/24h**.
- 필드: `evaluations.{intelligence,coding,agentic}_index` + `pricing` + `performance`.
- **매칭**: OpenRouter `id` → AA slug(org 제거, `.`→`-`, `:free` 제거, **날짜 접미사 제거**). `canonical_slug`는 날짜가 붙어 부적합 → **`id` 우선**.
- 캐시: `~/.cache/devforge/artificial_analysis.json` (24h TTL).
- **Pro/Commercial 제외**(비용). Free tier만.
- 한계(실측): AA도 모든 자유 모델을 평가하지 않음(지표 보유 10/21) → 나머지는 §4.2 2·4단계로 처리.
- **Attribution 필수**: artificialanalysis.ai 출처 표기.

### 4.3 산출물 분리 (코딩 설정 불변)
```
기존:  ~/.config/opencode/opencode-rr.json          (coding, 유지)
신규:  ~/.config/devforge/analysis_models.json      (reasoning)
```
```json
{ "schema_version": 1, "role": "reasoning", "primary": "openrouter/<reasoning-model>",
  "chain": ["openrouter/<m2>", "openrouter/<m3>"], "fallback_local": "qwen3-8b" }
```
- **품질 우선 + org 다양성** 선정: 최고 품질 1개 → 다른 org의 best로 채움(약한 모델이 org 커버리지 때문에 체인에 들어가지 않게).

### 4.4 주기·폴백
- 분석용은 **주 1회**(또는 모델 불변 시 유지) — 일일 교체는 **결정성 저하**(같은 오류, 다른 결론).
- 폴백 체인: `OpenRouter(primary) → chain → local Qwen 8B → insufficient_evidence`.
- 로컬 폴백은 `devforge-inference`(기존 운영) 재사용. 결과에 `source="local"` + confidence 보수 적용.

### 4.5 구현 형태 (완료)
| 변경 | 파일 |
|------|------|
| 점수 공용화 + role + AA enrichment + 합성 폴백 | `scripts/proxies/model_score.py` (**신규**) |
| `--role coding\|reasoning` + AA 연결 + 품질우선 선정 | `refresh_openrouter_free_models.py` (**기존 동작 불변**) |
| 분석용 산출 | `~/.config/devforge/analysis_models.json` (데이터) |
| 소비 | §2 분석 로직이 **파일만 읽음**(코드 결합 0) |

### 4.6 성공 기준 (검증 완료)
- [x] `--role coding` **동작 불변**, `--role reasoning` 추가 경로
- [x] 4계층 점수(primary→합성→AA→휴리스틱), 지표 보유 모델 우위
- [x] `analysis_models.json` 분리 산출, `opencode-rr.json` **미변경**
- [x] 폴백 체인(OpenRouter→local Qwen) 정의
- [x] **21 단위 테스트**, ruff/mypy clean, e2e 실측(AA 673모델)

---

## 5. 미해결 / 선결

1. ~~추론 지표 확보 경로 확인~~ **확인 완료(2026-09-23)**: AA `intelligence_index` 사용(`reasoning_index` 없음).
2. 분석 로직 §2-3단계의 LLM 입력 토큰 한도·비용 실측(자유 모델 특성).
3. `catchdog_events` 편입 판단 트리거(§3-4) 실측 절차 구체화.
