# Extract + Enrich Quality Spot-Check

샘플: 10개 turn (최근 10일, enrich_meta 존재)

---

## [1] a3c7ac22 (2026-06-13 03:08:56.406+00)

### User Turn

엠비딩을 하지 않아도 7b가 하는 모든 것에 환각이 없을까? 환각 때문에 하는 거긴한데

### Response (text)

맞습니다. 그게 핵심이네요.  **7B 혼자 하면**: entity 생성도 7B, cosine 검증도 7B → 같은 모델이 만든 걸 같은 모델이 검증. 환각을 환각으로 못 잡을 확률이 높습니다.  **8B가 별도로 있는 이유**: 다른 모델, 다른 임베딩 공간, 다른 weight → 7B가 만든 entity가 진짜 turn과 관련 있는지 **교차 검증**하는 효과.  그러니까: - `mcp_cosine`의 가치는 단순 cosine similarity가 아니라 **모델 간 교차 검증(8B가 7B를 견제)** - 1사이클 지연은 이 구조에서 자연스러운 비용 - embedding_f16이 먼저 저장되어 있어야 8B가 참조할 기준이 생기는 거라 순서도 지금이 맞음  **개선 포인트는 이미 적용한 것들로 충분**합니다: - pending → deferred/failed 상태 구분 (무한 대기 방지) - `embed_failed` 태그 (오류 vs 무관련 구분) - max retry (리소스 낭비 차단)  지금 구조 그대로 유지하는 게 맞겠습니다.

### Extracted Facts

| # | type | evidence (first 120) |
|---|------|---------------------|
| 0 | text | 7B 혼자 하면: entity 생성도 7B, cosine 검증도 7B → 같은 모델이 만든 걸 같은 모델이 검증. 환각을 환각으로 못 잡을 확률이 높습니다. |
| 1 | text | 7B 혼자 하면: entity 생성도 7B, cosine 검증도 7B → 같은 모델이 만든 걸 같은 모델이 검증. 환각을 환각으로 못 잡을 확률이 높습니다. |
| 2 | text | 7B 혼자 하면: entity 생성도 7B, cosine 검증도 7B → 같은 모델이 만든 걸 같은 모델이 검증. 환각을 환각으로 못 잡을 확률이 높습니다. |
| 3 | text | 8B가 별도로 있는 이유: 다른 모델, 다른 임베딩 공간, 다른 weight → 7B가 만든 entity가 진짜 turn과 관련 있는지 교차 검증하는 효과. |
| 4 | text | 8B가 별도로 있는 이유: 다른 모델, 다른 임베딩 공간, 다른 weight → 7B가 만든 entity가 진짜 turn과 관련 있는지 교차 검증하는 효과. |
| 5 | text | 8B가 별도로 있는 이유: 다른 모델, 다른 임베딩 공간, 다른 weight → 7B가 만든 entity가 진짜 turn과 관련 있는지 교차 검증하는 효과. |
| 6 | text | mcp_cosine의 가치는 단순 cosine similarity가 아니라 모델 간 교차 검증(8B가 7B를 견제) |
| 7 | text | mcp_cosine의 가치는 단순 cosine similarity가 아니라 모델 간 교차 검증(8B가 7B를 견제) |
| 8 | text | mcp_cosine의 가치는 단순 cosine similarity가 아니라 모델 간 교차 검증(8B가 7B를 견제). |
| 9 | text | 1사이클 지연은 이 구조에서 자연스러운 비용 |
| 10 | text | 1사이클 지연은 이 구조에서 자연스러운 비용. |
| 11 | text | 1사이클 지연은 이 구조에서 자연스러운 비용 |
| 12 | text | embedding_f16이 먼저 저장되어 있어야 8B가 참조할 기준이 생기는 거라 순서도 지금이 맞음 |
| 13 | text | embedding_f16이 먼저 저장되어 있어야 8B가 참조할 기준이 생기는 거라 순서도 지금이 맞음 |
| 14 | text | embedding_f16이 먼저 저장되어 있어야 8B가 참조할 기준이 생기는 거라 순서도 지금이 맞음. |

