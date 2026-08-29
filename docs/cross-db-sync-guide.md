# Cross-DB 데이터 동기화 가이드

> 로컬 PostgreSQL ↔ 원격 NeonDB 간 데이터 이동 시 컬럼(UOM) 불일치 문제와 해법

## 1. 문제: 두 DB의 스키마가 다를 수 있다

로컬 DevForge DB(컨테이너)와 NeonDB(클라우드)는 같은 `news_articles` 테이블을 갖고 있지만,
컬럼 갯수·순서·타입이 다를 수 있다.

```
로컬 (localhost:5432)                  Neon (ep-round-hill.aws.neon.tech)
─────────────────────────              ─────────────────────────
 url              text                  url              text
 title            text                  title            text
 published_at     timestamptz           published_at     timestamptz
 full_text        text                  full_text        text
 highlights       jsonb                 highlights       jsonb
 embedding        vector(768)  ← 없음   ← 없음
 metadata         jsonb       ← 없음   ← 없음
```

**원인**: 로컬 DB는 DevForge 파이프라인(분류, 임베딩, 요약)에서 사용하는 추가 컬럼이 있고,
NeonDB는 Vercel 웹앱용으로 최소 컬럼만 유지한다.

## 2. 세 가지 원칙

### 원칙 1 — 명시적 컬럼 리스트(explicit column list)

```sql
-- ❌ 위험: 컬럼 순서(UOM)에 의존 → 데이터가 잘못 들어갈 수 있음
INSERT INTO news_articles VALUES ('url', 'title', ...);

-- ✅ 안전: 이름 기준 매핑 → 순서가 달라도 OK
INSERT INTO news_articles (url, title, source, published_at, ...)
VALUES ('url', 'title', 'source', '2026-08-29', ...);
```

**왜 명시적 리스트가 안전한가:**

| 특징 | 설명 |
|------|------|
| 이름 기준 매핑 | 컬럼 순서가 달라도 자동 정렬 |
| 스키마 변경 내성 | 대상 테이블에 컬럼 추가/삭제돼도 명시적 리스트만 수정 |
| 조기 실패 | 오타·누락 시 쿼리 단계에서 즉시 에러 |
| 가독성 | 어떤 컬럼에 어떤 값이 들어가는지一目瞭然 |

### 원칙 2 — COPY는 "헤더 + 컬럼 목록"으로

```bash
# 로컬 CSV dump (헤더 포함)
podman exec postgres psql -U postgres -d devforge_app -c \
  "\COPY (SELECT url, title, source FROM news_articles WHERE ...) TO STDOUT WITH CSV HEADER"

# Neon CSV 로드 (컬럼 리스트 + 헤더)
psql "$NEON_URL" -c \
  "\COPY news_articles (url, title, source) FROM STDIN WITH CSV HEADER"
```

**헤더가 중요한 이유:**
- 어떤 컬럼이 오는지 명시 → 순서 오류를 즉시 탐지
- 대상 컬럼 리스트와 함께 사용하면 양쪽 스키마 차이를 이름 기준으로 자동 대응

### 원칙 3 — PK/UNIQUE 충돌 시 upsert로 안전하게

```sql
INSERT INTO news_articles (url, title, full_text)
VALUES ('https://...', 'title', 'body')
ON CONFLICT (url) DO UPDATE SET
  full_text = EXCLUDED.full_text,
  title = EXCLUDED.title;
```

- `id`는 SERIAL PRIMARY KEY이지만, `url`에 UNIQUE 제약조건이 있으므로 `ON CONFLICT (url)`로 중복 탐지 가능
- 동일 URL이 이미 있으면 UPDATE로 덮어씀
- `EXCLUDED`는 INSERT하려던 값에 대한 특별 참조
- 중복 에러 없이 멱등(idempotent)하게 동작

> **주의**: `ON CONFLICT (id)`를 쓰면 로컬과 Neon의 시퀀스가 다르기 때문에 의도치 않은 동작 발생 가능. `url`(UNIQUE)을 충돌 키로 사용해야 함.

## 3. pg_dump를 사용한 안전한 동기화

### pg_dump가 자동으로 해주는 것
- 모든 컬럼을 **명시적 리스트**로 나열한 INSERT 문 생성
- 문자열 내 따옴표·개행 문자를 **자동 이스케이프**
- JSONB, 배열 등 복합 타입을 올바르게 직렬화

### 컨테이너에서 --where 인자 전달 문제

