# Provenance 마커 SQL — 실행 전 백업 필요

> **Status:** ready · **Date:** 2026-09-14
> **용도:** 기존 7163건 turns.source='unknown'을 마커로 전환

---

## 옵션 A: legacy 마커 적용 (권장)

```sql
-- 1. 백업 (반드시 실행)
CREATE TABLE turns_source_backup_20260914 AS
SELECT id, conversation_id, seq, source, created_at
FROM turns
WHERE source = 'unknown';

-- 2. 마커 적용
UPDATE turns
SET source = 'legacy:pre-2026-09'
WHERE source = 'unknown';

-- 3. 확인
SELECT source, count(*) FROM turns GROUP BY source;
-- 기대: legacy:pre-2026-09=7163, (신규는 여전히 unknown 또는 code fix 후 정상)
```

## 옵션 B: 원본 로그 백필 (원본 로그 보존 시만)

```sql
-- 원본 로그에서 source를 추출하여 UPDATE
-- 필요: /home/opc/ 하위 agent jsonl 파일 파싱 결과 기반
-- 임시 예시 (실제 구현은 parser 수준에서 처리):
UPDATE turns SET source = 'claude-code'
WHERE id IN (SELECT turn_id FROM ...);  -- parser 결과 기반
```

## 옵션 C: 변경 없음 (Gate 5 측정용)

```sql
-- 아무 조치 없음. Gate 5에서 수치 정합 시 결정.
-- 단, Gate 6 통과 불가 (provenance 0%).
```

---

## 실행 방법

```bash
# 1. 백업
podman exec -i postgres psql -U postgres -d devforge_app -c "
CREATE TABLE turns_source_backup_20260914 AS
SELECT id, conversation_id, seq, source, created_at FROM turns WHERE source='unknown';"

# 2. 마커 적용 (옵션 A)
podman exec -i postgres psql -U postgres -d devforge_app -c "
UPDATE turns SET source='legacy:pre-2026-09' WHERE source='unknown';"

# 3. 확인
podman exec -i postgres psql -U postgres -d devforge_app -c "
SELECT source, count(*) FROM turns GROUP BY source;"
```
