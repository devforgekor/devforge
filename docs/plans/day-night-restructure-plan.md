# Plan: Day/Night Pipeline Restructure

## Final Architecture (Implemented 2026-06-10)

```
turn_watcher.py (systemd, 상시)
  → turns DB (SSOT)

:00 ── devforge-day-extract.timer ── day_extract.sh
  fast: code-struct + duckdns + worklog (~10s)
  heavy: day_extract.py (25min budget)
    [Pod A 3B] extract.py --limit 50     → review_facts (user/thinking/text)
    [Pod A 3B] mcp_enrich.py --limit 20  → review_facts (mcp_meta)
    checkpoint advance (internal to each module)
    :25 check: remaining < 180s → defer

:30 ── devforge-day-verify.timer ── day_verify.sh  
  fast: code-struct + duckdns + worklog (~10s)
  heavy: day_verify.py (25min budget)
    [Pod B 14B] DB에서 extraction facts + MCP 로드
    [Pod B 14B] chunked LLM verify (reviewer system prompt)
    → review_facts (fact_type='verify_result')
    → eval/ pipeline_verify_*.json
    :55 check: remaining < 180s → defer

KST 01:00 ── devforge-night-cycle.timer (MODE=night)
  night_review.py (= night.py --review)
    [Pod B 30B] Proposer → [Pod B 14B] Refuter → [Pod B 14B] Judge
    eval/에서 pipeline_verify_*.json 로드 (= day_verify output)
  night_verify.py (= night.py --verify)
    [Pod B 27B] Verify → Phase 6 Feedback → Phase 7 Restore Day
    MODE=day 복원 → day timer 재개
```

## Timer Configuration

| Timer | OnCalendar | Script | Pod | Notes |
|-------|-----------|--------|-----|-------|
| `devforge-day-extract.timer` | `*:00` AccuracySec=1s | `day_extract.sh` | A (3B) | extract + MCP |
| `devforge-day-verify.timer` | `*:30` AccuracySec=1s | `day_verify.sh` | B (14B) | verify + category |
| `devforge-night-cycle.timer` | UTC 16:00 (KST 01:00) | `night_review.sh` → `night_verify.sh` | B (30B→14B→27B) | MODE=night |

Old timers `devforge-15m-cycle.timer` and `devforge-classify.timer` are **disabled**.

## Implemented Files

| File | Status | Lines | Role |
|------|--------|-------|------|
| `day_extract.py` | NEW | ~80 | extract + MCP chain, Pod A only |
| `day_verify.py` | NEW | ~250 | DB-based verify (replaces night.py Phase 2) |
| `day_extract.sh` | NEW | ~70 | shell wrapper (fast + heavy) |
| `day_verify.sh` | NEW | ~70 | shell wrapper (fast + heavy) |
| `devforge-day-extract.{timer,service}` | NEW | - | systemd :00 |
| `devforge-day-verify.{timer,service}` | NEW | - | systemd :30 |
| `night_review.py` | NEW | ~15 | alias → night.py --review |
| `night_verify.py` | NEW | ~15 | alias → night.py --verify |
| `night.py` | MODIFIED | - | Phase 1-2 deprecate, Phase 3 removed, v4.0 CLI |
| `day_cycle.py` | UNCHANGED | - | no longer called, kept for rollback |
| `15m_cycle.sh` | DISABLED | - | replaced by day_extract.sh + day_verify.sh |
  Phase 4: Night P-R-J (30B P → 14B R → 14B J, eval/에서 Phase 2 결과 읽음)
  Phase 5: 27B Verify
  Phase 6: Feedback
  Phase 7: Restore Day
```

### Current Problems
1. `day_cycle.py` → `night.py --phases 1 2`가 extract/MCP 결과를 읽지 않고 **stale eval_*.json 파일**을 읽음
2. Phase 1-2-3-4가 실제 데이터 흐름과 불일치 (eval/ 파일만 읽고 DB 무시)
3. Pod A (3B) + Pod B (7B)가 동시에 실행되어 4코어 ARM에서 경합 발생

---

## Proposed Design

### Timer: `*:0/30` — `day_extract` (Pod A only)

```
:00 ── day_extract ─────────────────────────────────── :28
  [Pod A] extract.py --limit 50     → review_facts (user/thinking/text)
  [Pod A] mcp_enrich.py --limit 50  → review_facts (mcp_meta)
  checkpoint advance
  :25 검사: 남은 turn ≤ 30개? → 추가 1회 / 아니면 defer
  :28 종료 (2분 버퍼)
