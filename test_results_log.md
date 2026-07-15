# Test Results Log — extract pipeline experiments

> 누적 기록. 세션 간 날아가지 않도록. 최신순.

---

## 2026-07-15: `_split_atomic` mini-merge (< 100 chars only)

**변경**: `_split_atomic`에서 600자 이하 문단을 전부 병합하던 것 → 100자 미만 tiny paragraph만 병합. `##` heading은 병합 안함.

**예상**: 서비스 row 12개 (각 ~58자) → 7개+5개로 2개 청크로 줄음. Overview/Health Checks 등은 그대로. 총 청크수: ~36개 예상.

**미테스트**.

---

## 2026-07-15: `_split_atomic` paragraph merge (600 chars)

**변경**: `_split_atomic`에 문단 병합 로직 추가 — blank line으로 분리된 작은 paragraph들을 600자까지 병합.

**결과**:
| 항목 | 값 |
|------|-----|
| 청크 수 | 16 (46 → 16) |
| English recall | **4/14** |
| 총 fact 수 | 35 |
| 테스트 시간 | 3291s (55분) |
| Grounded fact | 19 |
| `status` → `status_is` | 정상 변환됨 |

**문제**: merge가 너무 공격적. 539자 overview 청크에 hardware spec들이 몰려 LLM이 system RAM(22Gi), OS(Oracle Linux 9.7), DB(PostgreSQL 16), Caddy auto-HTTPS 등 8개 key fact 추출 실패. 서비스 row 10개가 한 청크에 들어가 8개 fact 제한에 걸림.

**Embedding match**: GT#1(ARM), GT#10(SWAP)만 매칭 (기존 8개 → 2개)
**Substring match**: GT#12(devforge-pod-a), GT#13(devforge-swap) — 변함 없음

**교훈**: 청크 병합으로 청크 수를 줄이면 LLM이 한 청크에서 추출하는 fact 수가 제한(8개)되어 recall 급락. 서비스 테이블 row는 각각 독립 청크가 필요.

---

## 2026-07-15: Status hallucination fix — refine 후 실행 (Phase 2c-2b)

**변경**: `_fix_status_hallucination` 호출 위치를 `extract.py` Phase 2a(extract chunk 후) → Phase 2c-2b(refine+merge 후, store 전)로 이동. `failed`도 regex에 추가.

**결과**:
| 항목 | 값 |
|------|-----|
| English recall | 10/14 (동일) |
| Service status | 정상: inactive, activating, failed |

**상세**:
- 44개 fact 대상 실행, 10개 서비스 source_statuses 매칭
- refine 단계에서 Qwen3-8B가 hallucination한 `active` → 올바른 상태로 복원
- `_fix_status_hallucination`이 refine 후에 실행되어야 효과 있음 확인

**문제**: English recall 10/14 유지. 4개 miss는 format-invariant(paths, data-pod).

---

## 2026-07-15: Baseline (46 chunks, after English blanket fix)

**변경**: English 문서 `_split_dense_bullets` blank line fix 적용, `_fix_status_hallucination` refine 전 실행.

**결과**:
| 항목 | 값 |
|------|-----|
| 청크 수 | 46 (English) |
| English recall | 10/14 |
| Service status | **hallucination**: refine 후 Qwen3-8B가 모든 service를 `active`로 재작성 |
| 청크 속도 | ~120초/청크 (2병렬) |

**문제**: `_fix_status_hallucination`이 refine 전에 실행되어서 refine에서 hallucination이 다시 발생. `cache_prompt=False`로 인해 청크당 830토큰 시스템 프롬프트 재처리 → 46청크 × ~120초 = 5600초+.

---

## 2026-07-15: Baseline (46 chunks, before English blanket fix)

**변경**: `_split_dense_bullets`가 Korean/English 모두에 blank line 추가.

**결과**:
| 항목 | 값 |
|------|-----|
| 청크 수 | 46 (English) |
| English recall | 10/14 |
| Korean recall | 측정 안함 |
| Service status | **hallucination**: `active`로 잘못 추출 |
| `status_is` 문제 | source에서 `status` 대신 `status_is` pred 사용 |

**문제**: `_fix_status_hallucination` regex가 `failed` 미포함. Refine 단계가 모든 service를 `active`로 hallucination.
