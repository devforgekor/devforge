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

---

## 2026-07-15: Merge logic 제거 + `_expand_spec_parens` 유지 (commit e9861bc)

**변경**:
- `_split_atomic` paragraph merge logic 제거 (63dd64b에서 추가됐던 코드)
- `_expand_spec_parens` `\n\n` → `\n` 유지

**결과**:
| 항목 | 값 |
|------|-----|
| English recall | **8/14** |
| 총 fact 수 | 39 |
| 테스트 시간 | 5156s (85분) |
| Grounded fact | 27 |
| 이전 대비 | 4/14 → 8/14 (merge 제거 효과), 10/14 대비 -2 (Host expansion에서 5개 spec 중 1개만 추출) |

**MISS (6)**: 22Gi RAM, zram, /opt/ai_data, /mnt/lv_db, /opt/projects, data-pod  
**시간 개선**: 5606s → 5156s (청크 수 감소로 인한 LLM 호출 절약)

---

## 2026-07-15: 400tok/16fact/\n\n 유지 — full config (current HEAD)

**변경**:
- `_split_atomic max_chars`: 600 → **1600** (~400 tok × 4 chars/tok)
- `_expand_spec_parens` `\n` → **`\n\n`** 복원 (c70e171 revert)
- System prompt: `"Up to 8 facts"` → **`"Up to 16 facts"`**

**결과**:
| 항목 | 값 |
|------|-----|
| English recall | **10/14** |
| 총 fact 수 | 46 |
| Grounded fact | 33 |
| Precision | 10/45 |
| 테스트 시간 | 5866s (97분) |
| Baseline 대비 | recall 동일 (10/14), 시간 +260s |

**MISS (4)**: /opt/ai_data (100G), /mnt/lv_db (30G), /opt/projects (10G), data-pod (postgres)  
**결론**: 1600자 청크 + 16 facts 제한으로도 baseline recall 초과 불가. 3개 storage path + data-pod는 청크 크기나 fact cap 문제가 아니라 LLM이 source text에서 아예 추출하지 않음. LLM이 file path를 subject로 인식하지 못하는 것이 근본 원인.