```bash
# ❌ 실패: 따옴표 중첩으로 인식 불가
podman exec postgres pg_dump --where="collected_at > '2026-08-22'"

# ✅ 해법 1: stdout 리디렉션 (가장 일반적)
podman exec -i postgres pg_dump -U postgres -d devforge_app \
  --table=news_articles --data-only --column-inserts \
  > /tmp/neon_sync.sql

# ✅ 해법 2: 히어도쿠멘트 (heredoc, 따옴표 중첩 회피)
podman exec -i postgres bash <<'EOF'
pg_dump -U postgres -d devforge_app \
  --table=news_articles --data-only --column-inserts \
  --where="collected_at > current_date - interval '7 days'"
EOF
```

### 전체 파이프라인 (안전 버전)

```bash
#!/bin/bash
set -euo pipefail

# 1. 로컬 DB에서 dump
podman exec -i postgres pg_dump -U postgres -d devforge_app \
  --table=news_articles --data-only --column-inserts \
  > /tmp/neon_sync.sql

# 2. Neon에 적용 (파일이므로 명령줄 이스케이프 문제 없음)
psql "$NEON_URL" -f /tmp/neon_sync.sql

# 3. 오래된 데이터 정리
psql "$NEON_URL" -c "DELETE FROM news_articles WHERE collected_at < now() - interval '7 days'"
```

## 4. Shebang 파일로 실행

```bash
#!/bin/bash
# ============================================
# sync-neon.sh — lokal DB → NeonDB 동기화
# ============================================
set -euo pipefail

NEON_URL="${NEON_DATABASE_URL:?NEON_DATABASE_URL not set}"

# 연결 확인 (Neon 서버리스 특성상 cold start 시 시간 소요)
echo "[sync] Checking Neon connection..."
psql "$NEON_URL" -c "SELECT 1" || { echo "ERROR: Neon 연결 실패. URL과 네트워크를 확인하세요."; exit 1; }

echo "[sync] Dumping local DB..."
podman exec -i postgres pg_dump -U postgres -d devforge_app \
  --table=news_articles --data-only --column-inserts \
  > /tmp/neon_sync.sql

ROW_COUNT=$(grep -c "^INSERT" /tmp/neon_sync.sql || echo "0")
echo "[sync] Dumped ${ROW_COUNT} INSERT statements"

echo "[sync] Applying to Neon..."
psql "$NEON_URL" -f /tmp/neon_sync.sql

echo "[sync] Pruning old data (7 days+)..."
psql "$NEON_URL" -c "DELETE FROM news_articles WHERE collected_at < now() - interval '7 days'"

echo "[sync] Done"
```

## 5. Blind Spot — 놓치기 쉬운 것들

| Blind Spot | 설명 | 대책 |
|-----------|------|------|
| 타입 차이 | 로컬 `jsonb` ↔ Neon `text` → 캐스팅 실패 | `\d table`로 양쪽 타입 비교 |
| 컬럼명 차이 | 로컬 `highlights_ko` ↔ Neon `highlights` → INSERT 시 컬럼 매핑 실패 | `pg_dump --column-inserts` 사용으로 컬럼명 명시적 지정 |
| DEFAULT 값 | `created_at` DEFAULT now()가 Neon에만 있음 | 명시적 컬럼 리스트로 제어 |
| 시퀀스/ID | 로컬 serial ID vs Neon serial ID 충돌 | `id` 컬럼은 동기화 대상에서 제외 |
| PK vs UNIQUE | `id`(PK)는 시퀀스가 다르므로 충돌 키로 부적합 | `url`(UNIQUE)을 `ON CONFLICT` 키로 사용 |
| 인덱스 | PK가 다르면 `ON CONFLICT`가 실패 | 양쪽 PK/UNIQUE 정의 확인 |
| 타임존 | 로컬 timestamptz vs Neon timestamptz | 보통 문제 없으나 `AT TIME ZONE` 필요 시 |
| 트랜잭션 | 대량 INSERT 실패 시 부분 적용 | `BEGIN ... COMMIT`으로 원자성 보장 |

## 6. 핵심 요약

```
명시적 컬럼 리스트 + COPY 헤더 + ON CONFLICT(UNIQUE) = 스키마 차이에 강한 동기화
```

1. **절대 `INSERT INTO table VALUES (...)`를 쓰지 말 것** — 항상 컬럼 리스트를 명시
2. **절대 컬럼 순서(UOM)에 의존하지 말 것** — 이름 기준으로 매핑
3. **pg_dump의 `--column-inserts`를 사용할 것** — 이스케이프와 컬럼 리스트를 자동 처리
4. **COPY는 HEADER + 대상 컬럼 리스트를 함께 쓸 것** — 순서 불일치를 즉시 탐지
5. **`ON CONFLICT (url)`로 멱등성을 보장할 것** — `id`(PK) 대신 `url`(UNIQUE)을 충돌 키로 사용