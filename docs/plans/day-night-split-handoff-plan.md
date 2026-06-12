# DevForge — Day/Night Pipeline 분할 및 DB SSOT Handoff 계획

**Version**: 1.0
**Date**: 2026-06-08
**Status**: DRAFT (실험 종료 후 구현)
**Language**: Korean (user-facing), English (code/schema)

---

## 1. 문제 정의

### 1.1 현재 구조의 문제점

```
prj_cycle.py (2,142줄) — 단일 파일, 단일 실행
┌─────────────────────────────────────────────────────┐
│  Phase -1: extract (3B)                             │
│  Phase  0: py_verify (Python)                       │
│  Phase  1: day_verify (7B)                          │
│  Phase  2: rubric (7B)                              │
│  Phase  3: P-R-J (30B-14B-14B) — Pod B 스왑 필요    │
│  Phase 3.5: handoff (14B)                           │
│  Phase  4: night_verify (27B)                       │
└─────────────────────────────────────────────────────┘
        ↑ 실행 중 ERROR 발생 시 전체 재시작
        ↑ Pod B 스왑 4회 (day→30B→14B→14B→27B)
        ↑ extract 실패해도 P-R-J 재시도 불가능
```

**4가지 문제:**
1. **단일 장애점**: extract부터 night_verify까지 하나의 프로세스. 중간에 죽으면 전부 재시작
2. **Pod B 스왑 비효율**: day mode(3B) → P(30B) → R(14B) → J(14B) → V(27B)로 4회 스왑
3. **handoff 일관성 없음**: JSON 파일 기반 (`pipeline_state_{tag}.json`). race condition 위험
4. **runner.py 역할 과다**: 실험 오케스트레이션 + 컨테이너 관리 + 파이프라인 실행 + 메트릭 추출 + Slack 리포팅. LLM이 혼동하여 수정 시 사이드 이펙트 발생

### 1.2 제안하는 구조

```
              ┌──────────────────────────────────────────────────────┐
              │                  exp_runner.py                       │
              │  (실험 오케스트레이션: phase loop, retry, 비교)      │
              └──────┬─────────────────────────────┬────────────────┘
                     │                             │
        ┌────────────▼─────────┐     ┌─────────────▼──────────────┐
        │    day_runner.py     │     │     night_runner.py        │
        │  (Pod A + 3B 유지)   │     │  (Pod B 스왑: 30B→14B→27B) │
        │  day_pipeline.subproc│     │  night_cycle.subproc    │
        └────────────┬─────────┘     └─────────────┬──────────────┘
                     │                             │
        ┌────────────▼─────────┐     ┌─────────────▼──────────────┐
        │   day_pipeline.py    │     │    night_cycle.py       │
        │  (3B/7B만 사용)      │     │  (30B/14B/14B/27B 사용)     │
        │  extract → py_verify │     │  DB handoff 로드           │
        │  → day_verify        │     │  → P-R-J → night_verify   │
        │  → day_review → DB   │     │  → DB 저장 → Pod B 복원    │
        │  Pod B 3B 유지       │     │                             │
        └──────────┬───────────┘     └─────────────────────────────┘
                   │ DB (activity_log)
                   │ type='day_review', source='day_pipeline'
                   │ body: {findings, verdict, scores, ...}
                   ▼
            [night_cycle.py가 DB에서 읽음]

공통 라이브러리 (lib/):
  lib/infra/container_manager.py  — start/stop/memory/health
  lib/runner/snapshot.py          — save/restore/transform
  lib/runner/metrics.py           — 추출 + Slack 리포팅
  lib/infra/pod_manager.py        — pod lifecycle (기존)
  lib/prompt_builder.py           — 프롬프트 SSOT (roadmap Phase 2)
```

---

## 2. 통합 대상: 기존 계획 문서

### 2.1 code-size-refactoring.md Phase 1 (병행)

prj_cycle.py 분할은 아래 추출 작업과 함께 진행:

| 추출 대상 | 대상 파일 | 예상 라인수 | 비고 |
|-----------|----------|------------|------|
| Pod 관리 | `lib/infra/pod_manager.py` | ~300 | `kill_all()`, `start_pod_a()`, `start_pod_b()`, `ensure_model()` 등 |
| TokenBudget + PipelineState | `lib/llm/token_budget.py` | ~280 | 상태 머신, 컨텍스트 빌더 |
| llm_call 래퍼 | `lib/llm_client.py` (기존) | — | 이미 존재, call_one() 통합 |
| JSON 스키마 검증 | `lib/llm/json_parser.py` (기존) | — | 이미 존재 |

⇒ **prj_cycle.py 사라짐. day_pipeline.py + night_cycle.py 생성.**

### 2.2 refactoring-roadmap.md Phase 2 (순차)

Prompt centralization은 day/night 분할 **이후** 진행:
- `prompt_builder.py`가 day_pipeline.py + night_cycle.py 모두에서 사용
- PROPOSER_SYSTEM_PROMPT, REFLECTOR_SYSTEM_PROMPT 등을 prompt_builder.py로 통합
- 각 phase별 flag (`--rubric-off`, `--feedback-off`)는 prompt_builder.py의 파라미터로 전환

### 2.3 phases.md — Tier 3 Observer HUD

