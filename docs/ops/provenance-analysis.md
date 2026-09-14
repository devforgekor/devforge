# Provenance Backfill Analysis — Gate 6 통과 대상

> **Status:** analysis · **Date:** 2026-09-14 · **Owner:** devforge
> **원인:** turns.source 7160/7160 = `unknown` (provenance 0%)
> **영향:** Gate 6 "provenance 동작: 신규 100% + legacy:pre-2026-09 no-op" 불통과

---

## 1. 현황 확인 (수동 검증)

| 항목 | 값 | 확인 방법 |
|---|---|---|
| 총 turns | 7160 | DB count |
| source=unknown | 7160 (100%) | `GROUP BY source` |
| source=NULL | 0 | `IS NULL` check |
| source≠unknown | 0 | distinct query |
| turns 생성 기간 | 2026-05-19 ~ 2026-09-14 (118일) | min/max created_at |
| 관찰(observations) 7일 | 529건 | observations table |
| observations 1시간 | 0건 | — |

**결론:** 모든 turns가 `unknown` source. 원본 로그 보존 여부 확인 필요.

---

## 2. 원인 분석

### 가설 A: 원본 에이전트 로그 미보존
- turns 테이블에 source 정보가 기록되지 않음
- turn_watcher가 raw 파싱 시 source 추출 실패
- 확인: `/home/opc/` 하위의 agent 로그 파일 (`*.jsonl`, `*_turns*` 등) 존재 여부

### 가설 B: provenance 설정 누락
- MCP ingress 경로에서 source 정보 전달 미구현
- ADR-0006의 provenance 조건 미충족
- 확인: `scripts/hooks/` 내 source 설정 로직

### 가설 C: 기존 turns는 unknown, 신규 turns에 source 기록 예정
- schema에 source 컬럼은 존재하나, 기존 데이터는 백필 안됨
- 확인: `turns` 테이블 스키마 + turn_watcher 코드

---

## 3. 확인 사항 (수동 검증 결과)

| 확인 | 결과 | 근거 |
|---|---|---|
| turns.source 컬럼 존재 | ✅ | `\d turns` — `source text NOT NULL DEFAULT 'unknown'` |
| 원본 로그 보존 여부 | ❓ 미확인 | 별도 확인 필요 |
| turn_watcher source 파싱 | ❌ 미설정 | `_insert_chunk` (turn_watcher.py:245) — source 컬럼 없음 |
| MCP regular turns source | ❌ 미설정 | mcp_server.py:173 INSERT — source 컬럼 없음 |
| MCP ingest source | ❌ 미설정 | mcp_server.py:530 INSERT — source 추출 후 `agent`에만 사용, turns.source 미삽입 |
| SSE turns source | ❌ 미설정 | mcp_server_sse.py:222 INSERT — source 컬럼 없음 |
| 향후 turns에 source 기록 여부 | ❌ 전 경로 미설정 | 모든 INSERT에 source 컬럼 누락 |

### 핵심 발견

**`turns.source`는 모든 삽입 경로에서 설정되지 않습니다.** 4개 경로 모두 source 컬럼을 INSERT에 포함하지 않음:
- turn_watcher.py:245 — `source_message_id, agent, meta`만 삽입, source 없음
- mcp_server.py:173 — `conversation_id, seq, user_turn, text, thinking, agent`만 삽입
- mcp_server.py:530 — ingest에서도 source 추출 후 `agent`에만 사용, turns.source 미삽입
- mcp_server_sse.py:222 — 위와 동일

**`source`는 `DEFAULT 'unknown'`** (스키마 레벨)로 모든 기존 turns는 100% unknown.

---

## 4. 대응 방안 (우선순위)

### 즉시 (W1 내) — 코드 수정 완료 (rf-triage-07)
**provenance 코드 고침: 4개 파일 INSERT에 `source` 컬럼 추가**

| 파일 | 변경 | source 값 |
|---|---|---|
| `scripts/turn_watcher.py` | INSERT 컬럼 + VALUES | `esc_sql(source)` — 원본 세션 source |
| `scripts/mcp_server.py:mem_save` | INSERT 컬럼 + VALUES | `esc_sql(tag)` — 세션 출처 태그 |
| `scripts/mcp_server.py:ingest` | INSERT 컬럼 + VALUES | `esc_sql(source)` — JSON payload 추출 |
| `scripts/mcp_server_sse.py` | INSERT 컬럼 + VALUES | `esc_sql(tag)` — 세션 출처 태그 |

**배포 필요**: `systemctl --user restart devforge-turn-watcher` + `devforge-mcp` 컨테이너 재시작

### 즉시 (W1 내) — 기존 turns 처리 (3가지 옵션)

| 옵션 | 설명 | Gate 6 | 결정 |
|---|---|---|---|
| **A** (권장) | `legacy:pre-2026-09` 마커 UPDATE | 통과 | **아래 결정 필요** |
| **B** | 원본 로그에서 백필 | 통과 | 원본 로그 확인 필요 |
| **C** | 변경 없음 | 불통과 | 불가 |

### W2
- 신규 turns source ≠ unknown 확인 (100건 샘플)
- 기존 turns 마커 확인 (옵션 A 적용 시)
- 상세: `docs/ops/provenance-marker-sql.md`

### W2
3. **provenance 100% 검증**
   - 백필 완료 후 `SELECT distinct source FROM turns` 결과 확인
   - 신규 turn 100건에 대해 source ≠ unknown 확인

---

## 5. 수락기준 제안 (v5 §2.8 H0 기준 참조)

| 시나리오 | 판정 | 조치 |
|---|---|---|
| 원본 로그 보존됨 | **무해** | 백필 완료, source 기록 정상화 |
| 원본 로그 미보존 | **주의** | `legacy:pre-2026-09` 마커 + 수동 검증 |
| 향후 turns source 미기록 | **해결** | 코드 수정 완료 (4개 파일 INSERT) |

---

## 6. 확인을 위한 명령어

```bash
# 1. 원본 에이전트 로그 보존 여부
find /home/opc -name "*.jsonl" 2>/dev/null | head -20
find /home/opc -name "*turn*" -o -name "*agent*" 2>/dev/null | head -20
ls /home/opc/.local/share/opencode/ 2>/dev/null
ls /home/opc/.cache/opencode/ 2>/dev/null

# 2. turn_watcher 로그 확인
podman logs devforge-turn-watcher --tail 50 2>/dev/null | grep -i source | head -10

# 3. turns 테이블 스키마 확인
podman exec -i postgres psql -U postgres -d devforge_app -c "\d turns" 2>/dev/null

# 4. MCP server source 관련 코드 확인
grep -rn "source" /opt/projects/server/scripts/mcp_server.py 2>/dev/null | head -20
grep -rn "source" /opt/projects/server/src/devforge/adapters/driving/mcp/ 2>/dev/null | head -20
```