### Enrich Metadata

- **tldr**: User questions if 7B alone can solve hallucination issues without embedding, and the conversation discusses the value of cross-validation with...
- **intent**: question
- **category**: explanation
- **entities**: 0 files, 0 funcs, 2 techs, 0 users
  - techs: ['7B', '8B']
- **tags**: ['7b', '8b', 'cosine', 'cross-validation', 'embedding']
- **faithfulness**: 7B=100 GROUNDED, 8B=100 GROUNDED, tldr=0 UNGROUNDED


---

## [2] f76ba745 (2026-06-13 03:07:36.173+00)

### User Turn

단 1 사이클 지연만 넘기면 되는거 아니야? 1시간 차이는 무시할 수 있어. 개인 서버니까

### Response (text)

맞습니다. 1시간 지연은 전혀 문제 없습니다.  그런데 하나 생각난 건, **day_cycle.sh 실행 순서만 바꾸면 지연마저 없앨 수 있습니다**.  현재: ``` 8B 로드 → embed_batch → mcp_cosine (이전 사이클 pending 처리) 7B 로드 → extract → mcp_enrich (새 pending 생성) ```  `mcp_cosine`이 `mcp_enrich`보다 먼저 실행되니까, 방금 만든 pending을 못 보고 이전 사이클 것만 처리합니다.  순서만 바꾸면: ``` 7B 로드 → extract → mcp_enrich (새 pending 생성) 8B 로드 → embed_batch → mcp_cosine (방금 만든 pending 즉시 처리, embedding_f16도 방금 저장됨) ```  이렇게 하면 **같은 사이클 안에서**: 1. `mcp_enrich`가 `cosine_status=pending` 생성 2. `embed_batch`가 turn에 `embedding_f16` 저장 3. `mcp_cosine`이 둘 다 읽어서 cosine 검증 → `done`  Python 코드는 그대로, `day_cycle.sh`에서

### Extracted Facts

