# Timetable — Google Calendar Sync from Excel/Sheets

> 분석일: 2026-08-30
> 위치: `/opt/workspace/timetable/`
> 상태: experimental (standalone)
> 포트: 8003 (127.0.0.1)

## 1. 개요

Excel 파일 업로드 또는 Google Sheets URL 입력 → 사용자 Google Calendar에 일정 자동 동기화하는 웹 애플리케이션.

### 1.1 핵심 기능

| 기능 | 설명 |
|------|------|
| **Google OAuth 2.0 로그인** | 사용자별 인증, access/refresh token 관리 |
| **Excel 업로드** | `.xlsx` → openpyxl + pandas 파싱 → Calendar 이벤트 |
| **Google Sheets 연동** | URL 입력 → gspread → 사용자 access token으로 읽기 |
| **동기화 모드 2종** | FULL_REPLACE (전체 교체) / INCREMENTAL_UPDATE (증분 업데이트) |
| **연간 일정 관리** | 학사/강의 일정 등 연 단위 정기 업데이트 대응 |

### 1.2 입력 경로

```
사용자 입력
├── Excel 파일 업로드 (.xlsx)
│   └── openpyxl → pandas DataFrame → CalendarEvent[]
└── Google Sheets URL
    └── gspread → Worksheet → CalendarEvent[]
```

## 2. 시스템 아키텍처

```
┌─────────────────────────────────────────────────┐
│                  사용자 브라우저                    │
└──────────┬──────────────────────────┬────────────┘
           │ HTTP                     │ OAuth redirect
           ▼                          ▼
┌──────────────────────────────────────────────┐
│          FastAPI (uvicorn, port 8003)          │
│  ├─ /calendar/auth/google/*  — OAuth 2.0      │
│  ├─ /calendar/upload/*       — Excel/Sheets   │
│  ├─ /calendar/status         — 동기화 상태     │
│  └─ /calendar/logout         — 세션 종료       │
│                                               │
│  의존성: Jinja2 templates (5개), StaticFiles  │
└──────┬──────────┬──────────────┬──────────────┘
       │          │              │
       ▼          ▼              ▼
┌──────────┐ ┌──────────┐ ┌──────────────┐
│PostgreSQL│ │Google API│ │  Google API  │
│(podman)  │ │ Calendar │ │  Sheets      │
│user_token│ │ v3       │ │  v4          │
│테이블     │ │ batch    │ │  (gspread)   │
└──────────┘ └──────────┘ └──────────────┘
```

### 2.1 기술 스택

| 계층 | 기술 |
|------|------|
| **Runtime** | Python 3.9+, uvicorn |
| **Framework** | FastAPI, Jinja2, StaticFiles |
| **DB** | PostgreSQL 16 (podman exec psql) |
| **OAuth** | google-auth-oauthlib (Flow), google-auth (Credentials) |
| **Calendar API** | google-api-python-client (build, batch) |
| **Sheets API** | gspread (user access token) |
| **Excel** | pandas, openpyxl |
| **Secrets** | `~/.config/devforge/secrets.env` |

## 3. 파일 구조

```
timetable/
├── main.py                          # FastAPI entry point, uvicorn.run
├── calendar_sync/
│   ├── __init__.py
│   ├── models.py                    # Pydantic models (SyncMode, CalendarEvent, etc.)
│   ├── db_migration.py              # user_tokens 테이블 생성
│   ├── oauth_service.py            # GoogleOAuthService (token 교환/갱신/저장)
│   ├── excel_parser.py             # ExcelParser (openpyxl → CalendarEvent[])
│   ├── sheets_parser.py            # SheetsParser (gspread → CalendarEvent[])
│   ├── calendar_service.py         # CalendarService (batch CRUD, sync mode)
│   ├── router.py                   # API routes, session 관리
│   └── templates/
│       ├── base.html               # Layout (한글 UI, 깔끔한 스타일)
│       ├── login.html              # 로그인 페이지
│       ├── upload.html             # 업로드 폼 (Excel + Sheets URL)
│       ├── result.html             # 동기화 결과 표시
│       └── status.html             # 사용자 정보/토큰 상태
└── lib/                            # ⚠️ DevForge lib 과다 복사 (~150개 파일)
    ├── db.py                       # psql_json, psql_ok, esc_sql (사용 중)
    ├── watchdog/                   # 미사용
    ├── extract_llm/               # 미사용
    ├── debate/                     # 미사용
    └── ... (140+ unused files)
```

## 4. 모듈 상세

### 4.1 `models.py` — Pydantic 모델

| 모델 | 필드 | 용도 |
|------|------|------|
| `SyncMode` | FULL_REPLACE / INCREMENTAL_UPDATE | 동기화 전략 |
| `UserToken` | user_id, email, access_token, refresh_token, token_expiry, scopes | DB 저장용 |
| `CalendarEvent` | title, start_date, end_date, description, location, attendees, start_time, end_time | 파싱 결과 |
| `SyncConfig` | mode, date_range_start/end, calendar_id | 동기화 설정 |
| `SyncResult` | created, updated, deleted, errors, events | 결과 보고 |