day/night 분할이 완료되면 Observer가 개별 파이프라인 상태를 모니터링:
- day_pipeline: extract 진행도, day_verify 결과
- night_cycle: P-R-J 진행도, night_verify 결과
- DB handoff 레코드를 직접 조회하여 가시화

---

## 3. 테스트 구조 분리

### 3.1 현황 (문제)

테스트 파일이 소스와 섞여 있거나 평면 구조로 방치됨:

```
scripts/
├── pipelines/
│   ├── day_cycle.py          ← prod
│   ├── prj_cycle.py          ← prod (곧 삭제)
│   ├── runner.py             ← prod (곧 삭제)
│   └── ...                   ← prod
├── lib/
│   └── test_sandbox.py       ← TEST가 lib/에 위치 (!)
└── test_r_role_dsv2.sh       ← TEST가 scripts/에 위치 (!)

tests/
├── test_dart_round.py        ← 평면, 어떤 모듈 테스트인지 불명확
├── test_verify_pipeline.py   ← 평면
├── test_verify_proposer.py   ← 평면
├── test_verify_reflector.py  ← 평면
├── test_extraction_faithfulness.py
├── test_small_verify.py
└── ...                       ← 14개 .py 전부 평면
```

**문제점:**
1. 소스 구조를 반영하지 않아 테스트 대상 파악이 어려움
2. `test_sandbox.py`가 `lib/`에 있어 prod 코드와 혼재
3. `.sh` 테스트가 `scripts/` 루트에 방치
4. `__pycache__`만 남고 소스가 사라진 파일들 (`scripts/__pycache__/test_*.pyc`)

### 3.2 목표 구조

소스 디렉토리 구조를 그대로 반영:

```
tests/
├── pipelines/
│   ├── day/
│   │   ├── test_extract.py         ← extract 단위 테스트
│   │   ├── test_day_verify.py      ← day_verify 단위 테스트
│   │   └── test_day_pipeline.py    ← day_pipeline 통합 테스트
│   ├── night/
│   │   ├── test_proposer.py        ← P 역할 테스트
│   │   ├── test_reflector.py       ← R 역할 테스트
│   │   ├── test_judge.py           ← J 역할 테스트
│   │   ├── test_night_verify.py    ← night_verify 테스트
│   │   └── test_night_cycle.py  ← night_cycle 통합 테스트
│   └── legacy/
│       └── test_prj_cycle.py       ← prj_cycle.py 제거 전 보관
├── runners/
│   ├── test_day_runner.py
│   ├── test_night_runner.py
│   └── test_exp_runner.py
├── lib/
│   ├── test_sandbox.py             ← scripts/lib/ 에서 이동
│   ├── test_container_manager.py
│   ├── test_token_budget.py
│   └── test_pod_manager.py
└── conftest.py                     ← 공통 fixture
```

### 3.3 마이그레이션 규칙

| 출발 | 도착 | 규칙 |
|------|------|------|
| `tests/test_verify_pipeline.py` | `tests/pipelines/night/test_night_verify.py` | night_verify 테스트 |
| `tests/test_verify_proposer.py` | `tests/pipelines/night/test_proposer.py` | P 역할 테스트 |
| `tests/test_verify_reflector.py` | `tests/pipelines/night/test_reflector.py` | R 역할 테스트 |
| `tests/test_small_verify.py` | `tests/pipelines/night/test_night_verify.py` 통합 | 기존 통합 |
| `tests/test_extraction_faithfulness.py` | `tests/pipelines/day/test_extract.py` | extract 테스트 |
| `tests/test_handoff_quality.py` | `tests/pipelines/night/` | handoff 테스트 |
| `tests/test_dart_round.py` | `tests/runners/` | runner 테스트 |
| `tests/test_text_quality.py` | `tests/lib/` | lib 테스트 |
| `tests/test_operator*.py` | `tests/lib/` | operator 테스트 |
| `tests/test_q4_quantization.py` | `tests/lib/` | quantization 테스트 |
| `tests/test_proposer_quantization.py` | `tests/lib/` | quantization 테스트 |
| `tests/test_reflector_comparison.py` | `tests/lib/` | 비교 테스트 |
| `tests/test_extract_quantized.py` | `tests/lib/` | quantization 테스트 |
| `tests/test_verify_mcp.py` | `tests/pipelines/day/test_extract.py` 통합 | MCP 테스트 |
| `tests/test_verify_optimized_params.py` | `tests/lib/` | params 테스트 |
| `scripts/lib/test_sandbox.py` | `tests/lib/test_sandbox.py` | prod에서 분리 |
| `scripts/test_r_role_dsv2.sh` | `tests/legacy/` | shell 테스트 |

---

## 4. 업계 사례 검증

### 4.1 검증 대상

| 참고 | 분야 | 핵심 내용 |
|------|------|----------|
| Ellipsis (ZenML LLMOps DB) | Production LLM 코드 리뷰 | 수평적 확장, 비동기 처리, DB handoff, graceful degradation |
| Pipes & Filters (Azure Architecture) | 파이프라인 아키텍처 | 모놀리스 분할 원칙, idempotency, schema contract |
| Sherlock (Sourcegraph) | 보안 코드 리뷰 LLM | enrichment-before-inference, multi-stage pipeline |
| SSOT (업계 표준) | Single Source of Truth | DB 중심 데이터 일관성, canonical ID, versioned schema |
| Idempotent Pipeline (Data 업계) | 중복 방지 | partition overwrite, idempotency key, checkpoint |

