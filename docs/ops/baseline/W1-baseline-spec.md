# W1 Baseline Measurement — Phase 0 Gate 2

> **Status:** in_progress · **Date:** 2026-09-14 · **Owner:** devforge
> **선행:** `specs/mcp-contract.json` (gate 1) · `docs/adr/0006-mcp-tool-surface.md` (Accepted)
> **목적:** W1(1주) 동안 공유 baseline을 측정하여 게이트 2·3·6 통과 여부와 임계값/허용오차/RTO/RPO 확정의 근거를 수집한다.

---

## 1. 측정 항목 (v5 §2.9 단일 baseline)

| # | 지표 | 측정 방법 | 기대값/임계 | 측정 빈도 |
|---|---|---|---|---|
| 1 | `turns` 삽입/시간 | DB `turns` 테이블 count + max(created_at) - min(created_at) | 일별 패턴 기록 | 매일 D1-7 |
| 2 | MCP 툴 p95 지연 | MCP 서버 로그 또는 호출 타임 로그 | baseline×1.3 = 경보 임계 (절대값 금지) | 매일 D1-7 |
| 3 | day_cycle 단계 소요 | 각 단계(scan/extract/verify/enrich/embed) 시작-종료 시각 | 단계별 기록 | 매일 D1-7 |
| 4 | 훅 오버헤드(auto_log 호출당 ms) | `auto_log.py` 진입/종료 타임차 | baseline 대비 +20% 이내 (G8) | 매일 D1-7 |
| 5 | 백업 복원 속도 | pg_dump + restore 테스트(실데이터) | RTO/RPO 확정 근거 (W2 D1-2) | D3-5 |

---

## 2. 측정 스크립트

### 2.1 baseline-collect.sh

```bash
#!/usr/bin/env bash
# W1 Baseline Collector — runs daily during W1 (D1-D7)
# Output: docs/ops/baseline/YYYY-MM-DD.json

set -euo pipefail

OUTDIR="/opt/projects/server/docs/ops/baseline"
mkdir -p "$OUTDIR"
DATE=$(date +%Y-%m-%d)
OUTFILE="$OUTDIR/$DATE.json"

DB="podman exec -i postgres psql -U postgres -d devforge_app -t -A"

# 1. turns rate
TURNS_COUNT=$($DB -c "SELECT count(*) FROM turns WHERE created_at >= NOW() - INTERVAL '24 hours'")
TURNS_AGE=$($DB -c "SELECT EXTRACT(EPOCH FROM (NOW() - MIN(created_at))) FROM turns WHERE created_at >= NOW() - INTERVAL '24 hours'")

# 2. MCP tool calls (from MCP server logs or DB)
MCP_CALLS=$($DB -c "SELECT count(*) FROM observations WHERE created_at >= NOW() - INTERVAL '24 hours'" 2>/dev/null || echo "n/a")

# 3. day_cycle stage timing (from watchdog logs)
DAY_CYCLE_LOG="/home/opc/.local/state/devforge/day-cycle-$(date +%Y%m%d).log"
if [ -f "$DAY_CYCLE_LOG" ]; then
  DAY_CYCLE_STAGES=$(grep -oP '(scan|extract|verify|enrich|embed)\s+\K[0-9.]+s?' "$DAY_CYCLE_LOG" || echo "not_found")
else
  DAY_CYCLE_STAGES="log_not_found"
fi

# 4. hook overhead (auto_log call timing)
HOOK_OVERHEAD="measure_separately"

# 5. backup restore test (once during W1)
RESTORE_TIME="pending"

cat > "$OUTFILE" << EOJSON
{
  "date": "$DATE",
  "turns": {"count_24h": "$TURNS_COUNT", "age_seconds": "$TURNS_AGE"},
  "mcp_calls_24h": "$MCP_CALLS",
  "day_cycle_stages": "$DAY_CYCLE_STAGES",
  "hook_overhead": "$HOOK_OVERHEAD",
  "backup_restore": "$RESTORE_TIME"
}
EOJSON

echo "Baseline collected: $OUTFILE"
```

