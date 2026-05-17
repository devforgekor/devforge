# DevForge Server — 설계

**상태**: Phase 1 완료 (2026-05-14)  
**서버**: OCI ARM (24GB), Podman/Quadlet, PostgreSQL 16

## 핵심 결정

| # | 결정 | 사유 |
|---|------|------|
| 1 | Podman + Quadlet (user systemd) | rootless, docker 호환, 선언적 컨테이너 관리 |
| 2 | 이미지 태그 고정 (`:2026.05.14`) | 재현 가능한 배포, 롤백 용이 |
| 3 | PostgreSQL 16 (data-pod) + devforge_app | pg_trgm 검색, JSONB 메타, 단일 DB |
| 4 | MCP SSE (원격) | Claude Code Mac ↔ 서버 연동. stdio 불가 (원격) |
| 5 | 단일 비밀값 주입 (EnvironmentFile) | secrets.env 하나로 모든 컨테이너 비밀 관리 |
| 6 | AI 추론은 Mac이 담당 | 서버는 기록/검색만. LLM은 로컬 Qwen만 제공 |
| 7 | Caddy (root, host network) | Auto-HTTPS, reverse proxy 단일 진입점 |

## 인프라 구성

```
ai-pod (10.89.0.8, 16GB)     data-pod (10.89.0.9, 2GB)
├─ litellm :4000             └─ postgres :5432
├─ devforge-llm :8080           └─ /mnt/lv_db (30GB LVM)
└─ devforge-api :8000
   ├─ MCP SSE
   ├─ POST /ingest
   └─ GET /stats

Caddy (host network)
├─ /api/* → litellm:4000
├─ /devforge/* → localhost:8000
└─ netdata.* → localhost:19999
```

## 네트워크

- `devforge-net`: Podman bridge, 10.89.0.0/24, DNS enabled
- ai-pod: ports 4000, 8000 → 127.0.0.1 only
- data-pod: no published ports (pod 내부 통신)
- Caddy: host network, ports 80/443

## 접근 제한
- `/opt/workspace/` — 사용자 명시적 지시 없이 접근 금지 (Seedling 등 타 프로젝트 포함)

## 애플리케이션 구조

```
/opt/projects/server/
├── Dockerfile
├── requirements.txt
├── healthcheck.py
├── api/
│   ├── mcp_server.py       ← MCP SSE 서버
│   ├── ingest.py           ← POST /ingest
│   ├── search.py           ← 검색 + 저장 로직
│   ├── db.py               ← PostgreSQL pool + init
│   ├── stats.py            ← GET /stats (7섹션)
│   └── __init__.py
├── scripts/
│   └── cli.py              ← CLI (search/save/recent/worklog add/recent/search)
└── docs/
    └── schema.sql          ← DB 스키마 정본
```

## MCP 도구

| Tool | 입력 | 동작 |
|------|------|------|
| `mem_save` | tag, summary, detail | conversations + turns 저장 |
| `mem_search` | query, tag (optional) | ILIKE 검색, JSON 반환 |

## CLI

```bash
# 대화 검색/저장
python3 /opt/projects/server/scripts/cli.py search "쿼리"
python3 /opt/projects/server/scripts/cli.py save --source claude
python3 /opt/projects/server/scripts/cli.py recent

# 작업 기록 (worklog)
python3 /opt/projects/server/scripts/cli.py worklog add "제목" "요약" --tags "A,B"
python3 /opt/projects/server/scripts/cli.py worklog recent --limit 5
python cli.py worklog search "키워드" --tag "A"         # 작업 검색
```

## DB 스키마

정본: `schema.sql` (app container 내)  
대상 DB: `devforge_app` (PostgreSQL 16)

테이블: `conversations`, `turns`, `obs_dec`, `mcp_dec`, `observations`, `worklog_entries`  
인덱스: pg_trgm (검색), GIN (tags, meta), UNIQUE (date, title), UNIQUE partial (status='in_progress', dormant)  
worklog_entries 컬럼: status (dormant, always 'done'), kind (dormant, always 'task'), agent (AI 도구명), model (모델명), turn_ids (연결된 턴 UUID 배열)

## 백업

- `dump_postgres.sh`: daily pg_dump (devforge_app), 7일 retention
- `test_dump_restore.sh`: monthly restore validation
- `nightly_batch.sh`: daily batch pipeline (03:00 UTC) — light jobs first (retry 3x) → heavy jobs after
- 대상: `/mnt/secure_meta/snapshots/`

## 관련 문서

- 현황/계획: `docs/phases.md`
- 작업 추적: `docs/tasks.yaml` (todo / in_progress / blocked / done, Kanban labels: To Do / In Progress / Blocked / Done)
- 작업 이력: `devforge_app.worklog_entries` (PostgreSQL, cli.py worklog로 조회)
- 운영 설정: `/opt/projects/server/CLAUDE.yaml`, `handover.yaml`, `blueprint.yaml`
