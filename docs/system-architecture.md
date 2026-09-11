# DevForge 시스템 전체 구조

> 서버 전체 런타임·데이터 흐름·스토리지의 통합 구조 문서.
> 최종 갱신: 2026-09-11 (이전 2026-09-09 버전을 전면 갱신 — OCI 스토리지 계층 추가)
> 자동 생성 문서(`docs/architecture/*`)와 달리 이 문서는 **수동 관리**다.

---

## 1. 시스템 개요

**DevForge**는 Oracle Cloud(OCI, `ap-tokyo-1`, ARM Ampere A1) 단일 서버에서 운영되는
LLM 추론 + 파이프라인 + 웹앱 + 파일 교환 통합 시스템이다.

- Host: DEVFORGE (ARM Neoverse-N1 4-core, 22Gi + zram + swap)
- OS: Oracle Linux Server 9.7 (aarch64)
- Runtime: Podman (rootless, user `opc`) / Caddy(rootful, host network)
- DB: PostgreSQL 16 (pod `svc`, `devforge_app`)
- Entry point: `CLAUDE.yaml` → `cli.py status --json`(라이브 상태) + `devforge_app.worklog_entries`(DB)

---

## 2. 계층 구조

```
[사용자 / 외부]
   │ HTTPS
   ▼
┌──────────────────────────────────────────────────────────────┐
│ EDGE — Caddy (rootful, host net, /etc/caddy/Caddyfile)         │
│  /docs/* · /cashbook/* · /news/* · /api/* (ebook)              │
│  /send*, /receive* (파일 교환) · /devforge/tg-webhook*          │
│  /netdata* · (legacy) /webhooks/slack* · /slack/actions*        │
└───────────────┬──────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────┐
│ APPS                                                          │
│  FastAPI hub :8002  (Slack/Telegram/email + MCP mount)         │
│    └─ Blob Explorer :8085 (OCI 백엔드 + Droplr, /send·/receive)    │
│  MCP server  :8000  (FastMCP Streamable HTTP)                  │
│  ebook-api   :8089  · cashbook :8100 · news :8091              │
│  tg_webhook  :8001  · review_dashboard :9002                   │
│  proxies: anthropic :44777 · anthropic_openrouter :44778        │
│           gemini_openai :4431 · openrouter_rr :8451            │
│           or_rate_limiter :4311                                │
└───────────────┬──────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────┐
│ CORE — Podman pods                                            │
│  pod svc    : postgres :5432 · devforge-worker · flaresolverr  │
│               · devforge-mcp · (devforge-inference, 동적)       │
│  pod data   : data-pod-infra (legacy)                          │
│  systemd --user services/timers (watchdog, day-cycle, backup…) │
└───────────────┬──────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────┐
│ STORAGE                                                       │
│  LVM: /opt/ai_data(100G) · /mnt/lv_db(30G) · /opt/projects(10G)│
│       · /opt/workspace(6G) · /(44.5G)                          │
│  DB  : postgres data → /mnt/lv_db (bind)                       │
│  Files: /opt/ai_data (models, novels, search.db, backups)      │
│  Remote: OCI Object Storage (backups + file exchange)          │
│  External: Azure Blob(현행 교환) · Droplr(단축) · Notion(메모)   │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. 런타임 인벤토리

### 3.1 컨테이너 (Podman)

| Name | Pod | Purpose |
|---|---|---|
| `postgres` | svc | PostgreSQL 16, `devforge_app` (data bind `/mnt/lv_db`) |
| `devforge-worker` | svc | `worker_supervisor.py` — raw_consumer Pass 2/3 |
| `flaresolverr` | svc | Cloudflare 우회 (:8191/:8192), ebook bookto31 |
| `devforge-mcp` | svc | FastMCP 서버 (:8000), `mcp_server.py` |
| `devforge-inference` | (동적) | llama.cpp server :8080-8084 (mode별 모델) |
| `data-pod-infra` | data-pod | legacy pod infra |
| `caddy` (rootful) | — | reverse proxy, host net |

> 서비스 발견/상태는 `cli.py status --json`이 SSOT. 컨테이너는 quadlet
> (`~/.config/containers/systemd/`)로 관리.

### 3.2 systemd --user 서비스 (대표)

| 서비스 | 실행 | 역할 |
|---|---|---|
| `devforge-watchdog` | `scripts/watchdog.py` | 서비스/타이머/컨테이너/디스크 감시·복구 |
| `devforge-turn-watcher` | `scripts/turn_watcher.py` | 대화 로그 → turns(raw) 수집 |
| `devforge-day-cycle` | `scripts/day_cycle.sh` | 일일 파이프라인 체인 |
| `devforge-system-sync` | `scripts/system_sync.sh` | 아키텍처 문서 갱신 + DuckDNS + autocommit |
| `devforge-backup` | `scripts/osync_backup.py all` | OCI 백업(DB+앱) |
| `devforge-restore-test` | `scripts/osync_restore_test.py` | 월간 복원 검증 |
| `ebook-watcher` / `ebook-api` | ebooklib | ebook 수집/서빙 |
| `devforge-news*` | `/opt/workspace/news/*` | 뉴스 수집/digest/API |
| `cashbook` | uvicorn:8100 | 가계부 웹앱 |
| `anthropic-proxy` 등 | `scripts/proxies/*` | LLM API 호환 프록시 |
| `golden-image-*` | `scripts/golden_image/*` | 골든 이미지 배포/연간 점검 |

### 3.3 타이머 (요약)

- 매일: `backup-safety`(23:00 UTC), `daily-structure`(00:00), `activity-summarizer`(00:00), `news-digest`(23:30)
- 주기: `system-sync`(30분), `dev-poll`(10분), `golden-image-deploy-check`(15분), `workspace-autocommit`(30분)
- 주/월: `weekly-enrich-rebuild`(일 18:00), `restore-test`(1일 20:30), `reference-monitor`(월 16:00)
- 전체 목록: `systemctl --user list-timers` / `docs/specs/timer-registry.yaml`

### 3.4 Caddy 라우트 (live `/etc/caddy/Caddyfile`)

| Path | Backend |
|---|---|
| `/docs/*` | file_server `/data/docs` |
| `/send*`, `/receive*` | `127.0.0.1:8085` (Blob Explorer) |
| `/presign*` | `127.0.0.1:8085` (write-PAR 발급) |
| `/calendar*` | `127.0.0.1:8002` (calendar_sync) |
| `/auth/google/callback*` | `127.0.0.1:8002` (rewrite → `/calendar/auth/google/callback`) |
| `/api/*` | `host.containers.internal:8089` (ebook) |
| `/news/*` | `127.0.0.1:8091` |
| `/cashbook/*` | `host.containers.internal:8100` |
| `/devforge/tg-webhook*` | `127.0.0.1:8001` |
| `/netdata*` | `10.89.0.1:19999` (basic auth) |
| `/webhooks/slack*`, `/slack/actions*` | `127.0.0.1:8084`/`:8087` (legacy, 리스너 없음) |

---

## 4. 데이터 흐름

### 4.1 LLM 추론
`scripts/lib/pod_manager/`가 mode+model을 `current-mode-inference.env`에 쓰고
`llama.cpp:server` 컨테이너 `devforge-inference`를 띄운다.
포트: 8080 reranker · 8081 embed · 8082 extract/enrich · 8083 verify · 8084 27B verifier.

### 4.2 turn / observation 파이프라인
```
turn_watcher → turns(raw) → raw_consumer(worker) → pending
  → day_cycle: batching → cleaned → scanned → extracted+verified → enriched → embedded
```
결과는 DB(`turns`, `review_facts`, `observations`, `embeddings`)에 저장되고
MCP(`fact_*`, `obs_*`, `search_*`, `mem_*`)로 노출된다.

### 4.3 ebook 파이프라인
`ebook-watcher`(5분 loop): discover → collect(FlareSolverr/Playwright) → enrich → index → revalidate.
`ebook-api`(:8089)가 서빙, Caddy `/api/*` 경유.

### 4.4 watchdog
`devforge-watchdog`(60초) → 서비스/타이머/컨테이너/디스크/heartbeat 감시 →
`graduated_recover`(backoff + circuit breaker) + Slack/Opsgenie 알림.
- 추가 감시(2026-09-11): 컨테이너 `devforge-fastapi`/`devforge-worker`(alert-only), 타이머 `devforge-backup-safety`, **one-shot 결과**(`ActiveState/Result`: daily-structure·backup·restore-test·system-sync, alert-only).

### 4.5 알림
`scripts/lib/notify.py Notifier` — Apprise(Telegram + Gmail SMTP) + Slack.
FastAPI hub, `telegram_send`, `mcp_server.py`에서 사용.

### 4.6 백업 / 복원
`devforge-backup`(DB daily + app weekly) → OCI `devforge-standard/backups/`.
`devforge-restore-test`(월간) → scratch DB 복원 검증. 상세: [object-storage.md](./object-storage.md).

---

## 5. 스토리지 계층

| 위치 | 크기 | 내용 |
|---|---|---|
| `/opt/ai_data` | 100G | models/gguf, flaresolverr(novels/epub), search.db, backups(스테이징), containers |
| `/mnt/lv_db` | 30G | PostgreSQL data (bind) |
| `/opt/projects` | 10G | server repo |
| `/opt/workspace` | 6G | ebooklib, news, common-lib |
| `/` | 44.5G | OS |

- 원격: **OCI Object Storage** (`devforge-standard`, `devforge-archive`).
- 파일 교환: OCI Object Storage (`uploads/*`, PAR → Droplr). 파이프라인 산출물은 Azure Blob(현행).
- 단축: **Droplr** (`drplr` CLI).

---

## 6. 외부 연동

| 대상 | 용도 | 코드 |
|---|---|---|
| OCI Object Storage | 백업 + 파일 교환 | `scripts/osync_backup.py`, `lib/oci_storage.py`, `blob_explorer/` |
| Azure Blob | (제거됨 2026-09-11) | 이관 완료 → OCI + Droplr |
| Droplr | 최종 단축 주소 | `lib/droplr.py` (HTTP API), `scripts/droplr_upload.py` |
| Notion | 메모/리뷰 기록 | `lib/notion_client.py` |
| Slack/Telegram/Gmail | 알림 | `lib/notify.py` |

---

## 7. 문서 생성 파이프라인

- 생성기: `scripts/gen_architecture.py` (입력: `collect_structural()` 라이브 + `CLAUDE.yaml` 정적)
- 산출물(자동, 수동 편집 금지):
  - `docs/architecture/infrastructure.md` (서버 정체성)
  - `docs/architecture/software.yaml` (모델/모드/파이프라인)
  - `docs/architecture/code-structure.yaml` (파일 레이아웃 SSOT, 30분 hash-guard)
  - `docs/specs/timer-registry.yaml` (타이머)
- 실행: `devforge-system-sync.timer`(30분, `--check-structure`) + `devforge-daily-structure.timer`(매일 전체 + git push)
- 수동 편집 문서: 이 문서, `docs/object-storage.md`, 각종 design/audit/runbook

---

## 8. 알려진 이슈 / 불일치 (2026-09-11 조사)

| 항목 | 상태 | 설명 |
|---|---|---|
| `container-devforge-fastapi` | ✅ resolved (2026-09-11) | 이미지에 `jinja2`+`oci` 추가, `calendar_sync`는 선택적 import로 변경(google-* 없어도 허브 정상). :8002/:8085 정상, `svc.pod`가 8085 publish |
| `calendar_sync` (Google) | ✅ resolved (2026-09-11) | 이미지 deps(google/pandas)+import 수정+`TemplateResponse` 호환+pod 8002 publish+Caddy `/calendar`,`/auth/google/callback` 라우트. redirect_uri가 Google 등록값과 일치 |
| Caddy 사용자 사본 | 🟡 stale | `/home/opc/.config/caddy/Caddyfile`는 옛 버전. live는 `/etc/caddy/Caddyfile`(rootful) |
| `container-devforge-caddy` | 🔴 failed | quadlet 사용 안 함(실제는 rootful `caddy.service`) |
| `devforge-worker` | ✅ resolved (2026-09-11) | `worker_supervisor.py`를 `_archive/`에서 복원 → 정상 기동(Pass 2 raw→pending 동작) |
| `devforge-nli`, `gemini-proxy` | 🔴 | ExecStart 대상 파일이 worktree에 없음 |
| `devforge-daily-structure` | 🔴 failed | 문서 생성 + git push 실패 → `software.yaml`(2026-07-27) stale |
| `CLAUDE.yaml#storage` | 🟡 | 옛 LV(`lv_logs`/`lv_meta`/`lv_tmp`) 표기 — 실제 LVM과 불일치 |
| legacy backup | 🟡 | `/usr/local/bin/dump_postgres.sh`(→`/mnt/secure_meta`)는 폐기, osync가 대체 |

---

## 9. 관련 문서

| 문서 | 경로 | 내용 |
|---|---|---|
| OCI 스토리지/파일교환 | `docs/object-storage.md` | 버킷·백업·PAR·Droplr·통합 이점 |
| 서버 정체성(자동) | `docs/architecture/infrastructure.md` | 라이브 상태 |
| 코드 구조 SSOT | `docs/architecture/code-structure.yaml` | 파일 레이아웃 |
| watchdog 감사 | `docs/watchdog-comprehensive-audit.md` | watchdog 패치 이력 |
| golden image runbook | `docs/runbook-golden-image.md` | 이미지 배포 |
| ebook 아키텍처 | `/opt/workspace/ebooklib/docs/00-ARCHITECTURE.md` | ebook 상세 |
| 통합 제어 | `CLAUDE.yaml` | 진입점/엔트리포인트 목록 |

### 이전 산출물 참고
- `_archive/server-specs-and-llm-architecture.md` (2026-05-25) — 구 아키텍처
- `docs/_archive/specs/system-design.yaml` (2026-06-06) — 구 시스템 설계
- `scripts/blob_explorer.py` — 현 `blob_explorer/` 패키지의 전신(현재 git history에만 존재)
