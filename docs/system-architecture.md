# DevForge 시스템 전체 구조

> ebook 파이프라인 + watchdog + 데이터 흐름의 통합 구조 문서.
> 최종 갱신: 2026-09-09

---

## 1. 시스템 개요

**DevForge** 서버(Oracle Cloud, 한국 리전)에서 운영되는 웹소설 수집·변환·감시 통합 시스템.

```
[사용자 브라우저] ── HTTPS ──▶ [Vercel CDN] ── 프록시 ──▶ [DevForge 서버]
                                     │                        │
                            Next.js (miniebook.vercel.app)   FastAPI (:8089)
                                     │                        │
                                     └───── 데이터 ──────────▶ 로컬 JSON DB
```

---

## 2. 전체 아키텍처 다이어그램

```
┌─────────────────────────────────────────────────────────────────────┐
│                        사용자 브라우저                                 │
│            https://miniebook.vercel.app (Vercel CDN)                │
│  - 라이브러리 / 소설 상세 / 회차 읽기 / EPUB 다운로드 / Admin          │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ HTTPS (Vercel catch-all 프록시)
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Vercel CDN (Next.js ISR)                         │
│  /            → 정적 HTML (5분 ISR)                                 │
│  /novel/[id]  → 소설 상세 + 회차 (ISR)                              │
│  /admin       → 파이프라인 관리 (URL 입력 → discover)               │
│  /api/*       → catch-all 프록시 → devforge FastAPI                 │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ HTTPS (Caddy → nip.io)
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    DevForge 서버 (Oracle Cloud)                      │
│                                                                     │
│  [Caddy] reverse proxy                                              │
│   └─ FastAPI (:8089) — ebook 백엔드                                │
│       ├─ routers/pipeline.py   : /api/pipeline/start, /status       │
│       ├─ routers/novels.py     : /api/novels/*                      │
│       ├─ routers/chapters.py   : /api/chapters/{wr_id}              │
│       └─ routers/metadata.py   : /api/metadata/*                    │
│                                                                     │
│  [ebook-watcher.service]  (systemd, Type=notify)                    │
│   └─ pipeline.py loop --source bookto31  (5분 간격)                 │
│                                                                     │
│  [devforge-watchdog.service]  (60초 루프)                           │
│   └─ lib/watchdog/ — 서비스/타이머/컨테이너/heartbeat 감시          │
│                                                                     │
│  [FlareSolverr] (:8191) — Cloudflare 우회 (bookto31)               │
│  [Playwright] — toki31 AES-GCM 복호화                               │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ 파일 읽기/쓰기
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│              로컬 데이터 스토리지 (/opt/ai_data/)                    │
│  /opt/ai_data/flaresolverr/novels/{소설명}/                         │
│      ├── meta.json              (소설 메타데이터)                    │
│      ├── {wr_id}.json           (챕터별 본문)                        │
│      └── _chapters_index.json   (회차 인덱스 캐시)                   │
│  /opt/ai_data/flaresolverr/ebook_watcher/                           │
│      ├── queue.json             (수집 큐 — fcntl 락 보호)            │
│      ├── failed.json            (DLQ — 실패 챕터 보존)               │
│      ├── status.json            (진행 상황)                          │
│      └── *.lock                 (동시성 락 파일)                     │
│  /opt/ai_data/scripts/watchdog_state.json (watchdog 상태 영속화)     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. ebook 파이프라인 구조

### 3.1 파이프라인 단계

```
[1] discover ──▶ [2] collect ──▶ [3] enrich ──▶ [4] index ──▶ [5] revalidate
  회차 목록        source별 fetch     namu.wiki      인덱스 재구축     Vercel ISR
  (epage+spage)    → JSON 저장        메타데이터                     캐시 갱신