### 4.2 Ellipsis (Production LLM Code Review)

**URL:** https://www.zenml.io/llmops-database/building-and-deploying-production-llm-code-review-agents

**구조 요약:**
```
GitHub Webhook → Hookdeck → FastAPI → Hatchet Queue → Clone Repo →
  Parallel Generators (GPT-4o + Claude Sonnet) → 
  Filter Pipeline (dedup → confidence → hallucination → editing) → 
  Post comments + feedback loop
```

**계획안과 일치하는 항목:**

| 항목 | Ellipsis | 우리 계획 |
|------|----------|----------|
| **Generator 분리** | "dozens of smaller agents, independently benchmarked" | day_review(7B-3B-7B) / P-R-J(30B-14B-14B) 분리 |
| **Filter stage** | "Filter Pipeline: dedup → confidence → hallucination" | day_verify(7B) 가 lightweight filter 역할 |
| **Async 처리** | "async workflows: latency becomes secondary to accuracy" | day→night 분리로 day는 빠르게, night는 정확하게 |
| **Component isolation** | "if one fails, others continue" | day 실패 → night skip (부분 성공 허용) |
| **Graceful degradation** | "exit gracefully, submit what they learned" | day 결과만으로도 가치 있음, night는 선택적 |
| **DB handoff** | Hatchet queue + DynamoDB cache | activity_log JSONB body |
| **LLM retry** | "simple retries and timeouts" | 3-level recovery + 최대 3회 retry |

**계획안에서 보강할 점 (Ellipsis 기준):**
- **Hallucination detection layer**: Filter Pipeline에 hallucination 검증 단계 명시 필요. 지금은 day_verify가 검증하지만, hallucination 전용 단계가 없음
- **Confidence thresholding**: 명시적 confidence 임계값 정의. `confidence < 70 → night_review에서 집중 검증`이 아니라 `confidence < 50 → reject` 같은 hard threshold

### 4.3 Pipes & Filters (Azure Architecture Center)

**URL:** https://learn.microsoft.com/en-us/azure/architecture/patterns/pipes-and-filters

**핵심 원칙:**

> "Break down complex processing into separate components (filters). Filters are independent, self-contained, typically stateless. They receive messages from an inbound pipe and publish messages to a different outbound pipe."

**계획안 검증:**

| Pipes & Filters 원칙 | 우리 계획 | 판정 |
|----------------------|----------|------|
| Independent filters | day_pipeline / night_cycle 완전 분리 | ✅ 합격 |
| Well-defined I/O schema | activity_log JSONB body에 schema_version | ✅ 합격 |
| Message tolerance (pass-through) | day 결과를 night가 읽기만 하고 day는 건드리지 않음 | ⚠️ schema contract 명시 필요 |
| Idempotency | 아직 명시 안 됨 | ❌ **보강 필요** |
| Error: fail vs propagate | 섹션 4.6 에러 처리에 정의 | ✅ 합격 |
| Reusability | day_runner / night_runner는 pipeline subprocess만 실행 | ✅ 합격 |

**계획안에서 보강할 점 (Pipes & Filters 기준):**

1. **Idempotency — 가장 중요**
   - 문제: runner가 day_pipeline을 재시도하면 `INSERT INTO activity_log`가 중복 실행됨
   - 해결: `INSERT ... ON CONFLICT (run_id) DO UPDATE` 또는 `SELECT ... FOR UPDATE` 선행 체크
   - 추가: `run_id`에 UNIQUE 제약 조건 또는 upsert 패턴 필요

2. **Schema Contract**
   - 현재 `body JSONB`에 암시적 스키마만 있음
   - 필요: day_review body의 JSON Schema를 night_cycle이 import해서 검증
   - Pipes & Filters: "filters are only aware of their input and output schemas"

3. **Duplicate message detection**
   - runner 재시작 → 같은 run_id로 INSERT 시도 → 중복 레코드
   - 해결: `INSERT ... WHERE NOT EXISTS (SELECT 1 FROM activity_log WHERE run_id = X AND type = 'day_review')`

### 4.4 Sherlock (Sourcegraph)

**URL:** https://sourcegraph.com/blog/lessons-from-building-sherlock-automating-security-code-reviews-with-sourcegraph

**Sherlock 구조:**
```
Trigger (GitHub App) → Enrichment (cross-file context) → 
  Correlation (SAST + LLM) → Output (severity-prioritized)
```

**계획안 검증:**

| Sherlock 교훈 | 우리 계획 | 판정 |
|---------------|----------|------|
| "enrichment before inference" | extract(py_verify) → day_verify 순서 | ✅ 합격 |
| "correlate scanner + LLM" | py_verify(구조 검증) + day_verify(LLM) | ⚠️ correlation 방식 명확하지 않음 |
| "hallucination is real" | day_verify가 confidence 계산 | ✅ 있음 |
| "models suggest best practices not edge cases" | P-R-J가 deep review | ✅ night에서 해결 |

**계획안에서 보강할 점 (Sherlock 기준):**
- **Scanner-LLM correlation 명시**: py_verify 결과와 day_verify 결과를 어떻게 병합할지 구체화 필요. 지금은 "py_verify → day_verify" 순차 실행만 있고, 두 결과의 correlation이 없음

