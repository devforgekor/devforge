# Observations 테이블 구현 계획

**상태**: 승인 대기  
**작성일**: 2026-05-17  
**관련**: Qwen worker observations 저장 단절 해소

## 배경

- Qwen worker는 15분마다 실행되어 `observations`(사실/이상 탐지)를 생성하나, `print()`로 로그 출력 후 소멸됨
- `obs_dec` 테이블은 승인된 판단만 저장 (MCP `mem_save` → `meta.type=="decision"`)
- Qwen의 관찰(증거)과 에이전트의 결정(판단)은 분리 저장되어야 함

## 변경 범위

### P0 — 구현 (5개 파일)

| # | 파일 | 변경 내용 |
|---|------|-----------|
| 1 | `api/db.py` | SCHEMA_SQL에 observations 테이블 추가 |
| 2 | `docs/schema.sql` | observations 스키마 문서화 |
| 3 | `scripts/lib/qwen_executor.py` | `execute_observations()` 함수 추가 |
| 4 | `scripts/qwen_worker.py` | `print()` → `execute_observations()` 호출 |
| 5 | `scripts/gen_tmux_banner.py` | observations 총계 조회, 배너에 표시 |

### P1 — 후속 (7개 파일)

| # | 파일 | 변경 내용 |
|---|------|-----------|
| 6 | `api/stats.py` | GET /stats에 total_observations 추가 |
| 7 | `scripts/session_start.py` | SessionStart 컨텍스트에 최근 observations 포함 |
| 8 | `scripts/gen_server_state.py` | changelog 항목에 observations 필드 추가 |
| 9 | `scripts/update_handover.py` | observations 키 보존 |
| 10 | `scripts/cli.py` | `observation recent` 서브커맨드 추가 |
| 11 | `api/mcp_server.py` | `obs_recent` MCP 도구 추가 |
| 12 | `docs/design.md` | observations 테이블 문서화 |

### 시스템 파일 — 변경 없음

- `devforge-qwen-worker.timer` — 기존 15분 주기 그대로 사용
- `motd-gen.timer` — gen_tmux_banner.py가 observations 포함하므로 자동 반영

## DB 스키마

```sql
CREATE TABLE IF NOT EXISTS observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    observation TEXT NOT NULL CHECK (length(trim(observation)) > 0),
    category TEXT NOT NULL DEFAULT 'general',
    source TEXT NOT NULL DEFAULT 'qwen_worker',
    context JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_observations_created ON observations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_category ON observations(category);
```

- `obs_dec` 테이블과 완전 분리 (FK 없음, turn_id 불필요)
- `context` JSONB: 관련 메타데이터 (orphan_agents, turn_count 등)
- `category`: `orphan_turns`, `orphan_worklogs`, `no_match`, `anomaly` 등

## 핵심 구현 상세

### `execute_observations()` — qwen_executor.py

```python
def execute_observations(observations: list, category: str = "general",
                         context: dict = None) -> int:
    if not observations:
        return 0
    
    # 방어: 테이블 없으면 생성
    _psql("""
        CREATE TABLE IF NOT EXISTS observations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            observation TEXT NOT NULL CHECK (length(trim(observation)) > 0),
            category TEXT NOT NULL DEFAULT 'general',
            source TEXT NOT NULL DEFAULT 'qwen_worker',
            context JSONB DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    
    ctx_json = json.dumps(context or {}, ensure_ascii=False)
    count = 0
    for obs in observations:
        safe = obs.replace("'", "''")  # SQL injection 방어
        _psql(
            f"INSERT INTO observations (observation, category, source, context) "
            f"VALUES ('{safe}', '{category}', 'qwen_worker', '{ctx_json}'::jsonb)"
        )
        count += 1
    
    print(f"  Observations saved: {count}")
    return count
```

### 배너 표시 — gen_tmux_banner.py

```
DevForge | 컨테이너 5/5 | 서비스 6 | CPU 0.3/0.6/0.6 | Mem 12Gi/22Gi (56%) | Obs 15
  Tasks      Claude  3/15     Copilot 11/15    Gemini  1/15     Qwen    0/15   
  Decisions  Claude  0/120    Copilot 0/172    Gemini  0/1      Qwen    0/0    
```

- `Obs 15`: 어제 observations 총계 (헬스바에 추가)
- `obs_dec` 행: 기존 유지 (per-agent, obs_dec 테이블 기준)
- nightly 이상 시 Obs 수치도 nightly 색상 적용

## 실행 순서

1. **스키마**: `api/db.py` + `docs/schema.sql` 수정
2. **저장 로직**: `qwen_executor.py`에 `execute_observations()` 추가
3. **호출부**: `qwen_worker.py` L362-364 교체
4. **배너**: `gen_tmux_banner.py`에 observations 쿼리 + 표시 추가
5. **검증**: `python3 scripts/gen_tmux_banner.py --stdout` 테스트
6. **DB 마이그레이션**: API 재시작 또는 수동으로 테이블 생성
7. **캐시 갱신**: `python3 scripts/gen_tmux_banner.py` 실행

## 검증 항목

- [ ] Qwen worker 실행 후 `SELECT COUNT(*) FROM observations` 증가 확인
- [ ] 작은따옴표 포함된 observation도 정상 INSERT 확인
- [ ] 배너에 observations 총계 표시 확인
- [ ] nightly 색상이 observations에도 적용되는지 확인
