# DevForge 시스템 전체 구조

> Status: active · Date: 2026-09-23 · Owner: devforge · Related: `docs/ARCHITECTURE.md`, `docs/REFACTORING_PLAN.md`
> 서버 전체 런타임·데이터 흐름·스토리지의 통합 구조 문서.
> 최종 갱신: 2026-09-23 (root offload bind 14 + SELinux restorecon + journald 상한·SystemKeepFree + 일일 정리 타이머 반영)
> 자동 생성 문서(`docs/architecture/*`)와 달리 이 문서는 **수동 관리**다.

---

## 1. 시스템 개요

**DevForge**는 Oracle Cloud(OCI, `ap-chuncheon-1`, ARM Ampere A1) 단일 서버에서 운영되는
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
│  MCP server  :8000  (리팩터드 SSE — devforge.adapters.driving.mcp.server) │
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
| `devforge-mcp` | svc | MCP 서버 (:8000), `devforge.adapters.driving.mcp.server` (리팩터드; `scripts/mcp_server.py`는 비활성 레거시) |
| `devforge-inference` | (동적) | llama.cpp server :8080-8084 (mode별 모델) |
| `data-pod-infra` | data-pod | legacy pod infra |
| `caddy` (rootful) | — | reverse proxy, host net |

> 서비스 발견/상태는 `cli.py status --json`이 SSOT. 컨테이너는 quadlet
> (`~/.config/containers/systemd/`)로 관리. 비활성 quadlet은 반드시
> `*.container.disabled`로 rename(주석 stub을 `.container`로 남기면 generator 실패).
> rootless bridge의 발행 포트는 `rootlessport`(userspace proxy)가 포워딩 —
> 컨테이너 healthcheck와 별개로 **호스트 도달성**을 watchdog이 감시(§4.4, 2026-09-12).
>
> **DSN 주입(2026-09-23)**: `devforge-mcp`·`devforge-watchdog-v2`는
> `ExecStartPre`(`kv-export-env.sh`)로 KV `DEVFORGE-DATABASE-URL`(양쪽 볼트 등록)을
> `EnvironmentFile`에 넣는다. `devforge-fastapi`는 entrypoint(`kv-fetch-env.py`) 폴백
> (PASSWORD→DSN). `devforge-worker`는 KV 미전환(환경변수 미사용). MCP 재시작 전
> `/opt/ai_data/pip-cache`에 cp312 aarch64 wheel 존재 확인 — 캐시 비면 `import typer`
> 실패 crash loop(§OPERATIONS_GUIDE DSN 주입 참조).
>
> **코드 레이어 주의(2026-09-14)**: 위 서비스/컨테이너의 `ExecStart`는 아직 레거시
> `scripts/*`를 가리킨다. 리팩토링 최종본은 `src/devforge/` 패키지(§3.5)이며
> **컷오버는 미완료**다 — `devforge` CLI는 설치·동작하지만 라이브 서비스는 미사용.

### 3.2 systemd --user 서비스 (대표)

| 서비스 | 실행 | 역할 |
|---|---|---|
| `devforge-watchdog` | `scripts/watchdog.py` | 서비스/타이머/컨테이너/디스크 감시·복구 |
| `devforge-turn-watcher` | `scripts/turn_watcher.py` | 대화 로그 → turns(raw) 수집 |
| `devforge-day-cycle` | `scripts/day_cycle.sh` | 일일 파이프라인 체인 (oneshot; 타이머 없음 — watchdog `orchestrator.py`가 pending 턴 존재 시 `start`로 기동) |
| `devforge-system-sync` | `scripts/system_sync.sh` | DuckDNS + autocommit (문서생성 은퇴 2026-09-14) |
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
| `/api/portal/*` | `127.0.0.1:8002` (portal read API — `/api/*`보다 우선) |
| `/api/news/*` | `127.0.0.1:8091` (rewrite: `/api` strip → news API) |
| `/api/*` | `host.containers.internal:8089` (ebook) |
| `/news/*` | `127.0.0.1:8091` |
| `/cashbook/*` | `host.containers.internal:8100` |
| `/devforge/tg-webhook*` | `127.0.0.1:8001` |
| `/netdata*` | `10.89.0.1:19999` (basic auth) |
| `/webhooks/slack*`, `/slack/actions*` | `127.0.0.1:8084`/`:8087` (legacy, 리스너 없음) |