### 4.5 SSOT (Single Source of Truth)

**업계 원칙:**
1. **하나의 canonical location** — activity_log가 day/night handoff의 유일한 통로
2. **Versioned schema** — `body.schema_version` 필드 포함
3. **Lossless mapping** — day 결과가 night로 전달될 때 데이터 손실 없음
4. **Append-only log pattern** — activity_log는 INSERT-only, UPDATE 없음 (기존 스키마 특성)

**계획안 검증:**

| SSOT 원칙 | 우리 계획 | 판정 |
|-----------|----------|------|
| Canonical location | activity_log (type='day_review') | ✅ |
| Versioned schema | schema_version: 1 | ✅ |
| Lossless mapping | JSONB body에 전체 결과 직렬화 | ✅ |
| Append-only | activity_log는 INSERT-only | ✅ |
| Validation on write | `_log_schema_warnings()` 존재 | ✅ |
| No silent data drop | 모든 finding이 handoff에 포함 | ⚠️ 확인 필요 |

**계획안에서 보강할 점 (SSOT 기준):**
- **Schema version migration 전략**: schema_version=1→2로 업그레이드할 때, night_cycle이 두 버전을 모두 읽을 수 있어야 함. 현재 "parser 분기"만 언급됐을 뿐 구체적 계획 없음

### 4.6 Idempotency & 중복 방지

**업계 표준 패턴:**

1. **Partition overwrite** — `DELETE FROM activity_log WHERE run_id=X AND type='day_review'` 후 INSERT (가장 간단)
2. **Idempotency key** — `run_id + type`이 복합 키
3. **Checkpoint table** — pipeline_checkpoints 테이블에서 마지막 처리 run_id 추적
4. **Upsert** — `INSERT ... ON CONFLICT DO NOTHING`

**계획안에서 보강할 점:**

```sql
-- Partition overwrite 패턴 (권장)
BEGIN;
  DELETE FROM activity_log 
  WHERE run_id = '{run_id}' AND type = 'day_review';
  
  INSERT INTO activity_log (type, source, run_id, body, ...)
  VALUES ('day_review', 'day_pipeline', '{run_id}', '{body}', ...);
COMMIT;

-- 또는 idempotency key 기반 (더 안전)
INSERT INTO activity_log (type, source, run_id, body, ...)
SELECT 'day_review', 'day_pipeline', '{run_id}', '{body}'::jsonb, ...
WHERE NOT EXISTS (
  SELECT 1 FROM activity_log 
  WHERE run_id = '{run_id}' AND type = 'day_review'
);
```

**Idempotency가 깨지는 시나리오:**
1. day_pipeline 성공 → activity_log INSERT → runner crash (DB 저장은 완료)
2. runner 재시작 → 같은 run_id로 day_pipeline 재실행
3. pipeline이 다시 INSERT 시도 → 중복 day_review 레코드
4. night_cycle이 DB 조회 → `ORDER BY id DESC LIMIT 1` → 최신 중복 레코드 선택 (데이터 일관성은 유지되지만 오버헤드)

### 4.7 검증 결론

**✅ 계획이 올바른 방향:**
- Pipes & Filters 패턴을 정확히 따름 (모놀리스 분할, 독립 필터, 명확한 I/O)
- Ellipsis 등 Production LLM 시스템과 동일한 아키텍처 결정 (async processing, component isolation)
- DB SSOT 접근법은 업계 표준과 일치

**⚠️ 보강이 필요한 5개 항목:**

| # | 항목 | 중요도 | 설명 |
|---|------|--------|------|
| 1 | **Idempotency 보장** | 🔴 Critical | day_pipeline 재시도 시 activity_log 중복 INSERT 방지 |
| 2 | **Schema contract 명시** | 🟡 Medium | day→night JSON 스키마를 코드로 검증 (night_cycle이 import) |
| 3 | **Hallucination detection layer** | 🟡 Medium | Filter Pipeline에 hallucination 검증 단계 추가 |
| 4 | **Confidence threshold 구체화** | 🟢 Low | hard threshold vs soft threshold 정책 정의 |
| 5 | **Scanner-LLM correlation** | 🟢 Low | py_verify + day_verify 결과 병합 로직 명시 |

---

## 5. 상세 설계

### 5.1 파일 목록

| 파일 | 상태 | 역할 | 예상 라인수 |
|------|------|------|------------|
| **Pipeline** | | | |
| `scripts/pipelines/day_pipeline.py` | **NEW** | extract → py_verify → day_verify → day_review | ~800 |
| `scripts/pipelines/night_cycle.py` | **NEW** | night_review(P-R-J) → night_verify → 최종 저장 | ~1,000 |
| `scripts/pipelines/prj_cycle.py` | **DELETE** | legacy (day+night 통합) | — |
| **Runner** | | | |
| `scripts/pipelines/day_runner.py` | **NEW** | day_pipeline 단독 실행, Pod A+B(3B) 관리 | ~150 |
| `scripts/pipelines/night_runner.py` | **NEW** | night_cycle 실행, Pod B 스왑(30B→14B→27B) | ~250 |
| `scripts/pipelines/exp_runner.py` | **NEW** | 5-phase 실험 오케스트레이션, 스냅샷, 비교 | ~300 |
| `scripts/pipelines/runner.py` | **MODIFY→DELETE** | → exp_runner.py로 이관 후 삭제 | ~680→0 |
| **Library (shared)** | | | |
| `lib/infra/container_manager.py` | **NEW** | start/stop/memory/health (runner.py→추출) | ~200 |
| `lib/runner/snapshot.py` | **NEW** | save/restore/transform (runner.py→추출) | ~100 |
| `lib/runner/metrics.py` | **NEW** | extract_metrics + Slack 리포팅 | ~150 |
| `lib/infra/pod_manager.py` | **기존** | pod lifecycle (code-size Phase 1) | ~300 |
| `lib/llm/token_budget.py` | **기존** | TokenBudget + PipelineState (code-size Phase 1) | ~280 |
| `lib/prompt_builder.py` | **기존** | 프롬프트 SSOT (roadmap Phase 2) | ~80 |

