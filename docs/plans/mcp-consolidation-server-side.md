# 계획 — Deep Dive 외부기능의 서버측 구현 (MCP 통합/축소)

> 작성: 2026-09-11 · 상태: **proposed** (사용자 승인 대기)
> 전제: "MCP는 전송 계층일 뿐" — 능력은 이미 `/opt/projects/server/scripts/`의 서버 코드다. 전송을 줄이고, 정확도는 캐시·리랭크·인용으로 확보한다.
> 근거: `docs/reports/control-plane-registry-research.md` §부록(웹 검증), MCP 공식 아키텍처, Anthropic code-execution-with-MCP, NVIDIA/Pinecone 리랭크 벤치, arXiv 2605.24660(툴 과다 시 선택 정확도 하락).
> 관련 규칙: `llm-agent-rule.md` Deep Dive, `AGENTS.md` MCP Tools / Shrimp+LSP.

---

## 1. 목표 / 비목표
**목표**
- Deep Dive가 의존하는 MCP를 **4종(yggdrasil·filesystem·lsp·context7/exa)+shrimp → 2종(devforge-mcp·lsp)** 으로 축소.
- 검색/문서/URL 조회를 **서버측 `lib/research/` + CLI**로 일원화 → 전송·스키마 오버헤드 제거.
- 결과를 **Postgres 캐시 + cross-encoder 리랭크 + 인용**으로 정규화 → 재현성·정확도 확보.
- 태스크는 기존 `tasks` DB(`cli.py task`)로 단일화(SSOT).

**비목표**
- LSP 제거(대체 불가, 유지).
- 외부 API 자체 교체(Exa/Brave/Context7 소스는 유지 — 정확도의 원천).
- Deep Dive 7단계 구조 변경(스텝 내용만 대체).

## 2. 방향 (Before → After)

```
[Before]  Deep Dive ─ MCP ─┬─ search-proxy   (stdio, Brave/Tavily/youcom)
                           ├─ exa-search      (stdio, Exa)
                           ├─ context7        (stdio, Context7)
                           ├─ fetch           (stdio, uvx)
                           ├─ filesystem      (stdio)
                           ├─ shrimp          (stdio, Node)  ← tasks DB와 중복
                           └─ yggdrasil       (stdio)        ← 스캐폴드

[After]   Deep Dive ─┬─ devforge-mcp (HTTP 1개; research/lsp-intel/obs/action/deepdive)
                     └─ lsp
          검색·문서·URL ──▶ cli.py research … ──▶ lib/research/ ──▶ 캐시/리랭크
          태스크        ──▶ cli.py task …    (기존 tasks DB)
          계획          ──▶ docs/plans/ + DB (yggdrasil 대체는 Phase 6, 선택)
```

## 3. 신규 / 변경 컴포넌트

### 3.1 신규 `scripts/lib/research/` (핵심)
기존 MCP 래퍼의 **코어를 이동**(신규 작성 최소). 기존 파일은 전환기 동안 얇은 래퍼로 유지.

| 파일 | 이관 원본 | 공개 함수 |
|---|---|---|
| `lib/research/web.py` | `proxies/search.py::SearchProxy._search_provider/_call_api` | `web_search(query, max_results=5) -> list[dict]` |
| `lib/research/exa.py` | `exa_mcp.py::_call_exa_api/_handle_exa_search/_handle_exa_get_contents` | `exa_search(query, num=10)`, `exa_contents(urls)` |
| `lib/research/context7.py` | `context7_mcp.py::_call_api/_handle_resolve_library_id/_handle_query_docs` | `resolve_library_id(name)`, `query_docs(library_id, query)` |
| `lib/research/fetch.py` | `flaresolverr_bypass` / `mcp-server-fetch` 로직 | `fetch_url(url, max_chars=50000)` |
| `lib/research/cache.py` | 신규 | `make_key()`, `get()`, `put()`, `purge()` |
| `lib/research/rank.py` | `lib/llm_client.reranker_score` 재사용 | `rerank(query, items, top_k)`, `to_citations(items)` |
| `lib/research/__init__.py` | 신규(오케스트레이터) | `research(query, mode, limit, rerank, use_cache, ttl_sec)` |