```

- Pod B: 건드리지 않음 (8080/8081 off or idle)
- DB: checkpoint(extract) + checkpoint(mcp_enrich) 유지
- eval/: 저장 없음 (DB = SSOT)

### Timer: `*:30/30` — `day_verify` (Pod B only)

```
:30 ── day_verify ─────────────────────────────────── :58
  [Pod B] review_facts에서 extraction facts 로드 (DB 직접)
  [Pod B] 14B LLM verify + category
    → 각 extraction → verification_item {
        check, result, detail,
        category: bug|security|performance|quality|data_loss
      }
    → category_summary
  [Pod B] DB 저장: review_facts (fact_type='verify_result')
  [Pod B] eval/ 저장: pipeline_verify_*.json
  :55 검사: 남은 작업 3분以内? → 추가 / defer
  :58 종료 (2분 버퍼)
```

- Pod A: 건드리지 않음 (8082 off or idle)
- **night.py Phase 1-2 로직 재사용**: `_build_findings_context()` + `VERIFIER_SYSTEM_PROMPT` 그대로 사용
- 단, `load_eval_data()` 대신 DB에서 directly load

### Night (별도 타이머, MODE=night)

```
night_review:
  eval/에서 pipeline_verify_*.json 로드 (day_verify 결과)
  30B Proposer (--group-category) → 14B Refuter → 14B Judge
  → eval/ 저장

night_verify:
  27B Verify + Feedback + Restore Day
  → eval/ 저장
```

### Timer Changes

| Current Timer | Change | Target |
|---|---|---|
| `devforge-15m-cycle` `*:0/30` | **Keep** → `day_extract.sh` (fast + extract + MCP) | `*:0/30` |
| `devforge-classify` `*:15/30` | **Rename** → `day_verify` / `devforge-day-verify.timer` | `*:30/30` |
| `devforge-nightly` | **Split** → `night_review` + `night_verify` | TBD |

### File Changes

| File | Action | Reason |
|---|---|---|
| `day_cycle.py` | **Delete** | 더 이상 사용 안 함 |
| `day_extract.py` | **Create** | extract + MCP enrich, Pod A only |
| `day_verify.py` | **Create** | 14B verify + category, Pod B only, reuse night.py Phase 2 |
| `15m_cycle.sh` | **Modify** | fast tasks만 유지 → `day_extract.py` 호출 |
| `night.py` | **Refactor** | Phase 1-2-3 제거, Phase 4→night_review, Phase 5-6-7→night_verify |
| `night_review.py` | **Create** | P-R-J only (from night.py Phase 4) |
| `night_verify.py` | **Create** | 27B verify + feedback + restore (from night.py Phase 5-6-7) |
| `review_facts` | **Add column** | `source_file` (이미 추가 완료) |

### DB ↔ File Dual Path (key items)

| Data | DB (SSOT) | eval/ (handover) |
|---|---|---|
| extraction facts | `review_facts` fact_type=user/thinking/text | X |
| mcp_meta | `review_facts` fact_type=mcp_meta | X |
| verify_result | `review_facts` fact_type=verify_result | `pipeline_verify_*.json` |
| night proposals | X | `pipeline_night_proposals_*.json` |
| night verdicts | X | eval/ files |

### Buffer Logic (`:25` / `:55` check)

```python
# extract or verify 완료 후
미처리 = count(*) FROM turns WHERE created_at > checkpoint
          AND NOT EXISTS (SELECT 1 FROM review_facts WHERE ...)
예상_시간 = 미처리 * 초당_처리량(3B=~6s, 14B=~15s)
if 예상_시간 > 180:  # 3분 초과
    defer → checkpoint 유지 → 다음 사이클이 이어받음
else:
    추가 1회 실행 → checkpoint advance
```