### 3.5 코드 레이어 — `devforge` 패키지 (리팩토링 최종본)

`pip install -e .`로 설치되는 src-layout 패키지. Ports & Adapters 구조이며 정본은
[`docs/REFACTORING_PLAN.md`](./REFACTORING_PLAN.md)(v1.4) + [`docs/adr/`](./adr/)다.

| 계층 | 경로 | 역할 |
|---|---|---|
| CLI (진입점) | `src/devforge/cli.py` | `devforge` Typer 단일 진입점 (`pyproject.toml [project.scripts]`) |
| core | `src/devforge/core/{config,logging}.py` | `ConfigRegistry`(5개 소스 통합), 구조화 로깅 |
| ports | `src/devforge/ports/extract.py` | `LLMPort`/`ExtractPort`/`TurnRepository` (Protocol) |
| domain | `src/devforge/domain/models.py` | SQLAlchemy 모델 (`docs/specs/schema.sql` 대응) |
| adapters/driven | `.../llm/local_adapter.py`, `.../storage/*` | 로컬 llama.cpp(:8080-8085), PostgreSQL(`DatabaseGateway`) |
| adapters/driving | `.../{api,mcp,cli_cmds}/` | FastAPI(:8000), MCP SSE(:8100), CLI 서브커맨드 |
| application | `src/devforge/application/extract_pipeline.py` | extract 파이프라인 서비스 |

주요 명령(정본: [`docs/API_REFERENCE.md`](./API_REFERENCE.md)): `devforge status [--json]`,
`devforge pipeline orchestrate|status`, `devforge mcp serve`, `devforge inference switch|status|ensure`.

> **컷오버 전**: systemd 유닛/Quadlet은 레거시 `scripts/`(§3.2)를 계속 사용하며,
> `devforge` CLI·패키지는 **병행 설치만** 되어 있다. 운영 절차는
> [`docs/OPERATIONS_GUIDE.md`](./OPERATIONS_GUIDE.md) 참조.

---

## 4. 데이터 흐름

### 4.1 LLM 추론
`scripts/lib/pod_manager/`가 mode+model을 `current-mode-inference.env`에 쓰고
`llama.cpp:server` 컨테이너 `devforge-inference`를 띄운다.
포트: 8080 reranker · 8081 embed · 8082 extract/enrich · 8083 verify · 8084 27B verifier.

**KV Cache 최적화 (2026-07-04)**: 모든 day-mode 모델은 `cache_type_k/v: q8_0` (quantized 8-bit)을 사용해 메모리 사용량을 50% 절감. 이를 통해 dual 8B 모델 동시 실행 시 안정성 확보. 자세한 내용은 `docs/adr/0007-kv-cache-optimization.md` 참조.

### 4.2 turn / observation 파이프라인
```
turn_watcher → turns(raw) → raw_consumer(worker) → pending
  → day_cycle: batching → cleaned → scanned → extracted+verified → enriched → embedded
```
결과는 DB(`turns`, `review_facts`, `observations`, `embeddings`)에 저장되고
MCP(`fact_*`, `obs_*`, `search_*`, `mem_*`)로 노출된다.

**수집 소스 (2026-09-19 기준)**:
- claude: `~/.claude/projects/-home-opc/*.jsonl` **단일 경로만** 수집.
  claude는 반드시 HOME(~)에서 실행할 것 — `/opt/workspace` 등 다른 cwd에서
  실행하면 `-opt-workspace` 같은 프로젝트 디렉토리가 생성되고 수집되지 않는다.
- opencode: `~/.local/share/opencode/opencode.db`
- copilot: `~/.copilot/session-state` (비용 소진으로 비활성)
- gemini/aider: 2026-09-19 폐기 (파서 삭제)