**재사용(변경 없음)**: `lib/auth/key_rotator.KeyRotator`, `lib/auth/api_key_cipher`, `lib/db.{esc_sql,psql_json,psql_ok}`, `lib/llm_client.reranker_score`, `lib/search/hybrid.py`(RERANKER_URL=`http://127.0.0.1:8080/v1/rerank`).

### 3.2 DB — `research_cache`
```sql
CREATE TABLE IF NOT EXISTS research_cache (
  id          BIGSERIAL PRIMARY KEY,
  query_hash  TEXT        NOT NULL,
  provider    TEXT        NOT NULL,
  query       TEXT        NOT NULL,
  results     JSONB       NOT NULL,
  fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at  TIMESTAMPTZ NOT NULL,
  hit_count   INT         NOT NULL DEFAULT 0,
  UNIQUE (query_hash, provider)
);
CREATE INDEX IF NOT EXISTS idx_research_cache_hash ON research_cache (query_hash);
```
- DDL은 `docs/specs/schema.sql`에 동기화. TTL 기본: `docs` 7d, `web`/`exa` 1d. 조회 시 `hit_count++`.

### 3.3 CLI — `cli.py research`
기존 `cmd_*` + `add_parser` 패턴 준수(`cli.py` 내 `sub = ...`).
```
cli.py research search "질문" [--mode auto|web|exa|docs] [--limit N] [--no-rerank] [--no-cache] [--json]
cli.py research docs "library" "질문" [--json]
cli.py research fetch <url> [--max-chars N] [--json]
cli.py research cache stats|purge [--older-than D]
```

### 3.4 MCP(선택, 최소) — devforge-mcp에 **단일 툴**
CLI-first를 기본으로 하되, 구조화 호출이 필요하면 `mcp_server.py`에 툴 **1개만** 추가(스키마 비용 최소).
```python
@mcp.tool(name="research")
async def research_tool(query: str, mode: str = "auto", limit: int = 5, rerank: bool = True) -> str: ...
```

## 4. 인터페이스 (시그니처)
```python
# lib/research/__init__.py
def research(query: str, mode: str = "auto", limit: int = 5,
             rerank: bool = True, use_cache: bool = True,
             ttl_sec: int | None = None) -> dict:
    """mode: auto|web|exa|docs. 반환:
    {"query","mode","results":[{"title","url","snippet","source","score"}],
     "meta":{"cache_hit":bool,"reranked":bool,"provider":str,"count":int}}"""

# CLI JSON 계약은 위 dict를 그대로 출력(머신리더블).
```

## 5. 단계 (Phase)

각 단계는 독립 롤백 가능. Acceptance 통과 시 다음 단계.

**Phase 0 — 기준선/롤백 (0.5d)**
- MCP 설정 백업: `cp ~/.claude/mcp.json{,.bak}` 및 `~/.config/opencode/opencode.json{,.bak}`.
- 현재 툴 스키마 토큰 측정(서버별) 기록 → 이후 비교 기준.
- Acceptance: 백업 존재 + 기준 토큰 수 기록.

**Phase 1 — 코어 이관 (1d)**
- `proxies/search.py`·`exa_mcp.py`·`context7_mcp.py`에서 **API 호출 코어를 `lib/research/`로 이동**.
- 기존 MCP 파일은 `lib/research/*`를 호출하는 **얇은 래퍼**로 변경(동작 불변 → 무중단).
- Acceptance: `python3.11 -c "from lib.research import web"`, 기존 MCP `web_search`/`exa_search`/`query_docs` 응답 불변.

**Phase 2 — 캐시 + 리랭크 + 인용 (1d)**
- `research_cache` 마이그레이션, `cache.py`/`rank.py` 구현.
- `research()`에 캐시조회→없으면 검색→리랭크→저장. 인용 정규화.
- Acceptance: 동일 쿼리 2회 → 2회차 `meta.cache_hit=true`, `hit_count` 증가, API 미호출. 리랭크로 상위 결과 재정렬 확인.

**Phase 3 — CLI (0.5d)**
- `cli.py research ...` 추가. `--json` 계약 안정.
- Acceptance: `cli.py research search "python asyncio" --json` 이 스키마대로 출력, 문서 모드(`--mode docs`) 동작.