### 2.2 hook-overhead-measure.py

```python
"""Measure auto_log.py call overhead — runs 100 iterations."""
import time
import json
import os
import sys
sys.path.insert(0, '/opt/projects/server/scripts/hooks')

def measure_hook_overhead(iterations: int = 100):
    import auto_log
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        auto_log.observe("test", "test", {})
        elapsed = (time.perf_counter() - start) * 1000  # ms
        times.append(elapsed)
    avg = sum(times) / len(times)
    p95 = sorted(times)[int(len(times) * 0.95)]
    return {"iterations": iterations, "avg_ms": round(avg, 3), "p95_ms": round(p95, 3), "min_ms": round(min(times), 3), "max_ms": round(max(times), 3)}

if __name__ == "__main__":
    result = measure_hook_overhead(100)
    print(json.dumps(result, indent=2))
    with open("/opt/projects/server/docs/ops/baseline/hook-overhead.json", "w") as f:
        json.dump(result, f, indent=2)
```

---

## 3. 측정 일정

| 날짜 | 단계 | 활동 | 산출물 |
|---|---|---|---|
| **W1 D1** | D1 | MCP contract 최종 확인 (gate 1 통과 확인) | gate-1-result.json |
| **W1 D1-2** | D1-2 | hook overhead 측정 시작, day_cycle 패턴 초기 관찰 | baseline D1, D2 |
| **W1 D3-7** | D3-7 | **공유 baseline 측정**: turns율·MCP p95·훅 오버헤드·day_cycle 단계 | baseline D3-D7 |
| **W1 D3-5** | D3-5 | 백업 복원 속도 테스트 | backup-restore-result.json |
| **W1 D7** | D7 | W1 종합 리포트 | docs/ops/baseline/W1-summary.md |
| **W2 D1-2** | D1-2 | **임계값·허용오차·RTO/RPO 확정** | gate-2-result.json |

---

## 4. 게이트 연동

| 게이트 | 통과 조건 | 근거 |
|---|---|---|
| Gate 1: 계약 승인 | `specs/mcp-contract.json` Schema 검증 + allowlist diff=0 + ADR-0006 Accepted | ✅ specs/mcp-contract.json 작성 완료, ✅ ADR-0006 Accepted 전환 |
| Gate 2: baseline 완료 | W1 측정 + 임계값·허용오차·RTO/RPO 확정 | 🔄 W1 측정 진행 중 |
| Gate 3: 경보 발동 | baseline 기반 임계값에서 1건 발동 | gate 2 이후 |
| Gate 4: 롤백 리허설 | 정규화 diff(허용오차 내) + health 10분 | gate 3 이후 |
| Gate 5: 수치·용어 정합 | 32/12/25/≈15/9/−8/448/7152 전 문서 | 병행 |
| Gate 6: provenance 동작 | 신규 100% + `legacy:pre-2026-09` no-op | gate 4 이후 |

---

## 5. 현재 상태 (2026-09-14)

- [x] Gate 1: `specs/mcp-contract.json` 초안 작성 완료 (v5 §2.2 스키마, v3 §3.1 12툴)
- [x] Gate 1: ADR-0006 Accepted 전환 완료 (12툴 목록 반영)
- [ ] Gate 2: W1 baseline 측정 (D1 시작, 1주 소요)
- [ ] Gate 2: hook 오버헤드 측정 (auto_log.py 호출당 ms) — 실측 필요
- [ ] Gate 2: 백업 복원 속도 테스트 — 실측 필요
- [ ] Gate 2: RTO/RPO 확정 (W2 D1-2, 사용자 소유)

---

*End of W1 baseline spec. Measurement runs D1-D7, then W2 D1-2 for threshold finalization.*
