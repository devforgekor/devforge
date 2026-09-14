# W1 Baseline — D1 Summary (2026-09-14)

> **Gate 1**: ✅ PASS (mcp-contract.json allowlist diff=0 confirmed)
> **D1 일일 측정 로그**: `docs/ops/baseline/2026-09-14.json`

---

## 측정 결과

| 지표 | 측정값 | 기대/임계 | 판정 | 비고 |
|---|---|---|---|---|
| **Gate 1: 계약 검증** | allowlist diff=0 | diff=0 | ✅ PASS | `specs/mcp-contract.json` 12툴 ↔ opencode.json 12툴 정확히 일치 |
| **turns 삽입/시간** | 146건/24h | 일별 패턴 기록 | 기록 | 총 7160건, 2026-05-19~09-14 |
| **turns.source** | unknown: 7160/7160 | 100% unknown | ⚠️ 증거 불가 | provenance 0%. Gate 6 위협 |
| **MCP 호출 (24h)** | observations: 1건 | 기준 미정 | 기록 | headless 세션에서 auto_log 미발동 |
| **day_cycle 타이머** | inactive | 운영 중 예상 | ⚠️ 확인 필요 | 일별 패턴 측정 불가. 수동 실측 필요 |
| **netdata** | active | active | ✅ PASS | Observability 기본 infra 확인 |
| **훅 오버헤드** | avg 0.0004ms | baseline 대비 +20% 이내 | mock 측정 | live 측정은 실제 사용 시에만 가능 |
| **MCP 서버** | /health 200 OK | 정상 | ✅ PASS | FastMCP Streamable HTTP |

---

## 발견된 이슈

### 1. turns.source 100% unknown (Gate 6 우려)
- 7160건 모두 `unknown` source. provenance 백필 없음.
- **영향**: Gate 6 "provenance 동작" 통과 불가능 (현재 상태)
- **대응**: W2 D1-2까지 백필 스크립트 + 수동 검증 계획 수립

### 2. 일일 turn 유입 패턴 (14일)
| 날짜 | 건수 | 비고 |
|---|---|---|
| 09-14 (오늘) | 79 | 진행 중 |
| 09-13 | 134 | |
| 09-12 | 179 | 주중 최대 |
| 09-11 | 138 | |
| 09-10 | 47 | 주말 감소 |
| 09-07 | 11 | 주말 최저 |
| 평균 | ~82/일 | 주말 30-50%, 주중 100-180% |

### 3. 파이프라인 상태 (DB 실측)
- pending: 1615 (처리 대기)
- verified: 2276
- embedded: 3192
- embed_skipped: 77
- **turns.watcher 운영 중**, day_cycle 수동 실행 확인 필요

### 2. day_cycle 서비스 상태
- devforge-day-cycle.timer: inactive, devforge-day-cycle.service: activating
- worker 내부 동작 확인 필요 (일별 패턴 측정 불가)

### 3. observations 24h: 1건 (auto_log 활용도 극히 낮음)
- headless 세션에서 PostToolUse 훅이 거의 발동되지 않음.
- **영향**: 훅 오버헤드 측정은 실제 대화형 세션에서만 의미 있음.
- **대응**: W1 기간 중 대화형 세션 1회 이상 확보하여 live 측정 필요.

### 4. provenance 코드 레벨 확인
- **turns.source는 모든 삽입 경로에서 미설정** (4/4 경로 누락):
  - turn_watcher.py:245, mcp_server.py:173·530, mcp_server_sse.py:222
  - 스키마 DEFAULT `'unknown'` → 7160/7160 = 100% unknown
- **Gate 6 통과 불가** (현재 상태). W2 D1-2까지:
  1. 기존 turns: `legacy:pre-2026-09` 마커 + 수락기준 재정의
  2. 향후 turns: 모든 INSERT에 `source` 컬럼 추가 필요
- 상세: `docs/ops/provenance-analysis.md`

---

## 다음 D2 체크리스트

- [ ] day_cycle 서비스 동작 확인 (worker 내부 동작)
- [ ] 대화형 세션에서 hook overhead live 측정
- [ ] turns.source INSERT 경로 수정 (4개 파일)
- [ ] observations 24h count 재측정

## D1 측정 요약

| Gate | 상태 | 비고 |
|---|---|---|
| Gate 1: 계약 승인 | ✅ PASS | allowlist diff=0, 12툴 정확히 일치 |
| Gate 2: baseline | 🔄 D1 기록 중 | 일일 측정 D2-D7 진행 |
| Gate 3: 경보 발동 | ⏸ | baseline 기반 임계값 확정 후 |
| Gate 4: 롤백 리허설 | ⏸ | Gate 3 이후 |
| Gate 5: 수치·용어 정합 | ⏸ | turns.source 정정 포함 |
| Gate 6: provenance | ❌ 실패 | 7160/7160 unknown (W2 목표) |