**🔴 `CalendarEvent.to_google_event()`**: `start_time`/`end_time` 미지정 시 각각 `00:00:00`/`23:59:59` 기본값 사용. timezone은 `Asia/Seoul` 고정.

### 4.2 `oauth_service.py` — GoogleOAuthService

인증 흐름:

```
1. GET /auth/google/login
   → google_auth_oauthlib.Flow → authorization_url (state 포함)
   → 사용자 브라우저 리디렉션

2. Google 로그인 → 인증 코드 → /auth/google/callback
   → Flow.fetch_token(code) → Credentials
   → Google API userinfo.email 조회
   → store_tokens() → DB UPSERT
   → 세션에 user_id 저장

3. 이후 요청
   → get_valid_credentials()
   → token_expiry - 5min > now → refresh_token으로 갱신
   → DB에 갱신된 token 저장
```

| 메서드 | 설명 |
|--------|------|
| `get_auth_url()` | OAuth 인증 URL 생성 (state 포함) |
| `exchange_code(code)` | 인증 코드 → access/refresh token |
| `get_valid_credentials()` | 토큰 만료 체크 + 자동 갱신 |
| `get_user_info(credentials)` | Google userinfo API → email, name |
| `store_tokens(user_id, email, creds)` | INSERT ... ON CONFLICT DO UPDATE |
| `get_tokens(user_id)` | DB 조회 |
| `revoke_tokens(user_id)` | Google revoke API + DB 삭제 |

**OAuth Scope**: `calendar.events` + `userinfo.email` + `userinfo.profile` + `spreadsheets.readonly`

### 4.3 `excel_parser.py` — ExcelParser

**COLUMN_MAP** (한글/영문 컬럼명 매핑):

| 키 | 한글 별칭 |
|----|----------|
| `title` | 제목, 과목명, 일정명 |
| `start_date` | 시작일, 시작날짜, Date |
| `end_date` | 종료일, 종료날짜 |
| `start_time` | 시작시간, 시작시각 |
| `end_time` | 종료시간, 종료시각 |
| `description` | 설명, 내용, 비고, 메모 |
| `location` | 장소, 위치 |
| `attendees` | 참석자, 참여자 |

**파싱 로직**:
1. `pd.read_excel(bytes)` → header row 자동 감지
2. 컬럼명 → COLUMN_MAP 역매칭 (정규화 + strip)
3. 날짜/시간 파싱: `datetime.strptime()` with multiple format fallback
4. `attendees`는 쉼표/세미콜론 분할 → strip
5. `CalendarEvent[]` 반환

### 4.4 `sheets_parser.py` — SheetsParser

**URL 파싱**: `https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid=...` → 정규식 추출

**특이사항**:
- `gspread.Client`에 사용자 `access_token`을 직접 `Credentials` 객체로 전달
- 모든 워크시트 순회 (`client.open_by_key().worksheets()`)
- 각 워크시트의 모든 행을 `CalendarEvent`로 변환

### 4.5 `calendar_service.py` — CalendarService

**Batch 처리**:
- `BATCH_SIZE = 50` — Google batch 제한 준수
- `new_batch_http_request()` 사용
- 항목별 retry: exponential backoff (MAX_RETRIES=3, base_delay=1, max_delay=10)

**FULL_REPLACE 모드**:
1. `calendar.events().list()` → 기존 이벤트 조회
2. batch delete (기존 이벤트 일괄 삭제)
3. batch insert (새 이벤트 일괄 추가)

**INCREMENTAL_UPDATE 모드**:
- 기존 이벤트와 제목+시작일 기준 매칭
- 매칭 시: `patch()` 업데이트
- 미매칭: `insert()` 신규 추가
- 누락된 기존 이벤트: `delete()` 제거

### 4.6 `router.py` — API Routes

| 경로 | 메서드 | 설명 |
|------|--------|------|
| `/calendar/auth/google/login` | GET | Google OAuth 시작 (sheets=true → sheets scope 포함) |
| `/calendar/auth/google/callback` | GET | OAuth 콜백 (code → token → session) |
| `/calendar/login` | GET | 로그인 페이지 렌더링 |
| `/calendar/upload` | GET | 업로드 페이지 렌더링 |
| `/calendar/upload/excel` | POST | Excel 파일 업로드 + 동기화 |
| `/calendar/upload/sheets` | POST | Sheets URL 제출 + 동기화 |
| `/calendar/status` | GET | 사용자 상태 (토큰, 이메일) |
| `/calendar/logout` | GET | 세션 종료 |

**세션 관리**: `_sessions: dict` (인메모리 dict, `calendar_session` 쿠키)

### 4.7 `db_migration.py` — user_tokens 테이블