### 4.3 ebook 파이프라인
`ebook-watcher`(5분 loop): discover → collect(FlareSolverr/Playwright) → enrich → index → revalidate.
`ebook-api`(:8089)가 서빙, Caddy `/api/*` 경유.

### 4.4 watchdog
`devforge-watchdog`(60초) → 서비스/타이머/컨테이너/디스크/heartbeat 감시 →
`graduated_recover`(backoff + circuit breaker) + Slack/Opsgenie 알림.
- 추가 감시(2026-09-11): 컨테이너 `devforge-fastapi`/`devforge-worker`(**자동 재시작**), 타이머 `devforge-backup-safety`(kick), **one-shot 결과**(`ActiveState/Result`: daily-structure·backup·restore-test·system-sync, 실패 시 **자동 재실행** + backoff/circuit, 반복 실패 시에만 알림).
- **incident 기록(2026-09-11)**: 감지 시 **재시작 전** 로그/상태 캡처(시크릿 마스킹·8KB) → 조치 → `watchdog_incidents` 테이블에 감사 기록(open/resolved, dedup_key, fail/reopen 카운트). 7일 내 3회+ 반복 → DB `tasks`에 수정 티켓 자동 생성. 조회 `cli.py watch incidents [--open]·watch incident <id>`. 보존: events 90일 / incidents 180일.
- 감시 확장(2026-09-11 후속): 웹앱 `ebook-api`/`devforge-news-api`/`cashbook`(**자동 재시작**), **system 스코프** `caddy`/`netdata`(alert-only, rootful), 타이머 `dev-poll`/`news-digest`/`kuhwa-schedule`/`workspace-autopush` 추가. `ebook-watcher` `enable`(재부팅 생존). `system-sync` max_idle 1800→2700(30분 주기 경계 오탐 보정).
- **watchdog 자기 복구(2026-09-11)**: 유닛 `Type=notify` + `WatchdogSec=900` — 매 사이클 `sd_notify(WATCHDOG=1)`, **hang 시 systemd가 kill+restart**. `OnFailure=devforge-watchdog-failed.service` — 크래시루프(60s 내 5회) 시 **Slack 알림**. **dead-man's switch**: 매 사이클 `/var/tmp/watchdog_last_cycle_ts` 기록 + `devforge-watchdog-liveness.timer`(5분마다)가 stale(>900s) 시 알림. **외부 감시**: `WATCHDOG_PING_SSH=onmydoc`(secrets.env) → 5분마다 onmydoc(161.33.199.207)으로 SSH push(`~/wd_monitor/wdpulse.py record`). onmydoc의 `wd-check.timer`(5분)가 30분+ stale이면 **minipark4u@gmail.com 메일**(6h cooldown, 평소 무음). HTTP 방식 `WATCHDOG_PING_URL`도 지원.
- **incident → 자동 수정 루프(2026-09-11)**: 반복 incident(3회+/7일) → DB `tasks` + **GitHub Issue 자동 생성**(라벨 `watchdog,auto-safe`, 멱등) → `cli.py dev poll --auto-safe --claim`(dev-poll 타이머)이 claim → `lib/dev_pipeline`이 PR. (부수 수정: `poll_issues`가 gh의 `state="OPEN"`(대문자)을 소문자 비교로 모두 걸러내던 버그 → case-insensitive로 수정)
- **svc pod 포트포워딩 감시(2026-09-12)**: 컨테이너는 healthy여도 `rootlessport`(userspace proxy)가 죽으면 호스트 `127.0.0.1:8000/8002/8085/8191` 도달 불가 → devforge-mcp/FlareSolverr 불통. `check_svcpod_ports`(TCP connect)로 감지 후 `recover_svcpod_forwarding`(`svc-pod.service` 재기동, postgres 볼륨 유지)으로 자동 복구(60s 주기, backoff/circuit). task#32 · [`reports/svcpod-portforwarding-recovery-20260912.md`](./reports/svcpod-portforwarding-recovery-20260912.md).

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
| `/opt/ai_data` | 100G | models/gguf, flaresolverr(novels/epub), search.db, backups(스테이징), containers, **`system-savings/`** |
| `/mnt/lv_db` | 30G | PostgreSQL data (bind) |
| `/opt/projects` | 10G | server repo |
| `/opt/workspace` | 6G | ebooklib, news, common-lib |
| `/` (boot) | 45G | OS only — 대용량 캐시·넷데이터 등은 아래 bind로 offload |