```

### 3.2 실행 경로

| 경로 | 트리거 | 설명 |
|------|--------|------|
| **Admin URL 제출** | `/api/pipeline/start` | URL → source/ID 분기 → discover(전체 회차) → loop 시작 |
| **상시 루프** | `ebook-watcher.service` | 5분 간격 collect → index → revalidate |
| **월 1회 자동 discover** | `loop` 내 날짜 체크 | 매월 1일 연재작 새 회차 감지 (max_pages 200) |

### 3.3 수집기 (source 분기)

| source | collector | 방식 | 속도 |
|--------|-----------|------|------|
| `bookto31` | `_collect_bookto31` | FlareSolverr + HTML 파싱 | 적응형 (최소 5분) |
| `toki31`/`newtoki` | `_collect_newtoki` | Playwright + AES-GCM | 적응형 (5~60초) |

### 3.4 안전 장치

| 장치 | 설명 |
|------|------|
| **systemd WatchdogSec(600s)** | loop이 5분마다 sd_notify → 10분 미수신 시 hang 판정 |
| **queue 파일 락** | `queue.lock` + `queue.collect.lock` 분리 (flock 무력화 방지) |
| **atomic write** | tmp 파일 + os.replace → JSON 손상 불가 |
| **DLQ** | 3회 실패 → failed.json 보존 |
| **적응형 딜레이** | 10×fetch 시간 (bookto31 최소 5분 / toki31 5~60초) |
| **월 1회 discover** | 연재작 전체 회차 정확히 확인 (max_pages 200) |

---

## 4. watchdog 구조

### 4.1 계층 구조

```
devforge-watchdog.service (60초 루프)
  └─ lib/watchdog/
      ├─ orchestrator.py   : main_loop, 복구 조율
      ├─ checker.py        : check_service / check_ebook_pipeline / check_timer
      ├─ recovery.py       : recover_service / recover_ebook_watcher / graduated_recover
      ├─ state.py          : WatchdogState (영속화) / ComponentTracker (backoff+circuit)
      ├─ config.py         : SERVICE_TARGETS / TIMER_TARGETS / BACKOFF_SCHEDULE
      ├─ messenger.py      : heartbeat (PostgreSQL 기반 dead-man's switch)
      └─ notifier.py       : Slack/Opsgenie 알림
```

### 4.2 감시 대상

| 대상 | 체크 방식 | 복구 |
|------|-----------|------|
| `devforge-turn-watcher` | svc_active | recover_service |
| `openrouter-rr-proxy` | svc_active | recover_service |
| `devforge-day-cycle` | svc_active | recover_service |
| `ebook-watcher` | **check_ebook_pipeline** (프로세스+로그활동) | **recover_ebook_watcher** (readiness) |
| 컨테이너 5종 | podman ps | alert-only (재시작 금지) |
| 타이머 7종 | LastTrigger idle | kick |

### 4.3 이중 감시 구조 (ebook-watcher)

```
1차: systemd WatchdogSec (Type=notify)
  loop이 5분마다 WATCHDOG=1 → 10분 내 미수신 → on-watchdog 재시작

2차: devforge-watchdog (60초)
  check_ebook_pipeline → 프로세스 존재 + 로그 활동(20분) → recover_ebook_watcher
```

### 4.4 복구 로직 (graduated_recover)

```
실패 감지 → backoff 대기 (0→10→20→40→80→120→300, ±10% jitter)
         → recover (restart)
         → readiness 확인 (ebook은 프로세스+로그활동)
         → circuit breaker (3회 연속 실패 → OPEN 120초 → HALF_OPEN)
```

### 4.5 상태 영속화

- 상태 파일: `/opt/ai_data/scripts/watchdog_state.json` (5분 주기 저장)
- watchdog 재시작 시 `load_state()`로 backoff/circuit breaker 보존
- **효과**: restart storm 방지

---

## 5. 데이터 흐름 (챕터 기준)

```
1. discover (URL 제출 or 월 1회)
   → 전체 회차 wr_id 추출 → queue.json 등록 (락 보호)
2. loop collect (5분 간격)
   → queue에서 1개 fetch (source별 collector)
   → JSON 저장 (novels/{소설}/{wr_id}.json)
   → 실패 시 3회 재시도 → DLQ(failed.json)
3. index → _chapters_index.json 재구축
4. revalidate → Vercel ISR 캐시 갱신
5. 사용자 열람
   → /api/novels/{id}/chapters (인덱스 캐시) → CDN
```

---

## 6. 현재 데이터 현황 (2026-09-09)

| 작품 | 소스 | 저장 회차 | 상태 |
|------|------|----------|------|
| 아포칼립스의 고인물 | toki31 | 287 | 수집 완료 |
| 하남자의 탑 공략법 | bookto31 | 557 | 완결 |
| 오늘만 사는 기사 | bookto31 | 363 | 연재 중 |
| 화산귀환 | bookto31 | 34 | 수집 중 (queue 1888) |
| 게임 속 바바리안으로 살아남기 | bookto31 | 31 | 수집 중 (queue 210) |

---

## 7. 관련 문서

| 문서 | 위치 | 내용 |
|------|------|------|
| ebook 아키텍처 | `/opt/workspace/ebooklib/docs/00-ARCHITECTURE.md` | ebook 파이프라인 상세 |
| 데이터 파이프라인 | `/opt/workspace/ebooklib/docs/01-DATA-PIPELINE.md` | 데이터 흐름 |
| 자동화 시스템 | `/opt/workspace/ebooklib/docs/07-AUTOMATION.md` | systemd + watchdog |
| 유지보수 | `/opt/workspace/ebooklib/docs/06-MAINTENANCE.md` | 운영 가이드 |
| watchdog 종합 감사 | `/opt/projects/server/docs/watchdog-comprehensive-audit.md` | watchdog 패치 이력 |