### 5.2 DB Handoff 스키마

`activity_log` 테이블의 JSONB `body` 컬럼을 활용:

```sql
-- day_pipeline 결과 저장 (runner.py → INSERT)
INSERT INTO activity_log (
    type, source, title, summary, body,
    run_id, exec_status
) VALUES (
    'day_review',
    'day_pipeline',
    'Day Review: {tag}',
    'day_verify={verdict} confidence={conf} findings={n}',
    '{
        "phase": 0,
        "extract": {"processed": 10, "facts": 24, "failed": 2},
        "py_verify": {"issues": 1, "findings": 8},
        "day_verify": {
            "verdict": "approved_with_conditions",
            "confidence": 95,
            "verification_items": [...]
        },
        "day_review": {
            "findings": [...],
            "verdicts": [...],
            "scores": {"P": 85, "R": 78, "consensus": 82}
        },
        "pipeline_state_path": "data/experiment/pipeline_state_r1_norubric.json"
    }',
    '{run_id}',
    'DONE'
);

-- night_cycle이 DB에서 조회
SELECT id, body, run_id
FROM activity_log
WHERE type = 'day_review'
  AND source = 'day_pipeline'
  AND exec_status = 'DONE'
  AND run_id = '{run_id}'
ORDER BY id DESC LIMIT 1;
```

**Schema 버전 관리:** `body` JSON에 `"schema_version": 1` 필드 포함.
변경 시 버전을 올리고 night_cycle에서 version별 파서 분기.

### 5.3 day_pipeline.py 데이터 흐름

```
run_extract()
  ├─ Pod A(7B:8082) 시작 + Pod B(3B:8080) 확인 (기존 Pod B running 유지)
  ├─ extract_pipeline(skip_mcp=True)
  │   └─ results: {processed, failed, facts}
  └─ 반환

run_py_verify()
  ├─ Python 구조 검증 (ast.parse, SQL injection 등)
  └─ 반환: {issues, findings}

run_day_verify(context)
  ├─ Pod A(7B:8082) ensure (skip_if_healthy=True)
  ├─ call_one("day_verify", ...) → JSON
  └─ 반환: {verdict, confidence, verification_items}

run_day_review(context)
  ├─ call_one("day_review", ...) → JSON
  │   ├─ P 역할: day_verify 결과 기반 finding 분류
  │   ├─ R 역할: finding 채택/기각
  │   └─ J 역할: 종합 점수 + handoff
  └─ 반환: {findings, verdicts, scores}

저장(run_id, results)
  ├─ INSERT INTO activity_log (type='day_review', ...)
  └─ pipeline_state_{tag}.json (fallback)
```

**Pod B 스왑 없음.** 3B + 7B만으로 day_review 완료.

### 5.4 night_cycle.py 데이터 흐름

```
main(run_id, tag)
  ├─ DB 조회: day_review 결과 로드
  │   SELECT body FROM activity_log WHERE type='day_review' AND run_id='{run_id}'
  │   └─ 실패 시 exit (without retry)
  │
  ├─ Pod A stop (RAM 확보)
  │
  ├─ night_review (P-R-J)
  │   ├─ P(30B): day_review 결과 기반 심층 분석
  │   ├─ R(14B): finding 채택/기각 + rejected_findings audit trail
  │   ├─ J(14B): P/R 점수 + handoff
  │   ├─ R handoff writer(14B): 최종 handoff 문서
  │   └─ save: handoff_{llm,py,r}.json
  │
  ├─ night_verify (27B)
  │   ├─ call_one("night_verify", ...) → JSON
  │   └─ save_feedback_to_db() → activity_log (기존 유지)
  │
  ├─ 최종 결과 → activity_log
  │   INSERT INTO activity_log (type='night_review', ...)
  │
  └─ Pod B → day mode 복원
```

### 5.5 Runner 분할: day_runner / night_runner / exp_runner

**변경 전 runner.py (680줄, 6가지 역할):**
```
runner.py
├── Snapshot 관리 (save/restore/transform)  ← 실험 전용
├── 컨테이너 관리 (stop/memory/health)       ← 전역 공통
├── Pod B 모드 쓰기                         ← Pod B 전용
├── 파이프라인 실행 (Popen+SIGKILL)          ← 실행기
├── 메트릭 추출 + Slack 리포팅              ← 공통
└── 실험 오케스트레이션 (phase loop, retry) ← 실험 전용
```

