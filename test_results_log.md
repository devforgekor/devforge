# Test Results Log — extract pipeline experiments

> 누적 기록. 세션 간 날아가지 않도록. 최신순.
>  
> **변경사항은 commit으로 고정됨. 이 로그는 실험 결과만 기록.**

---

## 2026-07-15: `_split_atomic` — 100자 이하 paragraph unconditional merge (commit 77360a0)

**변경**: `_split_atomic` paragraph merge 조건 변경.
- 기존: `len(merged[-1]) + len(para) + 1 <= max_chars` (600자 이하일 때만 병합)
- 변경: `len(para) <= 100 or len(merged[-1]) + len(para) + 1 <= max_chars` (100자 이하는 무조건 병합, 100자 초과는 600자 제한)

**목적**: tiny paragraph(서비스 row ~58자, storage item ~50자)는 무조건 이전 paragraph에 합쳐 청크 수 감소. 큰 paragraph는 기존대로 600자 제한 유지.

**미테스트**.

---

## 2026-07-15: `_expand_spec_parens` — spec 항목 간 blank line 제거 (commit c70e171)

**변경**: `_expand_spec_parens`에서 각 spec 항목 사이 `\n\n` → `\n`.
- 기존: 각 spec이 별도 paragraph로 분리 (e.g. `- DEVFORGE has ARM Neoverse-N1.` `- DEVFORGE has 4-core.` 각각 개별 문단)
- 변경: 같은 entity의 spec들은 같은 문단 내 라인으로 유지

**목적**: "Host: DEVFORGE (ARM...)" 같은 expansion에서 5-6개 spec이 각각 별도 청크가 되는 문제 해결. Sentence merge가 같은 문단 내에서만 동작하므로, blank line 제거로 문장 병합 효율 향상.

**미테스트**.

---

## 2026-07-15: Production code 확인 — 변경 없음

**결론**: 이전 대화에서 정리된 변경사항들은 실제 파일에 적용된 적 없음.
- Phase 2c-2b `_fix_status_hallucination` after refine: **이미 production에 있음** (extract.py:683-697)
- `failed` regex 포함: **이미 production에 있음** (extract_llm.py:910)
- `_split_atomic` paragraph merge (600자): **이미 production에 있음** (extract_llm.py:307-319)

실제로 누락된 건 `_expand_spec_parens`의 `\n\n` → `\n` 뿐. 위의 commit c70e171에서 수정함.