- **root offload (2026-09-23)**: `/opt/ai_data/system-savings/` 아래로 데이터를 옮긴 뒤 `/etc/fstab` bind 14곳
  (`netdata`, `~/.cache`, `~/.npm`, `~/.rustup`, `~/.local/{bin,n,share/*,lib/*}`, `~/nltk_data`)으로 기존 경로를 유지.
  모두 `x-systemd.requires=opt-ai_data.mount` — 재부팅 시 `lv_ai_data` 마운트 후 자동 복원.
  이전 후 `restorecon -RF`로 SELinux 라벨 재부여 (오프로드 경로 `unlabeled_t` 방지).
- **일일 정리**: `root-volume-daily-clean.timer` (매일, `/usr/local/sbin/root-volume-daily-clean.sh`) —
  sandbox `/var/tmp` 잔재·dnf·pip·journal 200M·85% 이상이면 경고.
  journald: `SystemMaxUse=200M`, `SystemKeepFree=2G`, `MaxRetentionSec=2week` (`size.conf` 단일 SSOT).
- 주의: `opencode.db`(~2G)는 본 세션 종료 후 `opencode-db-offload.service`(재부팅 oneshot)로 `system-savings` 이전 대기 —
  이전 전까지 root에 잔존(진단: `findmnt /home/opc/.local/share/opencode`가 없으면 미이전).
  이전 스크립트는 `rsync -aX` 후 마운트 view에서 `restorecon -RF` 수행.

- 원격: **OCI Object Storage** (`devforge-standard`, 청주 `axgly0lmehyp`; `devforge-archive`는 미생성).
- 파일 교환: OCI Object Storage (`uploads/*`, `releases/*`, PAR → Droplr). 파이프라인 산출물도 OCI (Azure Blob은 2026-09-11 제거).
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
| Vercel (miniebook) | **최종 뷰어**(포털 홈/상태/뉴스/도서관) | `/opt/workspace/minihome/apps/ebooklib/apps/frontend` (Next.js, 서버측 `NEXT_PUBLIC_API_URL`=nip.io) |

> **뷰어 = 표시 전용(Vercel)**: 로직·데이터는 devforge에서 완성, Vercel은 ISR 캐시로 표시만. 파일 교환은 devforge(`/send`,`/receive`) 직접 — Vercel 미경유. (계획: `docs/plans/vercel-viewer-plan.md`)

---

## 7. 문서 생성 파이프라인

> **2026-09-14: 자동 생성기 은퇴.** `gen_architecture.py`가 실제로 신뢰할 수 없어 제거됨
> (`_archive/gen_architecture.py`). `docs/architecture/*`·`docs/specs/timer-registry.yaml`은
> **동결(frozen)/수동 관리**로 전환 — 코드 구조 SSOT는 `src/devforge/` + `docs/architecture/code-structure.yaml`(수동).
> `devforge-system-sync`/`daily-structure` 유닛은 문서생성 호출을 제거하고 DuckDNS + git autocommit/push만 유지한다.

- 수동 관리 문서: `docs/architecture/*`, `docs/specs/timer-registry.yaml`, 이 문서, `docs/object-storage.md`, 각종 design/audit/runbook
- 라이브 상태 정본: `scripts/cli.py status --json` (컨테이너/모델/타이머/서비스/resources)

---

## 8. 알려진 이슈 / 불일치 (2026-09-11 갱신)