**변경 후 — 3개 runner + 3개 lib:**
```
exp_runner.py (실험 전용, ~300줄)
├── prepare_all_phases() → snapshot.py.save/restore/transform 호출
├── run_experiment(phases) → phase loop + retry
│   ├── 각 phase: day_runner.py → night_runner.py 순차 호출
│   └── Pod B 스왑: night_runner.py가 직접 관리
├── generate_comparison_report() → metrics.py 호출
└── Slack 알림: metrics.py.send_slack() 호출
        ↓ import
lib/runner/snapshot.py       — save/restore/apply_transform
lib/runner/metrics.py        — extract_metrics + Slack

day_runner.py (단순 실행, ~150줄)
├── run(run_id, tag, flags)
│   ├── Pod A(7B:8082) + Pod B(3B:8080) 확인 (container_manager.py)
│   ├── subprocess: day_pipeline.py → Popen+SIGKILL fallback
│   └── 메트릭 추출 + Slack 전송
└── exit code만 반환 (성공/실패)
        ↓ import
lib/infra/container_manager.py — start/stop/wait_health/_free_memory

night_runner.py (Pod B 스왑 관리, ~250줄)
├── run(run_id, tag, flags, day_handoff_id)
│   ├── DB에서 day_review 결과 로드
│   ├── Pod A stop (RAM 확보)
│   ├── Pod B 스왑: 30B(proposer) → 14B(reflector) → 14B(judge) → 27B(verifier)
│   │   └── 각 스왑: container_manager.stop() → _write_mode() → container_manager.start()
│   ├── subprocess: night_cycle.py → Popen+SIGKILL fallback
│   ├── Pod B → day mode 복원
│   └── 메트릭 추출 + Slack 전송
└── exit code만 반환 (성공/실패)
        ↓ import
lib/infra/container_manager.py — start/stop/wait_health/_free_memory
```

**핵심 원칙:**
- `day_runner.py`는 Pod B를 건드리지 않음 (day mode running인지만 확인)
- `night_runner.py`가 Pod B 스왑의 모든 책임을 가짐
- `exp_runner.py`는 "어떤 runner를 언제 실행할지"만 결정
- 각 runner는 subprocess로 pipeline을 실행하고 exit code만 반환
- 공통 로직은 lib/에 있어 LLM이 수정해도 다른 runner에 영향 없음

### 5.6 에러 처리 전략

| 실패 지점 | 영향 | 회복 전략 |
|-----------|------|----------|
| Pod A 시작 실패 | day_pipeline 불가 | day_runner → container_manager.recover_and_restart() (3-level) |
| extract 실패 | day_pipeline만 재시작 | day_runner가 retry |
| day_review 실패 | day_pipeline만 재시작 | day_runner가 retry |
| day_review DB 저장 실패 | night_cycle 불가 | pipeline_state.json fallback |
| Pod B night 모델 스왑 실패 | night_cycle 불가 | night_runner가 retry (기존 3-level 활용) |
| P-R-J 실패 | night_cycle만 재시작 | night_runner가 retry |
| night_verify 실패 | night_cycle만 재시작 | 최대 3회 retry |
| day_runner 성공 + night_runner 실패 | Phase 부분 성공 | exp_runner가 다음 phase로 진행 (부분 성공 허용) |

---

## 6. 실행 단계

### Phase A: 공통 라이브러리 추출

**작업:** runner.py에서 `container_manager.py`, `snapshot.py`, `metrics.py` 추출
**파일:** `lib/infra/container_manager.py`, `lib/runner/snapshot.py`, `lib/runner/metrics.py`
**기존 코드:** runner.py의 ~450줄 이동 (start/stop/memory/health, save/restore/transform, extract_metrics/Slack)
**이유:** 3개 runner(day/night/exp)가 모두 사용하므로 먼저 추출
**위험:** 낮음 — pure extraction, 로직 변경 없음
**테스트:** `python3 -c "from lib.infra.container_manager import _free_memory, wait_health"`
**의존성:** runner.py import 경로 변경, 기존 동작 유지

### Phase B: 기반 추출 (기존 code-size Phase 1과 병행)

**작업:** pod_manager.py, token_budget.py 추출
**파일:** `lib/infra/pod_manager.py`, `lib/llm/token_budget.py`
**기존 코드:** prj_cycle.py의 ~500줄 이동
**위험:** 낮음 — pure extraction, 로직 변경 없음
**테스트:** 추출 후 python3 -c "from lib.infra.pod_manager import ..."
**의존성:** runner.py import 경로 업데이트 필요

### Phase C: day_pipeline.py + day_runner.py 분할

**작업:** prj_cycle.py → day_pipeline.py, runner.py → day_runner.py
**파일:** `scripts/pipelines/day_pipeline.py`, `scripts/pipelines/day_runner.py` (신규)
**기존 코드:** prj_cycle.py의 day 부분 (~800줄) + runner.py의 실행 부분 (~100줄)
**위험:** 중간 — day_review LLM call (7B-3B-7B)은 기존 P-R-J와 다른 구조
**테스트:** `python3 day_runner.py --run-id test --tag r1 --flags "--structural --rubric-off"`

### Phase D: night_cycle.py + night_runner.py 분할