```sql
CREATE TABLE IF NOT EXISTS user_tokens (
    user_id         TEXT PRIMARY KEY,
    email           TEXT UNIQUE NOT NULL,
    access_token    TEXT NOT NULL,
    refresh_token   TEXT,
    token_expiry    TIMESTAMP WITH TIME ZONE NOT NULL,
    scopes          TEXT[],
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

## 5. 코드 리뷰 결과

### 5.1 종합 점수: 80 / 100

| 평가 차원 | 점수 | 근거 |
|-----------|------|------|
| 요구사항 충족 | 95 | 컨셉 문서의 모든 요구사항 구현 |
| 코드 품질 | 80 | 구조 명확, 모듈 분리 양호, but 몇 가지 NIT |
| 보안 | 75 | OAuth/refresh token 적절, but SQL injection 우려 |
| 확장성 | 60 | 인메모리 세션, 단일 시트, 과도한 lib 복사 |

### 5.2 🔴 Critical Issues

| # | 이슈 | 파일 | 설명 |
|---|------|------|------|
| C1 | `datetime.utcnow()` 사용 | `models.py:34-35` | Python 3.12+ deprecated, 타임존 정보 소실 |
| C2 | SQL 문자열 인터폴레이션 | `oauth_service.py` | `esc_sql()`에 의존한 SQL injection 방어 — parameterized query가 근본 해법 |

**C1 상세**:
```python
# 현재 (deprecated)
created_at: datetime = Field(default_factory=datetime.utcnow)
# 권장
created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

**C2 상세**:
- `oauth_service.py`의 `store_tokens()`, `get_tokens()`, `revoke_tokens()` 모두 f-string + `esc_sql()` 사용
- `lib/db.py`의 `escape_sql_string()`는 `\` → `\\`, `'` → `''` escape + `SET standard_conforming_strings = off` 의존
- 권장: `psycopg2` 또는 `psycopg`의 parameterized query (`%s` placeholders)

### 5.3 🟡 Warning Issues

| # | 이슈 | 파일 | 설명 |
|---|------|------|------|
| W1 | 인메모리 세션 | `router.py:34` | 서버 재시작 시 전체 세션 소실, 수평 확장 불가, stale 세션 미정리 |
| W2 | `lib/` 과다 복사 | `lib/` | ~150개 파일 중 실제 사용은 `db.py` 뿐, watchdog/extract_llm/debate/enrich 등 불필요 |
| W3 | Sheets Parser 토큰 갱신 불가 | `sheets_parser.py` | 사용자 access_token을 그대로 `Credentials` 객체로 전달, refresh token 미활용 |
| W4 | 단일 시트만 지원 | `excel_parser.py` | `pd.read_excel`에 `sheet_name=None` 미지정 |
| W5 | requirements.txt / pyproject.toml 없음 | 루트 | 의존성 목록 부재로 재현성 확보 불가 |

### 5.4 🟢 Comment Issues

| # | 이슈 | 설명 |
|---|------|------|
| N1 | 하드코딩된 경로 | `router.py` 템플릿/리디렉션 경로에 `url_for()` 대신 문자열 리터럴 사용 |
| N2 | `new_batch_http_request()` | Google API Python Client v2에서 제거 예정 (`batch` → `_batch` 우회) |
| N3 | 에러 처리 일관성 | `upload_excel`/`upload_sheets` 모두 try-except로 500 반환하지만 에러 메시지 포맷 불일치 |
| N4 | CORS 미설정 | standalone으로 동작하나, 추후 프론트 분리 시 CORS 미들웨어 필요 |

## 6. 배포 방법

### 6.1 사전 조건

| 항목 | 필수값 |
|------|--------|
| PostgreSQL | podman container (local), `devforge_app` DB |
| Google OAuth 2.0 | Client ID / Client Secret (Web application) |
| Redirect URI | `http://localhost:8003/calendar/auth/google/callback` |
| Secrets | `~/.config/devforge/secrets.env` 에 `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` |

### 6.2 실행

```bash
cd /opt/workspace/timetable

# DB 마이그레이션
python3 -c "
from calendar_sync.db_migration import migrate
migrate()
"

# 서버 실행
python3 main.py --port 8003 --host 127.0.0.1
```

### 6.3 접속

```
http://localhost:8003/calendar/login
```

## 7. 권장 개선 사항 (Priority 순)

| 우선순위 | 작업 | 영향 |
|----------|------|------|
| P0 | `utcnow()` → `now(timezone.utc)` 교체 | Deprecation 제거, 타임존 정확성 |
| P0 | `requirements.txt` 추가 | 재현성 확보 |
| P1 | `lib/` 디렉토리 정리 (불필요 파일 제거) | 유지보수성, 용량 절감 |
| P1 | PostgreSQL parameterized query로 마이그레이션 | SQL injection 근본 해결 |
| P2 | DB-backed 세션 도입 | 서버 재시작 내구성, 수평 확장 |
| P2 | Excel multi-sheet 지원 (`sheet_name=None`) | 사용자 편의 |
| P3 | Sheets Parser에 token refresh 로직 추가 | 장기 세션 안정성 |
| P3 | `url_for()`로 경로 리팩터 | 유지보수성 |
| P4 | `new_batch_http_request()` → google-api-python-client v2 대응 | 미래 호환성 |