# Vercel 최종 뷰어 계획 v2 (devforge=로직, Vercel=표시)

> 작성: 2026-09-11 (v1 대체) · 전제: **도메인 없음**, 파일교환은 devforge 유지.
> 기반 로직: `ebooklib/apps/frontend` (miniebook, Vercel Next.js) — 이미 devforge 프록시/뷰어 동작 중.

---

## 0. 방향 정정 (v1 → v2)
- **파일교환은 Vercel 미사용.** 사용자↔devforge 서버 교환이고 단순 → **devforge의 Blob Explorer(OCI+PAR+Droplr) 유지**.
- **Vercel = 표시(viewer) 전용.** **모든 로직/데이터/API는 devforge에서 완성**하고, Vercel은 그것을 보여주기만.
- **중간 단계 최소화**(홉이 많을수록 오류↑): 뷰어 → devforge API 단일 경로.
- **도메인 불필요**: 뷰어 주소는 `*.vercel.app`(Vercel 제공), 백엔드는 `https://devforge.152-69-229-246.nip.io`(이미 Let's Encrypt TLS).

```
[브라우저] ──▶ Vercel(miniebook 뷰어) ──/api/* catch-all 프록시──▶ Caddy ──▶ devforge API
              (표시만)                                             (모든 로직·데이터)
[파일교환]   브라우저 ──▶ devforge(Caddy /send,/receive) ──▶ OCI PAR ──▶ Droplr   ← Vercel 무관
```

## 1. 재사용할 기존 로직 (그대로 기반)
`ebooklib/apps/frontend`:
- `app/api/[...slug]/route.ts` — **catch-all 프록시**: `/api/<path>` → `${NEXT_PUBLIC_API_URL}/api/<path>`. JSON은 JSON으로, EPUB/이미지 등 **바이너리는 스트리밍**, **CORS 헤더 포워딩**. (그대로 사용)
- `NEXT_PUBLIC_API_URL` 기본값 = `https://devforge.152-69-229-246.nip.io` → **도메인 없이 동작**.
- 페이지: `/`(도서관), `/novel/[id]`, `/admin`, `lib/api.ts`.
- 배포: Vercel(`vercel.json`, `framework: nextjs`).

→ **새 앱을 만들지 않고 이 프론트를 확장**한다(수정 기준).

## 2. devforge가 제공해야 하는 것 (로직 완성은 여기서)
뷰어는 **읽기 전용 JSON API**만 호출. 모두 `/api/*` 하위로 노출(Caddy 라우트).
- 기존: `/api/novels/*`, `/api/chapters/*`(ebook :8089), `/news/*`(:8091)
- **추가 필요(대시보드용)**: `scripts/devforge_fastapi/portal.py` → `/portal/*` (Caddy `/api/portal/*` rewrite 또는 `/portal*`)
  - `GET /portal/health` — 서버/컨테이너 상태
  - `GET /portal/incidents?open=1` — `watchdog_incidents`
  - `GET /portal/backups` — 최근 `backups/database/` 객체(OCI)
  - `GET /portal/timers` — 주요 systemd timer last/next
- 정적/이미지는 기존 프록시 바이너리 스트리밍으로 처리.
- **파일교환 API(`/send`,`/receive`,`/presign`)는 뷰어에서 사용하지 않음**(devforge 전용 유지).

## 3. Vercel(프론트) 작업 — 확장만 (범위 확정)
기존 `ebooklib/apps/frontend`에 추가/수정:
1. **통합 index** `/` — 포털 홈(도서관/상태/뉴스 카드 + 링크). 기존 도서관 홈을 포털로 개편.
2. **상태 대시보드** `/status` — `/api/portal/health|incidents|backups|timers` 표시.
3. **뉴스** `/news` — 뉴스 API(:8091)를 뷰어 경로로 노출해 목록 표시.
4. **도서관 확장** `/novel/[id]` 등 기존 뷰어 개선.
프록시(`app/api/[...slug]/route.ts`)·`NEXT_PUBLIC_API_URL`(nip.io)·배포 설정은 그대로 사용.

## 4. 인증 (최소)
- 뷰어는 **공개 읽기** 중심 → 특별 인증 없이 시작.
- 민감 데이터를 뷰어에 넣을 경우에만 `PORTAL_TOKEN`(devforge) + Vercel env로 보호. (파일교환은 뷰어에 없으므로 무관)

## 5. 단계 (각 단계 독립 배포·검증)
- **Phase 1 — devforge 읽기 API**: `devforge_fastapi/portal.py`(`/portal/health|incidents|backups|timers`) + Caddy 라우트.
  - Caddy: `/api/portal/* → 127.0.0.1:8002` (기존 `handle /api/* → :8089` **앞에** 배치).
  - 검증: `curl -H 'Host: …' https://…/api/portal/health` → 200.
- **Phase 2 — 뉴스 경로 정렬**: 뷰어 프록시(`/api/*`)로 뉴스를 받도록 Caddy에 `/api/news/* → :8091` 추가(경로 strip 조정). 검증: `/api/news/...` 200.
- **Phase 3 — frontend 포털**: 통합 index + `/status` + `/news` + 도서관 링크. 로컬(`next dev`, `NEXT_PUBLIC_API_URL=nip.io`) 검증 후 Vercel 배포.
- **Phase 4 — 폴리시/도메인**: 필요 시 `PORTAL_TOKEN`(읽기 보호), 도메인 생기면 Vercel 커스텀 도메인 연결.

## 6. 리스크 / 유의
- origin 도쿄 → 동적 API 지연(수십 ms)은 잔존(파일은 devforge 직접이라 무관).
- 뷰어는 **표시만** — 로직을 Vercel에 넣지 않는다(중간 단계 최소화 원칙).
- `NEXT_PUBLIC_API_URL`이 `.env.local`/Vercel env에 설정돼야 함(기본값 nip.io).

## 7. 결정 (확정)
- **뷰어 범위 = 통합 index + 상태 대시보드 + 뉴스 + 도서관 확장** (4종 전부).
- 인증: 공개 읽기 기본, 민감 항목만 `PORTAL_TOKEN`(후속).
- 도메인: 보류(생기면 Vercel 커스텀 도메인만 연결).

## 8. 진행 결과 (2026-09-11 완료)

**구현**
- Phase 1: devforge `devforge_fastapi/portal.py` `/api/portal/{health,summary,incidents,backups}` (+Caddy `/api/portal/*`) — summary가 최신 뉴스 3건 포함.
- Phase 2: Caddy `/api/news/* → :8091`(strip) — `/api/news/{health,dates,articles,stats}`.
- Phase 3: 프론트 `ebooklib/apps/frontend` — 포털 홈(`/`, A안) + `/status` + `/news`(+`/news/[date]` SSG) + `/library` 이동 + 상단 nav.
- Phase 4: **프로덕션 배포** → https://miniebook.vercel.app (Vercel `miniebook` 프로젝트; 배포는 레포 루트 `/opt/workspace/ebooklib`에서 `VERCEL_PROJECT_ID=miniebook` 오버라이드).

**반응속도 최적화 (웜 TTFB)**
- `/` 0.72–0.85 → **0.16–0.36s**, `/status` 0.62 → **0.15–0.35s**, `/news` 0.41–1.10 → **0.15s** (`/news` ISR + `/news/[date]` SSG + news_api 기본 최신날짜). 오리진 API 20–150ms.

**프론트 정리 (lint 0)**
- dead code 제거: `ChapterClient` `goPrev/goNext`, `lib/api.ts` 미사용 fetch 함수 6종·`fetchJson`·미사용 타입 → `lib/api.ts` 타입 전용.
- `setState-in-effect` 2건 해결(admin=`useSyncExternalStore` 외부 스토어, NovelClient=`ref+jumpNonce`), `<img>` 규칙 scoped disable (`next/image`는 임의 원격 호스트라 부적합).
- `eslint` **0 problems**. 파일교환은 devforge 유지(Vercel 미경유).