| 항목 | 상태 | 설명 |
|---|---|---|
| `container-devforge-fastapi` | ✅ resolved | 이미지 `jinja2`+`oci`, `calendar_sync` 선택적 import. :8002/:8085 정상, `svc.pod` 8085 publish |
| `calendar_sync` (Google) | ✅ resolved | deps+import+`TemplateResponse` 호환+pod 8002+Caddy `/calendar`,`/auth/google/callback`. redirect_uri 등록값 일치 |
| `devforge-worker` | ✅ resolved | `worker_supervisor.py` 복원 → Pass 2 정상 |
| `devforge-nli`, `gemini-proxy` | ✅ 정리 | 비활성 + `_disabled/` 보관 |
| `devforge-daily-structure` | ✅ resolved(코드) | `gen_architecture` import 버그 수정 → 다음 00:00 UTC 실행 시 git push(그 전 미push backlog 자동 반영) |
| `CLAUDE.yaml#storage` | ✅ resolved | 실제 LVM으로 수정(root 44.5G / ai_data 100G / db 30G / projects 10G / swap 4G / workspace 6G) |
| watchdog 자기복구 | ✅ 강화 | `Type=notify`+`WatchdogSec`(hang) + `OnFailure`(크래시루프) + liveness 타이머 + 외부핑 (§4.4) |
| `container-devforge-caddy` (quadlet) | ✅ 정리 | 미사용 crash-loop → 비활성 + `_disabled/` 보관. live는 rootful `caddy.service`(reload 정상, exit 0) |
| Caddy 사용자 사본 | 🟢 표기 | `/home/opc/.config/caddy/Caddyfile`는 stale 표기(실제는 `/etc/caddy/Caddyfile`) |
| legacy backup | ℹ️ | `/usr/local/bin/dump_postgres.sh`(→`/mnt/secure_meta`) 폐기, osync가 대체 |

---

## 9. 관련 문서

| 문서 | 경로 | 내용 |
|---|---|---|
| OCI 스토리지/파일교환 | `docs/object-storage.md` | 버킷·백업·PAR·Droplr·통합 이점 |
| 서버 정체성(자동) | `docs/architecture/infrastructure.md` | 라이브 상태 |
| 코드 구조 SSOT | `docs/architecture/code-structure.yaml` | 파일 레이아웃 |
| watchdog 감사 | `docs/reports/watchdog-comprehensive-audit.md` | watchdog 패치 이력 |
| golden image runbook | `docs/runbooks/runbook-golden-image.md` | 이미지 배포 |
| ebook 아키텍처 | `/opt/workspace/minihome/apps/ebooklib/docs/00-ARCHITECTURE.md` | ebook 상세 |
| 통합 제어 | `CLAUDE.yaml` | 진입점/엔트리포인트 목록 |
| 리팩토링 계획(정본) | `docs/REFACTORING_PLAN.md` | src-layout + Ports&Adapters 전환 계획(v1.4) |
| 코드 아키텍처 | `docs/ARCHITECTURE.md` | 패키지 레이아웃·의존성 규칙(import-linter) |
| 온보딩/전환 | `docs/MIGRATION_GUIDE.md` | 설치·실행·레거시 매핑·트러블슈팅 |
| Track B 계획 | `docs/LLM_PROVIDER_PLAN.md` | 클라우드 LLM 공급자 추상화(proposed) |
| CLI/HTTP API | `docs/API_REFERENCE.md` | `devforge` 서브커맨드 + FastAPI/MCP 엔드포인트 |
| 운영 가이드 | `docs/OPERATIONS_GUIDE.md` | 설치·실행·마이그레이션·트러블슈팅 |
| 설계 결정 기록 | `docs/adr/` | config priority / LLM provider / shadow DB / Alembic |

### 이전 산출물 참고
- `_archive/server-specs-and-llm-architecture.md` (2026-05-25) — 구 아키텍처
- `docs/_archive/specs/system-design.yaml` (2026-06-06) — 구 시스템 설계
- `scripts/blob_explorer.py` — 현 `blob_explorer/` 패키지의 전신(현재 git history에만 존재)
