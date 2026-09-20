# Phase 3 계획 — 파이프라인 도메인화(embed) + 오케스트레이터

> **Status:** approved (D6=A) · **Date:** 2026-09-20 · **Owner:** devforge
> **정본 참조:** `docs/plans/final-plan.md` §5 Phase C · `docs/adr/0006-mcp-tool-surface.md`(contract frozen) · `docs/ops/baseline/`
> **경계 결정:** D6 = **A안** — 신규 로직(외부)이 pre-embed 전체를 소유, devforge 파이프라인은 **embed** 담당.

---

## 0. 범위 (D6=A)

```
신규 로직(외부) 소유                          devforge 파이프라인 소유
pending → text_clean → entity_scan            enriched → embed → embedded
        → extract → enrich → enriched          (+ 경량 유지보수: FTS5 refresh)
```

- **devforge가 소유하는 처리 단계 = `embed` 하나** (`enriched → embedded`, 실패 시 `embed_skipped`).
- `text_clean`은 **pre-extract 위치 유지** — 임베딩 앞으로 이동하지 않는다.
  - 근거: `text_clean`은 `text_clean` 컬럼을 생성하고 **extract와 embed 둘 다** 소비한다
    (`embed_batch`는 `COALESCE(t.text_clean, t.text_clean_polished, t.text)` 사용).
- **전제:** embed 시점에 clean 컬럼이 채워져 있어야 한다(신규 로직이 기록). 미기록 시 raw `text`로 임베딩됨 → 품질 저하.

---

## 1. 목표
- bash `day_cycle.sh`의 **임베딩 단계**를 devforge `pipeline_stages/embed` + `PipelineOrchestrator`로 이관.
- 비파괴: 프로덕션 DB에 직접 쓰지 않고 **shadow(`devforge_shadow`) → diff=0 검증** 후 컷오버.

## 2. 산출물
| 산출물 | 내용 |
|--------|------|
| `src/devforge/application/orchestrator.py` | `PipelineOrchestrator` + `BudgetManager` |
| `src/devforge/pipeline_stages/embed/` | embed stage 구현(enriched→embedded) |
| `src/devforge/application/day_cycle.py` | `run_full_cycle()` = 소유 스테이지 오케스트레이션 |
| `src/devforge/adapters/driving/mcp/tools/pipeline/` | `pipeline_status`, `pipeline_orchestrate` |
| `scripts/day_cycle.sh` | → `devforge pipeline orchestrate` 래퍼(≤10줄) |
| 검증 | `scripts/shadow_diff.py`(기존), `docs/ops/baseline/` |

## 3. 인터페이스 / 상태
- `Stage.run(ctx)`: input `enriched` → output `embedded`(성공) / `embed_skipped`(짧은 텀 등).
- **idempotent**: 재실행 안전(이미 embedded는 skip).
- 실패 시 상태 불변 + 로그/알림. clean 컬럼 부재 시 정책 결정 필요(권장: skip + 카운트, raw fallback은 금지).

## 4. 마일스톤
| 주차 | 작업 | 검증 |
|------|------|------|
| **W6** | orchestrator 골격 + embed stage(shadow write 대상) | 단위: 상태 전이/멱등성 |
| **W7** | MCP `pipeline_*` 툴 + `day_cycle.py` + 래퍼 | 툴 스키마, 래퍼 10줄 |
| **W8–9** | shadow 병렬 실행(≥14 사이클) | `shadow_diff.py` **diff=0**, 롤백 5분 |

## 5. 수락 기준 (Phase C)
- shadow 대조 **diff=0** + 2주 병렬, 롤백 ≤5분.

## 6. 의존성 / 전제
- 신규 로직이 `enriched` 생성 + `text_clean*` 컬럼 기록.
- 계약 **frozen**(ADR-0006), tool surface 보존.
- **ADR-0005(추출 라우팅)** 는 신규 로직(외부) 책임 — devforge Phase 3 범위 아님(방향 승인 D1은 별건).

## 7. 리스크
| 리스크 | 완화 |
|--------|------|
| embed 입력(clean 컬럼) 미보장 | 신규 로직이 clean 컬럼 기록 확인 + 부재 시 skip/경고 |
| day_cycle 로직 누락 | `data/day_cycle_behavior.md` + shadow 2주 병렬 |
| 통계 표본(n≥14) | W8–9 2주 확보 |
| 리소스(embed 8B vs day 8B 상호배타) | 시간대 분리(embed 윈도우), `--parallel`/ctx 튜닝 |