**Phase 4 — Deep Dive 규칙 전환 (0.5d)**
- `llm-agent-rule.md`·`AGENTS.md` 4단계를 `cli.py research`로 교체(새 선택 기준 유지: API/버전→`--mode docs`, 개념/사례→`--mode web|exa`).
- Acceptance: 규칙 문서에 MCP(context7/exa/search-proxy) 의존 서술 제거, CLI 예시 반영.

**Phase 5 — MCP 등록 제거 (0.5d)**
- `~/.claude/mcp.json`·`~/.config/opencode/opencode.json`에서 `search-proxy`·`exa-search`·`context7`·`fetch`·`filesystem` 제거(전환기 동안 `enabled:false` → 확인 후 삭제).
- `devforge-mcp`에 `research` 툴(선택) 추가 여부 결정.
- Acceptance: 세션 초기화 정상, 툴 목록에서 해당 서버 사라짐, 토큰 오버헤드 감소 측정.

**Phase 6 — 태스크/계획 단일화 (1d, 선택)**
- 태스크: 규칙의 `shrimp` 워크플로우를 `cli.py task` DB로 대체. shrimp 유지 시에도 **SSOT는 tasks DB**로 명시.
- 계획: `yggdrasil` 대체 여부 결정 — 기본은 **유지**(플랜 파일/아카이브 툴링 가치), 대체 시 `cli.py plan`(docs/plans + DB)로 이관.
- Acceptance: 태스크가 `tasks` 단일 소스, 규칙 문서 갱신. (yggdrasil 유지 시 변경 없음)

**Phase 7 — 정리/검증 (0.5d)**
- 사용처 없음 확인 후 래퍼 스크립트/`mcp-trunc-proxy` 항목 정리(`safe_delete_symbol`/`grep` 근거).
- `pytest -x --tb=short` 및 CLI 스모크. Deep Dive 1회 실전 검증.
- Acceptance: §7 검증 기준 전부 통과.

## 6. 정확도 설계 (핵심)
1. **캐시(SSOT)**: `research_cache`에 (query, provider) 해시 저장 → 재현성, 중복 API 호출 제거, 세션 간 공유.
2. **리랭크**: web+exa 후보를 병합 후 `reranker_score(query, doc)`로 재정렬(기존 Qwen3-Reranker-4B). 근거: 리랭크 +25~40% 정확도.
3. **인용 정규화**: 모든 결과에 `{title,url,source,fetched_at}` 부착 → Deep Dive 검증(출처 추적)에 사용.
4. **프로비넌스**: 최종 결과를 `obs_write`/worklog에 요약 기록 → 감사 추적.

## 7. 검증 기준 (Acceptance 종합)
- `cli.py research search "…" --json` 정상, 필수 필드(title/url/snippet/source/score) 포함.
- 반복 쿼리 캐시 히트(API 미호출) 확인.
- 리랭크 on/off 시 상위 정렬 차이 확인.
- MCP 툴 스키마 토큰 **감소** 측정(Phase 0 대비).
- 기존 MCP 호환(전환기) 또는 제거 후 세션 정상.
- Deep Dive 4단계가 CLI로 완료되고 출처 인용 포함.

## 8. 롤백
- 전환기: 각 MCP를 `enabled:false`로만 두고 CLI 병행 → 문제 시 재활성.
- 데이터: `research_cache`는 신규 테이블(기존 영향 없음) → `DROP` 가능.
- 코드: Phase 1은 "이동 + 얇은 래퍼"라 원복 시 래퍼만 원본 호출로 되돌림.
- env 토글: `RESEARCH_BACKEND=cli|mcp`(기본 `cli`, 문제 시 `mcp`).

## 9. 미결 / 추후 결정
- yggdrasil 유지 vs `cli.py plan` 대체 (Phase 6에서 결정).
- shrimp 완전 제거 vs 규칙만 tasks DB로 정렬.
- devforge-mcp에 `research` 툴 포함 여부(CLI-only 대비 스키마 비용).
- `fetch` 대체: `flaresolverr_bypass`(이미 MCP) 재사용 vs `lib/research/fetch.py` 신규.

## 10. 범위 밖 (Out of scope)
- LSP 통합/축소(유지).
- watchdog/컴포넌트 레지스트리(`docs/reports/control-plane-registry-research.md`) — 별도 계획.
- 외부 검색 벤더 교체.
