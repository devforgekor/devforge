# Plan: Day/Night Pipeline Restructure

## Final Architecture (Implemented 2026-06-10)

```
turn_watcher.py (systemd, 상시) → watchdog triggers day_cycle.sh
  → turns DB (SSOT)
  → day_cycle.sh: system sync → embed → extract → enrich → verify
```

## Implemented Files

| File | Status | Lines | Role |
|------|--------|-------|------|
| `day_extract.py` | NEW | ~80 | extract + MCP chain, Pod A only |
| `day_verify.py` | NEW | ~250 | DB-based verify (replaces night.py Phase 2) |
| `day_cycle.sh` | MODIFIED | - | async pipeline (watchdog-triggered) |
| `night_review.py` | NEW | ~15 | alias → night.py --review |
| `night_verify.py` | NEW | ~15 | alias → night.py --verify |
| `night.py` | MODIFIED | - | Phase 1-2 deprecate, Phase 3 removed, v4.0 CLI |
| `day_cycle.py` | UNCHANGED | - | no longer called, kept for rollback |
| `15m_cycle.sh` | DISABLED | - | replaced by day_cycle.sh |

### Current Problems
1. `day_cycle.py` → `night.py --phases 1 2`가 extract/MCP 결과를 읽지 않고 **stale eval_*.json 파일**을 읽음
2. Phase 1-2-3-4가 실제 데이터 흐름과 불일치 (eval/ 파일만 읽고 DB 무시)
3. Pod A (3B) + Pod B (7B)가 동시에 실행되어 4코어 ARM에서 경합 발생

---

## Proposed Design

### Current Architecture

```
turn_watcher.py → turns DB (SSOT)
  → watchdog triggers day_cycle.sh (pipeline_state-driven)
    → text_clean → embed → entity_scan → extract → enrich → verify
```

### File Changes
