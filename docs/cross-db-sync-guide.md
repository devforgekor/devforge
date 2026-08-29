# Cross-DB 데이터 동기화 가이드

> 로컬 PostgreSQL ↔ 원격 NeonDB 간 `news_articles` 데이터 이동의 실제 구현 분석과 안전한 설계 원칙

**실측 기반 문서** — 2026-08-29 Deep Dive 분석으로 두 DB의 실제 스키마·데이터를 확인함.

---

## 1. 실제 스키마 비교 (2026-08-29 실측)

**결론: 두 DB는 스키마가 완전히 동일하다.** 22개 컬럼, 타입, DEFAULT, 제약조건이 모두 일치. 차이는 **데이터(id 시퀀스, dedup)** 뿐이다.

| 컬럼 | 타입 | DEFAULT | 비고 |
|------|------|---------|------|
| id | integer | `nextval(id_seq)` | **PK, 시퀀스 생성** |
| url | text | — | **UNIQUE 제약** |
| title | text | — | |
| source | text | — | |
| published_at | timestamptz | — | |
| collected_at | timestamptz | `now()` | |
| language | text | `'ko'` | |
| category | text | `'ai'` | |
| full_text | text | — | |
| highlights | jsonb | `'[]'` | |
| summary | text | — | |
| pipeline_state | text | `'raw'` | CHECK 제약 |
| concept_ids | jsonb | `'[]'` | GIN 인덱스 |
| relevance_score | real | `0.0` | |
| embedding | vector(1024) | — | **pgvector** |
| metadata | jsonb | `'{}'` | |
| created_at | timestamptz | `now()` | |
| updated_at | timestamptz | `now()` | |
| title_ko | text | — | |
| summary_ko | text | — | |
| highlights_ko | jsonb | — | |
| dedup_group_id | integer | — | **자기참조 FK** → id |

### 유일한 스키마 차이
- 로컬 DB에만 `idx_news_articles_collected_at` 인덱스가 존재 (Neon에는 없음) — 동기화에는 무관.

### 실측 데이터 (2026-08-29)

| 항목 | 로컬 | Neon |
|------|------|------|
| 기사 수 | 593 | 523 |
| id 범위 | 1..2384 | 1..2340 |
| id 시퀀스 last_value | 2387 | 2342 |
| `dedup_group_id` 채워짐 | 76개 | 12개 |

---

## 2. 실제 `_sync_to_neon()` 동작 방식 (collector.py:170)

현재 구현은 **pg_dump → delete-then-insert** 방식 이다:

```
1. 로컬 DB에서 최근 7일 기사 pg_dump (--column-inserts)
2. Neon에서 같은 범위(collected_at > cutoff) DELETE   ← id 충돌 회피
3. dump 파일을 Neon에 -f 로 적용
4. 7일 retention 초과분 Prune (DELETE)
```

```python
# 핵심: pg_dump가 id 포함, PK 충돌은 delete-then-insert로 회피
del_proc = psql neon_url DELETE FROM news_articles WHERE collected_at > cutoff
apply     = psql neon_url -f /tmp/neon_sync.sql   # pg_dump INSERT들
```

### ⚠️ 이 방식의 3가지 숨은 위험 (실측에서 드러남)

**위험 1 — id 시퀀스 역주행**
로컬 max_id=2384, Neon max_id=2340. pg_dump `--column-inserts`가 id를 포함한 INSERT를 생성하면:
- 같은 id가 이미 Neon에 있는 기사 → PK 충돌 (delete-then-insert로 완화)
- **하지만 Neon의 `id_seq`가 여전히 낮은 값(last_value=2342)** → 다음 신규 삽입에 역주행 가능
- `dedup_group_id` FK가 참조하는 id가 삭제되면 무결성 위반 가능

**위험 2 — `dedup_group_id` 자기참조 FK 순서 문제**
- pg_dump는 행을 id 순서로 내보내지만, 참조되는 id가 아직 삽입 안 된 상태면 FK 위반
- 현재는 Neon에 dedup 12개(로컬 76개) 뿐이라 문제 미발견 → **로컬처럼 dedup이 늘면 발생**

**위험 3 — 불필요한 컬럼 전송**
- Vercel 리스트/검색 API(`/api/articles`, `/api/stats`)는 `embedding`, `metadata`, `highlights`, `summary`, `full_text` 미사용
- 단, 상세 페이지(`/articles/[id]`)는 `SELECT *`로 전체 컬럼 사용 → `full_text`, `concept_ids` 등 실제 필요
- 벡터 1024차원 전송은 리스트용 동기화에서만 불필요한 대역폭·용량 낭비

### Vercel 실제 사용 컬럼

**리스트/검색 API** (route.ts:43-48, page.tsx:43-49):
```
id, title, title_ko, source, language, category,
summary_ko, highlights_ko, published_at, collected_at, url,
relevance_score, dedup_group_id
```

**상세 페이지** ([id]/page.tsx):
```
SELECT * FROM news_articles WHERE id = $1  -- 전체 컬럼 사용
```
→ `full_text`, `concept_ids`, `embedding`, `metadata`, `highlights`, `summary` 도 상세 페이지에서 사용

---

## 3. 설계 원칙 (스키마 변경 없이 동기화를 견고하게)

사용자 요청: **스키마/로직 변경 금지, 지식만.**