**작업:** prj_cycle.py → night_cycle.py, runner.py → night_runner.py
**파일:** `scripts/pipelines/night_cycle.py`, `scripts/pipelines/night_runner.py` (신규)
**기존 코드:** prj_cycle.py의 night 부분 (~1,000줄) + runner.py의 Pod B 스왑 부분 (~150줄)
**위험:** 중간 — DB handoff 로딩 로직 추가 필요
**테스트:** `python3 night_runner.py --run-id test --handoff-id 42 --tag r1`

### Phase E: exp_runner.py 생성

**작업:** runner.py의 실험 오케스트레이션 부분 → exp_runner.py
**파일:** `scripts/pipelines/exp_runner.py` (신규)
**기존 코드:** runner.py의 ~300줄 (run_experiment, prepare_all_phases, generate_comparison_report)
**변경:** day_runner + night_runner를 순차 호출하도록 재구성
**위험:** 낮음 — 오케스트레이션 로직만 이관
**테스트:** `python3 exp_runner.py --phase 0 --dry-run`

### Phase F: prj_cycle.py + runner.py 제거

**작업:** 모든 참조를 새 파일로 교체
**파일:** `scripts/pipelines/prj_cycle.py` → 삭제, `scripts/pipelines/runner.py` → 삭제
**참조 확인:** `exp_runner.py`, `night_cycle.sh`, `15m_cycle.sh`, `prj_watchdog.py`(삭제됨)
**위험:** 낮음 — 모든 참조 교체 후 삭제

### Phase G: 테스트 구조 정리

**작업:** `tests/` 평면 구조 → 소스 미러링 구조로 재배치
**파일:** `tests/pipelines/day/`, `tests/pipelines/night/`, `tests/runners/`, `tests/lib/` (신규)
**기존 코드:** `tests/test_*.py` 14개 + `scripts/lib/test_sandbox.py`
**규칙:** 섹션 3.3 마이그레이션 규칙에 따라 1:1 이동 또는 통합
**위험:** 낮음 — pure file move, import 경로만 수정
**테스트:** `python3 -m pytest tests/ -x --tb=short` (전체 통과)

---

## 7. 마이그레이션 전략

### 7.1 점진적 전환 (권장)

day_pipeline.py + night_cycle.py를 prj_cycle.py와, exp_runner.py를 runner.py와 **병행 운영:**

```
 1. container_manager.py + snapshot.py + metrics.py 추출 (runner.py import 변경)
 2. pod_manager.py + token_budget.py 추출 (prj_cycle.py import 변경)
 3. day_pipeline.py + day_runner.py 생성 (prj_cycle.py day 부분 복사)
 4. night_cycle.py + night_runner.py 생성 (prj_cycle.py night 부분 복사)
 5. exp_runner.py 생성 (runner.py 실험 부분 복사)
 6. 실험: day_runner.py 단독 실행 → DB 결과 확인
 7. 실험: night_runner.py 단독 실행 (수동 handoff 주입)
 8. 실험: exp_runner.py가 day_runner + night_runner 순차 실행
 9. 모든 테스트 통과 → prj_cycle.py + runner.py 제거
```

### 7.2 DB 스키마 변경

**변경 없음.** `activity_log` 테이블 기존 스키마 그대로 사용.
- `type='day_review'` — 신규 타입, 기존 인덱스 영향 없음
- JSONB `body` 컬럼에 handoff 데이터 저장

### 7.3 시스템 서비스 영향

| 서비스 | 현재 | 변경 후 |
|--------|------|--------|
| `night_cycle.sh` | `prj_cycle.py` 호출 | `day_runner.py → day_pipeline.py` + `night_runner.py → night_cycle.py` |
| `15m_cycle.sh` | `prj_cycle.py --queue` 호출 | `day_runner.py --queue → day_pipeline.py` |
| `devforge-15m-cycle.timer` | 15분마다 prj_cycle | 15분마다 day_runner (변경 없음) |
| `runner.py` (수동) | 실험 전용 | → `exp_runner.py`로 대체 |
| `day_runner.py` (수동) | — (신규) | 개발/디버깅용 단독 실행 |
| `night_runner.py` (수동) | — (신규) | 개발/디버깅용 단독 실행 |

---

## 8. DB Handoff JSON 스키마 (상세)

### 8.1 day_review body (INSERT)

```json
{
    "schema_version": 1,
    "phase": 0,
    "flags": ["--rubric-off", "--feedback-off"],
    "extract": {
        "processed": 10,
        "failed": 2,
        "facts": 24,
        "models_used": ["extractor"]
    },
    "py_verify": {
        "issues_found": 1,
        "total_findings": 8,
        "issues": [{"file": "scripts/x.py", "type": "sql_injection", "severity": "high"}]
    },
    "day_verify": {
        "verdict": "approved_with_conditions",
        "confidence": 95,
        "verification_items": [
            {"check": "SQL injection in query", "result": "pass", "detail": "parameterized query"}
        ]
    },
    "day_review": {
        "findings": [
            {"id": "F001", "severity": "high", "category": "bug",
             "description": "Missing input validation",
             "file": "scripts/x.py",
             "line_range": "42-56"}
        ],
        "verdicts": [
            {"id": "F001", "verdict": "accept", "reason": "reproducible bug"}
        ],
        "scores": {
            "P": 85,
            "R": 78,
            "consensus": 82
        },
        "proposer_model": "night_proposer",
        "reflector_model": "night_reflector",
        "judge_model": "night_judge"
    },
    "pipeline_state_path": "data/experiment/pipeline_state_r1_norubric.json",
    "container_mode": "day",
    "pod_b_mode": "day"
}
```

