# Mini News 세션 작업 요약

> 날짜: 2026-08-29
> 상태: 일부 완료, 날짜 표시 버그 미해결

---

## 1. 완료된 작업

### 1.1 브랜딩 변경

- "DevForge News" → "Mini News"
- `layout.tsx` 타이틀, 네비게이션, 로고 텍스트 일괄 변경
- 커밋: `dde50fe`

### 1.2 웹 UI 개선

| 항목 | 변경 내용 |
|------|-----------|
| 페이지네이션 | 날짜 기반 (page 1 = 오늘), 숫자 네비게이션 |
| 정렬 | 국내경제 > 해외경제 > 기술 > AI 카테고리 우선순위 |
| 요약 표시 | `line-clamp-2` 제거, 전체 요약 텍스트 표시 |
| 카테고리 | 카테고리 라벨 제거 |
| 타임존 | `AT TIME ZONE 'UTC'` 제거 (DB 저장값은 이미 `timestamptz`) |

### 1.3 자동 배포

- `collector.py`에 `_git_push()`, `_verify_deployment()` 함수 추가
- 수집 완료 후 자동 git push → Vercel 배포 트리거
- `devforge-news.timer`: 6시간마다 실행 (UTC 00, 06, 12, 18시)

### 1.4 스코어링 개선 (Phase 1-5)

| Phase | 내용 | 파일 | 상태 |
|-------|------|------|------|
| 1 | Circuit Breaker (threshold=2) | `collector.py` | ✅ 완료 |
| 2 | 포토뉴스 필터 (text < 50 chars skip) | `collector.py` | ✅ 완료 |
| 3 | TF-IDF 클러스터링 (`char_wb` + Jaccard fallback) | `dedup.py` | ✅ 완료 |
| 4 | Source Tier DB화 (`news_source_tiers`, 29개 소스) | `scoring.py` | ✅ 완료 |
| 5 | Corroboration score (최대 0.45 보너스) | `scoring.py` | ✅ 완료 |

- 테스트 28개 통과 (17개 기존 + 11개 신규)
- 커밋: `5214cfd`

### 1.5 kuhwa 수정

- NEIS API 에러 코드 체크 (`data?.RESULT?.CODE`) 추가
- `courses` 필드 추가 (calendar.js)
- iCalendar DESCRIPTION에 학교 레벨 정보 포함
- 커밋: `6ab50aa`

### 1.6 번역기 설계

- API 실패 시 `title_ko=""` 반환 (의도적)
- `needs_summary` 파이프라인 상태로 재시도 유도
- `translator.py` 테스트 수정 완료

---

## 2. 미해결 문제

### 2.1 날짜 표시 버그 (Blocking)

**증상**: 웹페이지가 "2026년 8월 28일 · 총 69건" 표시. DB에는 8/29 KST 기사 102건 존재.

**분석 과정**:

1. DB 쿼리 직접 실행 → `2026-08-29`가 첫 번째 반환 확인
2. `page.tsx`에 `to_char` 명시적 문자열 변환 적용
3. Vercel 배포 후 확인 → 여전히 `currentDate":"2026-08-28"` 반환
4. 빈 커밋으로 재배포 시도 → 동일 결과

**원인 추정**: Vercel edge/runtime이 이전 배포 응답을 캐싱 중. `force-dynamic` + `noStore()` 설정에도 불구하고 Vercel 자체 캐시가 동작.

**해결 방안**:

| 방안 | 설명 | 우선순위 |
|------|------|----------|
| Vercel 대시보드 수동 재배포 | 가장 확실한 방법 | 높음 |
| `Cache-Control: no-store` 헤더 추가 | 응답 캐시 완전 차단 | 중간 |
| Vercel 캐시 만료 대기 | 자동 해소 (수분~1시간) | 낮음 |
| 배포 URL 쿼리 파라미터 추가 | 캐시 키 변경 | 낮음 |

### 2.2 추가 검증 필요 항목

- `formatFullDate` 함수가 `currentDate`를 항상 `YYYY-MM-DD` 문자열로 받는지 확인
- PostgreSQL `DATE` 타입이 JSON 직렬화될 때 `Date` 객체로 변환되는지 확인
- Vercel 서버리스 함수의 DB 연결 풀이 쿼리 결과를 캐싱하는지 확인

---

## 3. Git 상태

| 리포지토리 | 브랜치 | 커밋 | 설명 |
|------------|--------|------|------|
| `Minipark-KOR/news` | main | `510ccbd` | chore: trigger vercel rebuild |
| `Minipark-KOR/news` | main | `b0d07b1` | fix: use to_char for DATE serialization |
| `Minipark-KOR/news` | main | `dde50fe` | rename: DevForge News → Mini News |
| `Minipark-KOR/news` | main | `5214cfd` | feat: news pipeline improvements (Phase 1-5) |
| `Minipark-KOR/kuhwa` | main | `6ab50aa` | fix: NEIS API error check + courses |

---

## 4. DB 현황

### news_source_tiers (29개 소스)

| Tier | 소스 예시 |
|------|-----------|
| 1 | Bloomberg, Reuters, FT, WSJ, Economist |
| 2 | TechCrunch, The Verge, Ars Technica, Wired |
| 3 | Google News Business 등 |

### news_articles

| 날짜 (KST) | 기사 수 |
|------------|---------|
| 2026-08-29 | 102건 |
| 2026-08-28 | 214건 |
| 2026-08-27 | 98건 |
| 2026-08-26 | 61건 |

---

## 5. 다음 단계

### 즉시 (오늘)

1. **날짜 표시 버그 해결**: Vercel 대시보드에서 수동 재배포 또는 `Cache-Control: no-store` 헤더 추가
2. **최종 검증**: 날짜 표시 정상화 확인 후 전체 기능 테스트

### 단기 (이번 주)

1. **번역 재시도 파이프라인**: `needs_summary` 상태 기사 47건에 대한 요약 재생성
2. **Systemd timer 모니터링**: `devforge-news.timer` 정상 동작 확인
3. **Telegram 다이제스트**: 정상 발송 테스트

### 중기 (다음 주)

1. **Phase 6 제안**: 카테고리 세분화 (3개 → 5개 이상)
2. **모니터링 대시보드**: 수집 성공률, 중복 제거율 등 지표 시각화
3. **소스 추가**: TechMeme, Hacker News 등 추가 피드 검토

---

## 6. 주요 파일 경로

| 파일 | 용도 |
|------|------|
| `web/src/app/articles/page.tsx` | 날짜 기반 페이지네이션 (서버 컴포넌트) |
| `web/src/app/articles/ArticlesClient.tsx` | 클라이언트 정렬/네비게이션 |
| `web/src/app/layout.tsx` | "Mini News" 브랜딩 |
| `collector.py` | RSS 수집, Circuit Breaker, 자동 배포 |
| `dedup.py` | TF-IDF 클러스터링 |
| `scoring.py` | DB 소스 tier, corroboration score |
| `translator.py` | 영→한 번역, 실패 시 재시도 |
| `tests/test_phase_improvements.py` | Phase 1-5 테스트 11개 |
| `kuhwa/api/calendar.js` | NEIS 에러 체크 + courses |
| `kuhwa/api/schedule.js` | NEIS 에러 코드 체크 |