### 원칙 1 — 동기화 컬럼을 명시적으로 제한하라
`pg_dump` 대신 필요한 컬럼만 `SELECT`로 뽑아 오는 것이 안전:
```sql
-- Vercel 리스트/검색용 컬럼만 (embedding, metadata, id, dedup_group_id 제외)
SELECT url, title, title_ko, source, language, category,
       summary_ko, highlights_ko, published_at, collected_at, relevance_score
FROM news_articles
WHERE collected_at > ...;
```
이렇게 하면 `id` 시퀀스 역주행, `embedding` 대역폭 낭비, `dedup_group_id` FK 문제를 **한 번에 회피**.

### 원칙 2 — id는 절대 전송하지 말 것
- 로컬/Neon id 시퀀스가 다르므로 id를 옮기면 안 됨
- url(UNIQUE)이 진짜 식별자. 동기화의 충돌 키는 **url이어야 함**.

### 원칙 3 — upsert는 `ON CONFLICT (url)` 사용
```sql
INSERT INTO news_articles (url, title, title_ko, source, language, category,
                           published_at, collected_at, summary_ko, highlights_ko,
                           relevance_score)
VALUES (...)
ON CONFLICT (url) DO UPDATE SET
  title = EXCLUDED.title, title_ko = EXCLUDED.title_ko,
  published_at = EXCLUDED.published_at, collected_at = EXCLUDED.collected_at,
  summary_ko = EXCLUDED.summary_ko, highlights_ko = EXCLUDED.highlights_ko,
  relevance_score = EXCLUDED.relevance_score;
```
- `id`(PK) 대신 `url`(UNIQUE)을 키로 → 시퀀스 충돌 없음, 멱등
- PostgreSQL 17 문서 확인: UNIQUE 제약조건으로 `ON CONFLICT` 정상 동작

### 원칙 4 — dedup_group_id는 옮길 때 재매핑 필수
로컬 id와 Neon id가 다르므로 dedup 참조를 그대로 옮기면 FK 위반.
→ URL 기준으로 dedup_group_id 대상 url을 찾아 매핑하거나, 동기화 대상에서 제외.

---

## 4. 추천 안전 시나리오 (설계만)

```sql
-- 대상 컬럼 명시 (embedding, metadata, id, dedup_group_id 제외)
-- Neon 7일 이내 기사 대량 DELETE 후 재삽입 (delete-then-insert 유지)
BEGIN;
DELETE FROM news_articles WHERE collected_at > now() - interval '7 days';

INSERT INTO news_articles (url, title, title_ko, source, language, category,
                           published_at, collected_at, summary_ko, highlights_ko,
                           relevance_score)
SELECT url, title, title_ko, source, language, category,
       published_at, collected_at, summary_ko, highlights_ko, relevance_score
FROM dblink(...) -- 또는 호스트단 psql 파이프
ON CONFLICT (url) DO UPDATE SET
  title = EXCLUDED.title, title_ko = EXCLUDED.title_ko,
  published_at = EXCLUDED.published_at, collected_at = EXCLUDED.collected_at,
  summary_ko = EXCLUDED.summary_ko, highlights_ko = EXCLUDED.highlights_ko,
  relevance_score = EXCLUDED.relevance_score;
COMMIT;
```

**현실적인 대안** — 호스트에서 두 클라이언트 psql 파이프:
```bash
# 로컬 → Neon, 컬럼 명시 + ON CONFLICT 적용
podman exec postgres psql -U postgres -d devforge_app -c \
  "COPY (SELECT ... ) TO STDOUT" \
| psql "$NEON_URL" -c \
  "CREATE TEMP TABLE ... ; COPY ... FROM STDIN; INSERT ... ON CONFLICT (url) ..."
```

---

## 5. 테스트 검증 — id 시퀀스 역주행 발생 조건

| 시나리오 | delete-then-insert | 단순 INSERT |
|---------|--------------------|-------------|
| 같은 id가 이미 존재 | PK 충돌 (회피) | PK 충돌 |
| 신규 id < Neon 시퀀스 | 시퀀스 역주행 | 시퀀스 역주행 |
| 신규 id > Neon 시퀀스 | 정상 | 정상 |
| dedup FK 참조 대상 미삽입 | FK 위반 | FK 위반 |

**결론**: delete-then-insert도 id 임포트 시 시퀀스 역주행·FK 위반 리스크는 남음.
**명시적 컬럼 리스트로 id·embedding·dedup_group_id를 제외하는 것이 최선.**

---

## 6. 핵심 요약

```
동기화할 때 id·embedding·dedup_group_id를 빼면:
  - 시퀀스 역주행 ❌
  - 대역폭 낭비 ❌
  - FK 무결성 위반 ❌
  - 멱등성은 url(UNIQUE) + ON CONFLICT로 보장 ✅
```

1. **두 DB 스키마는 동일** — "컬럼 수·순서 다름"은 실제가 아님. 차이는 **데이터(id 시퀀스, dedup)**
2. **절대 `id`를 동기화하지 말 것** — 시퀀스가 다름
3. **url(UNIQUE)이 충돌 키** — `ON CONFLICT (url)` 사용 (PostgreSQL 17 UNIQUE 제약으로 검증됨)
4. **embedding(1024차원)은 리스트 API가 안 씀** — 리스트용 동기화에서 제외로 효율화 (상세 페이지는 사용)
5. **dedup_group_id는 옮기면 재매핑 필요** — 현재 FK 문제는 dedup 수 적어 잠복

---

## 7. 추후 개선 방향 (스키마 변경 필요 시, 현재 아님)
- Neon에 `embedding` 없이 `news_articles_web` View/테이블 분리
- 동기화 전용 유틸 `sync-news.py` 스크립트화 (Shebang + `set -euo pipefail`)
- `_sync_to_neon()`이 Vercel 리스트 API 미사용 컬럼 전송 중단