### 8.2 night_review body (INSERT)

```json
{
    "schema_version": 1,
    "phase": 0,
    "flags": ["--structural"],
    "day_handoff_id": 42,
    "prj_results": {
        "P_score": 85,
        "R_score": 78,
        "consensus": 82,
        "decision": "approved_with_conditions",
        "approved": ["F001", "F003"],
        "rejected": ["F002"],
        "r_rejected_findings": [
            {"id": "F002", "severity": "medium", "rejection_reason": "false positive"}
        ]
    },
    "handoff": {
        "llm_r": {"verdict": "approved", "confidence": 85},
        "python": {"verdict": "approved_with_conditions", "confidence": 75}
    },
    "night_verify": {
        "verdict": "approved",
        "confidence": 90,
        "verification_items": [
            {"check": "...", "result": "pass", "detail": "..."}
        ],
        "feedback": {
            "P": {"score": 75, "strengths": ["Accurate"], "weaknesses": ["Brief"]},
            "R": {"score": 80, "strengths": ["Precise"], "weaknesses": []},
            "J": {"score": 85, "strengths": ["Fair"], "weaknesses": ["Overly strict"]}
        }
    },
    "elapsed_seconds": 1800,
    "handoff_preference": "llm_r",
    "container_mode": "night"
}
```

---

## 9. 위험 및 완화

| 위험 | 영향 | 완화 |
|------|------|------|
| day_pipeline DB 저장 실패 시 night 불가 | night_cycle 미실행 | pipeline_state.json fallback + night_runner가 재시도 |
| activity_log에 day_review 타입 추가로 기존 쿼리 영향 | 없음 | 새 type='day_review'은 기존 쿼리와 독립적 |
| Pod B 스왑 책임이 night_runner로 이동하면서 기존 prj_cycle 내부 스왑과 충돌 | day_pipeline이 실수로 Pod B를 stop | day_pipeline.py에서 `stop_pod_b()` 호출 제거 |
| migration 중 누락된 참조 (import, shell script) | part별 import 에러 | `grep -r "prj_cycle\|from scripts"`로 전수조사 후 삭제 |
| day_review에 7B-3B-7B로 처리한 결과의 quality가 30B보다 낮음 | false positive 증가 | day_review confidence < 70이면 night_review에서 집중 검증 |
| runner 분할로 인한 기능 누락 (예: Slack 리포팅) | 일부 알림 누락 | 각 runner가 metrics.py를 import하므로 공유됨. 단위 테스트 확인 |
| lib/runner/ 디렉토리 분할로 sys.path 오류 | ImportError | Python path 구성 확인. PYTHONPATH에 scripts/ 추가 필요 |

---

## 10. 타임라인 (예상)

| 단계 | 작업 | 예상 시간 | 의존성 |
|------|------|----------|--------|
| A | container_manager + snapshot + metrics 추출 | 30분 | 5-phase 실험 완료 |
| B | pod_manager + token_budget 추출 (code-size) | 30분 | Phase A |
| C | day_pipeline + day_runner 생성 | 1시간 | Phase A + B |
| D | night_cycle + night_runner 생성 | 1.5시간 | Phase A + B |
| E | exp_runner 생성 | 30분 | Phase C + D |
| F | prj_cycle + runner 제거 | 30분 | Phase E |
| G | 테스트 구조 정리 | 30분 | Phase F |
| | **전체** | **~5시간** | |

---

## 11. 성공 기준

### Pipeline
- [ ] `day_runner.py` 단독 실행: Pod A+B 시작 → day_pipeline → DB 저장 → Pod A 종료
- [ ] `night_runner.py` 단독 실행: DB handoff 로드 → Pod B 스왑(30B→14B→27B) → night_cycle → DB 저장 → Pod B day 복원
- [ ] `exp_runner.py --phases [0,1,2,3,4]`: phase loop 성공, 각 phase day→night 순차 실행

### DB Handoff
- [ ] `SELECT * FROM activity_log WHERE type='day_review'` → 정상 레코드
- [ ] `SELECT * FROM activity_log WHERE type='night_review'` → 정상 레코드
- [ ] night_cycle이 day_review 레코드를 DB에서 읽어 P-R-J 정상 수행

### Cleanup
- [ ] `grep -r "prj_cycle" scripts/` → 빈 결과 (또는 의도된 참조만)
- [ ] `grep -r "from scripts" day_runner.py night_runner.py exp_runner.py` → lib/만 import
- [ ] 기존 5-phase 실험과 결과 비교: P score, R score, consensus 차이 ±10% 이내

### Test
- [ ] `scripts/lib/test_sandbox.py` → `tests/lib/test_sandbox.py` 이동 완료
- [ ] `pytest tests/ -x --tb=short` → 전체 통과
- [ ] `tests/` 디렉토리 구조가 소스 구조(day/night/runner/lib)와 1:1 매칭

---

*Generated: 2026-06-08 | Status: DRAFT | 다음: 사용자 검토 → 확정 → 구현*