| # | type | evidence (first 120) |
|---|------|---------------------|
| 0 | text | 1시간 지연은 전혀 문제 없습니다. |
| 1 | text | day_cycle.sh 실행 순서만 바꾸면 지연마저 없앨 수 있습니다 |
| 2 | text | 현재: 8B 로드 → embed_batch → mcp_cosine (이전 사이클 pending 처리) 7B 로드 → extract → mcp_enrich (새 pending 생성) |
| 3 | text | 순서만 바꾸면: 7B 로드 → extract → mcp_enrich (새 pending 생성) 8B 로드 → embed_batch → mcp_cosine (방금 만든 pending 즉시 처리, embedding_f1 |
| 4 | text | Python 코드는 그대로, day_cycle.sh에서 embed/extract phase 순서만 바꾸면 됩니다. 모델 스왑 횟수도 동일합니다. |

### Enrich Metadata

- **tldr**: User confirms 1-hour delay is acceptable and suggests reordering day_cycle.sh to eliminate delay.
- **intent**: request
- **category**: explanation
- **entities**: 1 files, 0 funcs, 1 techs, 0 users
  - files: ['day_cycle.sh']
  - techs: ['Python']
- **tags**: ['reordering', 'day_cycle.sh', 'performance']
- **faithfulness**: day_cycle.sh=100 GROUNDED, Python=100 GROUNDED, tldr=0 UNGROUNDED
- ⚠️ **missing files**: ['day_cycle.sh']


---

## [3] 73256057 (2026-06-13 03:05:28.961+00)

### User Turn

최대한 모델을 변환하지 않고, 1차, 2차로 나누는 것에 대해서는 개선할 점 있어? 아니면 더 나은 방법을 제시.

### Response (text)

좋은 질문입니다. 근본적으로 따져보면:  ## 현재 구조의 비효율  ``` 8B 로드 → embed_batch (embedding_f16 저장)         → mcp_cosine (embedding_f16 읽어서 cosine)         → 8B 언로드  7B 로드 → extract → mcp_enrich (텍스트만, cosine_status=pending)         → 7B 언로드  14B 로드 → verify → 14B 언로드 ```  **문제**: cosine 검증이 1사이클 지연됨. 8B가 만든 벡터를 8B가 다시 읽어서 검증하는데, 그 사이에 모델을 내렸다 올려야 함.  ## 근본적인 질문: 8B 전용 임베딩 모델이 꼭 필요한가?  `mcp_cosine`이 하는 일은 결국: > entity/tldr 텍스트를 임베딩 → turn 임베딩과 cosine 비교  이건 **같은 임베딩 공간**이기만 하면 어떤 모델이든 가능합니다. 8B가 아니어도 됩니다.  ## 대안: 7B Q8 하나로 통합  ``` 7B 로드 → embed_batch (7B로 turn embedding)         → extract (fact 추출)         → mcp

### Extracted Facts

| # | type | evidence (first 120) |
|---|------|---------------------|
| 0 | text | cosine 검증이 1사이클 지연됨 |
| 1 | text | 8B 전용 임베딩 모델이 꼭 필요한가 |
| 2 | text | 7B Q8 하나로 통합 |
| 3 | text | turns.embedding이 7B 차원으로 저장됨 |

### Enrich Metadata

- **tldr**: User asks about improving 1-pass/2-pass split or suggesting a better method.
- **intent**: question
- **category**: explanation
- **entities**: 0 files, 0 funcs, 2 techs, 0 users
  - techs: ['Python', 'FastAPI']
- **tags**: ['model_optimization', 'embedding', 'cosine_similarity']
- **faithfulness**: Python=0 UNGROUNDED, FastAPI=0 UNGROUNDED, tldr=0 UNGROUNDED


---

## [4] 92f4e24c (2026-06-13 03:02:58.92+00)

### User Turn

진행

### Response (text)

수정 3개, `mcp_cosine.py`에만 적용합니다.

### Extracted Facts

| # | type | evidence (first 120) |
|---|------|---------------------|
| 0 | text | 수정 3개, mcp_cosine.py에만 적용합니다. |

### Enrich Metadata

- **tldr**: User requests changes to mcp_cosine.py to handle embedding failures and deferred states.
- **intent**: request
- **category**: requirement
- **entities**: 1 files, 4 funcs, 1 techs, 0 users
  - files: ['mcp_cosine.py']
  - funcs: ['_verify_entity_cosine', 'mcp_cosine_pipeline', '_get_pending_mcp', '_get_pending_by_turn']
  - techs: ['Python']
- **tags**: ['mcp_cosine.py', 'embedding', 'deferred', 'skip_count', 'max_retry']
- **faithfulness**: mcp_cosine.py=100 GROUNDED, Python=0 UNGROUNDED, _verify_entity_cosine=100 GROUNDED, mcp_cosine_pipeline=100 GROUNDED, _get_pending_mcp=100 GROUNDED, _get_pending_by_turn=100 GROUNDED, tldr=0 UNGROUNDED
- ⚠️ **missing files**: ['mcp_cosine.py']


---

## [5] 1e92b0db (2026-06-13 02:52:56.734+00)

### User Turn

이 구조에서 개선할 부분 말해줘. 이건 내 생각이라 비효율적일 수 있어.

### Response (text)

몇 가지 볼게 있습니다.  ### 1. `mcp_cosine`에 retry dead loop 위험  `embedding_f16`이 NULL인 turn에 pending mcp_meta가 있으면, `mcp_cosine`이 매 사이클마다 "failed += 1" 기록만 남기고 계속 재시도합니다. `embedding_f16`이 영원히 안 채워지는 상황(예: turn row 삭제됨, embed_batch 버그)이면 무한 루프.  **제안**: `cosine_status`를 삼분화해서 구분 - `"pending"` — mcp_enrich가 생성, cosine 대기 - `"skipped"` — `embedding_f16` 없음, 나중에 재시도 - `"done"` — cosine 완료  그리고 일정 횟수 이상 skipped면 `"failed"`로 마감.  ### 2. `mcp_cosine`가 실패한 entity를 구분하지 않음  현재는 entity embedding API 호출 실패 시 `{"cosine": 0.0, "relevant": False}`로 마킹. 근데 이게 "진짜 관련 없음"인지 "API 호출 실패"인지 구분이 안 됩니다.  **제안**: 실패한 entity

### Extracted Facts

| # | type | evidence (first 120) |
|---|------|---------------------|
| 0 | text | mcp_cosine에 retry dead loop 위험 |
| 1 | text | embedding_f16이 NULL인 turn에 pending mcp_meta가 있으면, mcp_cosine이 매 사이클마다 "failed += 1" 기록만 남기고 계속 재시도합니다. |
| 2 | text | cosine_status를 삼분화해서 구분 - "pending" — mcp_enrich가 생성, cosine 대기 - "skipped" — embedding_f16 없음, 나중에 재시도 - "done" — cosin |
| 3 | text | 일정 횟수 이상 skipped면 "failed"로 마감. |
| 4 | text | mcp_cosine가 실패한 entity를 구분하지 않음 |

### Enrich Metadata

- **tldr**: User requests improvements to the current architecture, focusing on retry mechanisms and status handling in mcp_cosine.
- **intent**: request
- **category**: requirement
- **entities**: 1 files, 1 funcs, 1 techs, 0 users
  - files: ['mcp_cosine.py']
  - funcs: ['_get_turns_without_mcp']
  - techs: ['Python']
- **tags**: ['architecture', 'improvement', 'retry', 'status']
- **faithfulness**: mcp_cosine.py=100 GROUNDED, Python=0 UNGROUNDED, _get_turns_without_mcp=0 UNGROUNDED, tldr=0 UNGROUNDED
- ⚠️ **missing files**: ['mcp_cosine.py']


---

## [6] 9475bd8b (2026-06-13 02:52:10.86+00)

### User Turn

mcp_enrich   (:8082, 7B) → MCP JSON 생성, cosine_status="pending" - 이렇게 하드 코딩 되어 있으면, 혹시 2차에 작성된 내용도 덮어씌여지지 않을까? 로직 확인

### Response (text)

덮어씌워지지 않습니다. `_get_turns_without_mcp()`의 쿼리를 보면:  ```sql AND NOT EXISTS (   SELECT 1 FROM review_facts rf2   WHERE rf2.turn_id = t.id AND rf2.fact_type = 'mcp_meta' ) ```  **한 번 `mcp_meta` row가 생성된 turn은 — `cosine_status`가 pending이든 done이든 — 다시 처리되지 않습니다.** 정상 파이프라인에서는 1차가 2차 결과물을 건드릴 일이 없습니다.  흐름: ``` Cycle N:   mcp_enrich → mcp_meta INSERT (cosine_status=pending) Cycle N+1: mcp_cosine → 같은 row UPDATE (cosine_status=done) Cycle N+2: mcp_enrich → NOT EXISTS 가드가 걸러서 skip ```  단, `--turn-id` 디버그 모드는 이 가드를 우회하므로 수동으로 같은 turn을 두 번 돌리면 덮어쓸 수 있습니다. 하지만 그건 의도된 디버그 동작이니 문제 없습니다.

### Extracted Facts

| # | type | evidence (first 120) |
|---|------|---------------------|
| 0 | text | 덮어씌워지지 않습니다. |
| 1 | text | AND NOT EXISTS (SELECT 1 FROM review_facts rf2 WHERE rf2.turn_id = t.id AND rf2.fact_type = 'mcp_meta') |
| 2 | text | 한 번 mcp_meta row가 생성된 turn은 — cosine_status가 pending이든 done이든 — 다시 처리되지 않습니다. |
| 3 | text | 정상 파이프라인에서는 1차가 2차 결과물을 건드릴 일이 없습니다. |
| 4 | text | 단, --turn-id 디버그 모드는 이 가드를 우회하므로 수동으로 같은 turn을 두 번 돌리면 덮어쓸 수 있습니다. |

### Enrich Metadata

- **tldr**: User confirms that mcp_enrich.py prevents overwriting of mcp_meta rows.
- **intent**: report
- **category**: explanation
- **entities**: 2 files, 2 funcs, 1 techs, 0 users
  - files: ['mcp_enrich.py', 'mcp_cosine.py']
  - funcs: ['_get_turns_without_mcp', '_insert_mcp_fact']
  - techs: ['Python']
- **tags**: ['race_condition', 'mcp_meta', 'pipeline']
- **faithfulness**: mcp_enrich.py=100 GROUNDED, mcp_cosine.py=100 GROUNDED, Python=100 GROUNDED, _get_turns_without_mcp=100 GROUNDED, _insert_mcp_fact=100 GROUNDED, tldr=0 UNGROUNDED
- ⚠️ **missing files**: ['mcp_enrich.py', 'mcp_cosine.py']


---

## [7] 29f2bd49 (2026-06-13 02:46:47.844+00)

### User Turn

아니 스크립트 구성을mcp_cosine.py- mcp_enrich.py 이런 체인으로 작성하면 consien이 없으면 skip. 있으면 consine 먼저 처리해서 이전 턴을 마무리하고, 새 턴을 작업. - 네 의견은?

### Response (text)

맞습니다. `mcp_cosine.py`를 embed phase의 마지막 단계로 넣으면:  ``` 8B session (:8081):   embed_batch.py  → 새 turn embedding_f16 저장   mcp_cosine.py   → 이전 사이클 pending 처리 (없으면 skip, 있으면 완료)  7B session (:8082):   extract.py → mcp_enrich.py → 새 MCP JSON + cosine_status="pending" 생성  14B session (:8083):   day_verify.py → 검토 ```  이 chain이 말씀하신 대로 **이전 턴 마무리 → 새 턴 작업 → 검토** 순서로 깔끔하게 흐릅니다. `mcp_cosine.py`는 pending 없으면 count 쿼리 한 번 던지고 바로 exit하니까 부담도 없고요.  구현 시작하겠습니다.

### Extracted Facts

| # | type | evidence (first 120) |
|---|------|---------------------|
| 0 | text | mcp_cosine.py를 embed phase의 마지막 단계로 넣으면: |
| 1 | text | 8B session (:8081): embed_batch.py → 새 turn embedding_f16 저장 mcp_cosine.py → 이전 사이클 pending 처리 (없으면 skip, 있으면 완료) |
| 2 | text | 7B session (:8082): extract.py → mcp_enrich.py → 새 MCP JSON + cosine_status="pending" 생성 |
| 3 | text | 14B session (:8083): day_verify.py → 검토 |
| 4 | text | 이 chain이 말씀하신 대로 이전 턴 마무리 → 새 턴 작업 → 검토 순서로 깔끔하게 흐릅니다. |

### Enrich Metadata

- **tldr**: User approves embedding chain with mcp_cosine.py and mcp_enrich.py in 8B and 7B sessions respectively.
- **intent**: other
- **category**: explanation
- **entities**: 5 files, 0 funcs, 1 techs, 0 users
  - files: ['mcp_cosine.py', 'mcp_enrich.py', 'embed_batch.py', 'extract.py', 'day_verify.py']
  - techs: ['Python']
- **tags**: ['embed_chain', 'mcp_cosine', 'mcp_enrich', '8b', '7b']
- **faithfulness**: mcp_cosine.py=100 GROUNDED, mcp_enrich.py=100 GROUNDED, embed_batch.py=100 GROUNDED, extract.py=100 GROUNDED, day_verify.py=100 GROUNDED, Python=0 UNGROUNDED, tldr=0 UNGROUNDED
- ⚠️ **missing files**: ['mcp_cosine.py', 'mcp_enrich.py', 'embed_batch.py', 'extract.py', 'day_verify.py']


---

## [8] 14df76fb (2026-06-13 02:45:26.556+00)

### User Turn

2차 다음에 1차로 잘못 썼다. 그래야 1턴을 마무리하고 다음턴이 계속 순차적으로 되지. 맞나?

### Response (text)

아, 정리하면:  **매 사이클, 8B 로드된 상태에서:**  ``` embed_batch  (1차) → 새 turn의 embedding_f16 생성 mcp_cosine   (2차) → 이전 사이클에서 pending된 mcp_meta를 embedding_f16으로 cosine 검증 ```  이렇게 1차→2차 체인으로 묶어서 8B 세션 한 번에 처리. 이후 7B로 전환해서 extract + mcp_enrich(새 pending 생성), 14B로 verify.  결국 모델 스왑은 기존과 동일하고, embed phase 안에 `embed_batch → mcp_cosine` 순서로 들어가는 구조네요.  `day_cycle.sh`에서 embed phase 내 순서만 `embed_batch.py` 다음 `mcp_cosine.py`로 잡으면 됩니다. 맞나요?

### Extracted Facts

| # | type | evidence (first 120) |
|---|------|---------------------|
| 0 | text | 매 사이클, 8B 로드된 상태에서: |
| 1 | text | embed_batch (1차) → 새 turn의 embedding_f16 생성 |
| 2 | text | mcp_cosine (2차) → 이전 사이클에서 pending된 mcp_meta를 embedding_f16으로 cosine 검증 |
| 3 | text | 이렇게 1차→2차 체인으로 묶어서 8B 세션 한 번에 처리. |
| 4 | text | day_cycle.sh에서 embed phase 내 순서만 embed_batch.py 다음 mcp_cosine.py로 잡으면 됩니다. |

### Enrich Metadata

- **tldr**: User correcting the order of tasks within an 8B session
- **intent**: clarification
- **category**: explanation
- **entities**: 3 files, 2 funcs, 1 techs, 0 users
  - files: ['day_cycle.sh', 'embed_batch.py', 'mcp_cosine.py']
  - funcs: ['embed_batch', 'mcp_cosine']
  - techs: ['Python']
- **tags**: ['pipeline', 'order', '8b', 'session']
- **faithfulness**: day_cycle.sh=100 GROUNDED, embed_batch.py=100 GROUNDED, mcp_cosine.py=100 GROUNDED, Python=0 UNGROUNDED, embed_batch=100 GROUNDED, mcp_cosine=100 GROUNDED, tldr=0 UNGROUNDED
- ⚠️ **missing files**: ['day_cycle.sh', 'embed_batch.py', 'mcp_cosine.py']


---

## [9] 0a1a0e6b (2026-06-13 02:44:51.668+00)

### User Turn

맞아. 우리는 체인구조로 하기로 했으니 1차 다음에 2차를 넣는 구조로 해. 그래야. 이번 턴에 다 못해도 계속해서 순차적으로 처리할 수 있어. - 네 생각은?

### Response (text)

좋은 설계입니다. 제 생각은:  **장점** - 모델 스왑 추가 비용 없음 — 8B가

### Extracted Facts

| # | type | evidence (first 120) |
|---|------|---------------------|
| 0 | text | 모델 스왑 추가 비용 없음 |
| 1 | text | 8B가 이미 로드되어 있으므로 mcp_cosine이 추가로 로드할 필요가 없음 |
| 2 | text | 7B가 이미 로드되어 있으므로 mcp_enrich가 추가로 로드할 필요가 없음 |
| 3 | text | 다음 루프가 이전 루프에서 중단된 곳부터 이어갈 수 있음 (체크포인트 기반) |

### Enrich Metadata

- **tldr**: User confirms the proposed chain structure and design.
- **intent**: other
- **category**: decision
- **entities**: 6 files, 5 funcs, 2 techs, 0 users
  - files: ['day_cycle.sh', 'embed_batch.py', 'mcp_cosine.py', 'extract.py', 'mcp_enrich.py', 'day_verify.py']
  - funcs: ['embed_batch', 'mcp_cosine', 'extract', 'mcp_enrich', 'day_verify']
  - techs: ['Python', 'FastAPI']
- **tags**: ['chain_structure', 'model_switching', 'design_confirmation']
- **faithfulness**: day_cycle.sh=100 GROUNDED, embed_batch.py=100 GROUNDED, mcp_cosine.py=100 GROUNDED, extract.py=100 GROUNDED, mcp_enrich.py=100 GROUNDED, day_verify.py=100 GROUNDED, Python=0 UNGROUNDED, FastAPI=0 UNGROUNDED, embed_batch=100 GROUNDED, mcp_cosine=100 GROUNDED, extract=100 GROUNDED, mcp_enrich=100 GROUNDED, day_verify=100 GROUNDED, tldr=0 UNGROUNDED
- ⚠️ **missing files**: ['day_cycle.sh', 'embed_batch.py', 'mcp_cosine.py', 'extract.py', 'mcp_enrich.py', 'day_verify.py']


---

## [10] d2322b2d (2026-06-13 02:43:06.125+00)

### User Turn

너가 제대로 이해했는지. 다시 설명

### Response (text)

네, 제가 이해한 내용을 다시 정리하겠습니다.  **매 사이클마다 실행되는 전체 파이프라인**:  ``` Loop N:   embed_batch  (8B, :8081)  → 새 turn embedding_f16 저장   extract      (7B, :8082)  → fact 추출   mcp_enrich   (7B, :8082)  → tldr/intent/entities/tags JSON 생성 (텍스트만)                                entity/tldr cosine 검증은 ❌ skip → "cosine_status": "pending"   verify       (14B, :8083) → 검토  Loop N+1:   embed_batch  (8B, :8081)  → 새 turn embedding_f16 저장   mcp_cosine   (8B, :8081)  → Loop N의 pending mcp_meta 읽어서                                8B로 entity/tldr embedding → turns.embedding_f16과 cosine 비교                                결과

### Extracted Facts

| # | type | evidence (first 120) |
|---|------|---------------------|
| 0 | text | 매 사이클마다 실행되는 전체 파이프라인 |
| 1 | text | Loop N: embed_batch (8B, :8081) → 새 turn embedding_f16 저장 |
| 2 | text | Loop N: extract (7B, :8082) → fact 추출 |
| 3 | text | Loop N: mcp_enrich (7B, :8082) → tldr/intent/entities/tags JSON 생성 (텍스트만) |
| 4 | text | Loop N: verify (14B, :8083) → 검토 |

### Enrich Metadata

- **tldr**: User wants a re-explanation of the pipeline and verification process.
- **intent**: request
- **category**: explanation
- **entities**: 1 files, 2 funcs, 4 techs, 0 users
  - files: ['relative/file/path.py']
  - funcs: ['mcp_enrich', 'mcp_cosine']
  - techs: ['Python', 'FastAPI', 'sentence_transformers', 'MiniCheck']
- **tags**: ['pipeline', 'verification', 'cosine', '8b', '7b']
- **faithfulness**: relative/file/path.py=0 UNGROUNDED, Python=0 UNGROUNDED, FastAPI=0 UNGROUNDED, sentence_transformers=100 GROUNDED, MiniCheck=100 GROUNDED, mcp_enrich=100 GROUNDED, mcp_cosine=100 GROUNDED, tldr=0 UNGROUNDED
- ⚠️ **missing files**: ['relative/file/path.py']


---

# Summary

총 10개 turn 검토.
각 항목별로 확인할 점:
1. **Extract hallucination** — facts가 원본 turn과 일치하는가?
2. **Enrich tldr** — 한 줄 요약이 정확한가?
3. **Enrich intent/category** — 의도 분류가 맞는가?
4. **Enrich entities/files** — 코드베이스와 일치하는가? 누락/환각?
5. **Enrich tags** — 검색에 유용한가?
6. **Faithfulness** — reranker 점수가 실제로 믿을 만한가? (거짓 긍정/부정)
