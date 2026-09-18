# Azure Key Vault 전환 분석 — 하드코딩 환경변수 마이그레이션

작성일: 2026-09-18
분석자: Claude Code
목적: secrets.env 의존성 제거를 위한 전환 전략 수립
**상태: Phase 1~4 완료 (9개 서비스 전환 + secrets.env 제거 + 코드 레벨 리팩토링 완료)**

---

## 전환 완료 현황 (2026-09-18)

### ✅ 완료된 서비스 (9개)

**Phase 1: 저위험 프록시 (3개)**
- ✅ `anthropic-gudokpin-proxy.service` — kv-fetch-env.py 경유
- ✅ `anthropic-openrouter-proxy.service` — kv-fetch-env.py 경유
- ✅ `gemini-openai-proxy.service` — kv-fetch-env.py 경유

**Phase 2: 핵심 프록시 (2개)**
- ✅ `anthropic-proxy.service` — **Claude Code 메인 프록시**, kv-fetch-env.py 경유
- ✅ `devforge-watchdog.service` — Slack 알림, kv-fetch-env.py 경유

**Phase 3: 컨테이너 (2개)**
- ✅ `container-postgres.container` — ExecStartPre + kv-export-env.sh
- ✅ `container-devforge-mcp.container` — ExecStartPre + kv-export-env.sh

**Pre-Phase (참고)**
- ✅ `openrouter-rr-proxy.service` — 첫 전환 검증 완료
- ✅ `or-rate-limiter.service` — 첫 전환 검증 완료

### ⏭️ 전환 불필요 (1개)
- `container-devforge-worker.container` — 환경변수 미사용

### ⚠️ 보류 (2개)
- `ebook-watcher.service` — 경로 문제 (`/opt/workspace/ebooklib` 존재하지 않음, 실제 경로: `/opt/workspace/minihome/apps/ebooklib`)
- `container-devforge-fastapi.container` — 기존 코드 버그 (`NameError: name 'calendar_router' is not defined`), Key Vault 전환과 무관

### 🛠️ 구현된 신규 스크립트

1. **kv-fetch-env.py `env` 서브커맨드** (추가)
   - `python3 kv-fetch-env.py env` → stdout에 KEY='VALUE' 출력
   - shell eval 안전: single quote 이스케이프 처리
   
2. **kv-export-env.sh** (신규)
   - Key Vault → 임시 env 파일 생성 (`/run/user/{uid}/kv-temp.env`)
   - 컨테이너 ExecStartPre에서 사용
   - 권한 600 자동 설정

3. **토큰 캐싱** (P2 개선 #1)
   - `/run/user/{uid}/kv-token-cache.json`에 토큰 캐싱
   - TTL 55분 (Azure AD 토큰 3599초 - 5분 여유)
   - 캐시 히트 시 토큰 API 호출 건너뛰기 (4~5초 절약)

---

## Phase 4: 코드 레벨 리팩토링 (2026-09-18 완료)

### 목표
secrets.env 파일 직접 파싱 로직 제거 → 환경변수 우선 패턴으로 전환

### 수정된 파일

#### 1. News App (minihome/apps/news)

**exa_extractor.py** (Multi-Engine Search)
- `_load_keys_from_secrets()`: 개별 계정 키 자동 수집 (`{PREFIX}_{ACCOUNT}_API_KEY` 패턴)
- EXA, BRAVE, TAVILY 3개 엔진 모두 환경변수 우선 → secrets.env fallback
- **버그 수정**: `TRAVILY` → `TAVILY` prefix 오타 수정
```python
# Before: TRAVILY_API_KEYS 환경변수 불일치
# After: TAVILY_API_KEYS (Key Vault 실제 값과 일치)
```

**translator.py**
- `_load_secret()`: 환경변수 우선 → secrets.env fallback
- `_get_openrouter_keys()`: 환경변수 우선 체크 추가

**digest.py**
- `_load_secrets()`: OPENROUTER_, TELEGRAM_, GEMINI_, DEEPSEEK_ prefix 환경변수 우선

**multilingual_processor.py**
- `_load_tavily_keys()`: 환경변수 우선, 암호화/평문 키 파싱 유지

#### 2. Timetable App (minihome/apps/timetable)

**main.py**
- GOOGLE_, CLIENT_, CALENDAR_ prefix 환경변수 우선

**calendar_sync/oauth_service.py**
- 이미 환경변수 우선 패턴 구현 (변경 불필요)

#### 3. Server Scripts (projects/server/scripts)

**proxies/openrouter_rr_proxy.py**
- `_load_keys()`: 환경변수 우선 → secrets.env fallback
- 개별 계정 키 자동 수집 (`OPENROUTER_{ACCOUNT}_API_KEY`)

**lib/research/web.py**
- `_load_keys_for()`: Brave, Tavily, Youcom 개별 계정 키 자동 수집
- **버그 수정**: `TRAVILY` → `TAVILY` prefix 수정

### 검증 결과

✅ **구문 검증 통과**
- exa_extractor.py
- openrouter_rr_proxy.py
- web.py

✅ **통합 테스트 통과** (Key Vault 환경변수 로드 테스트)
```
News App:
  exa: 4개 ✓
  brave: 4개 ✓
  tavily: 4개 ✓

OpenRouter RR Proxy:
  openrouter: 3개 ✓

Search Proxy (web.py):
  brave: 4개 ✓
  tavily: 4개 ✓
  youcom: 4개 ✓
```

### secrets.env 제거

✅ **백업 생성**
```bash
~/.config/devforge/secrets.env.backup.20260918 (6.9K)
```

✅ **파일 제거 완료**
```bash
rm ~/.config/devforge/secrets.env
```

### 남은 작업 (TODO)

1. **파일명 변경 (보류)**
   - `exa_extractor.py` → `multi_engine_search.py` (또는 `mes.py`)
   - 이유: Exa, Brave, Tavily 3개 엔진 통합이지만 파일명이 단일 제공자처럼 보임
   - 일정: 추후 결정

---

## 1. 현황 분석

### 1.1 secrets.env 직접 참조 중인 서비스

| 서비스 | EnvironmentFile 사용 | 전환 난이도 | 비고 |
|--------|---------------------|-----------|------|
| `anthropic-gudokpin-proxy.service` | ✅ | **쉬움** | 파이썬 스크립트, os.environ만 사용 |
| `anthropic-openrouter-proxy.service` | ✅ | **쉬움** | 파이썬 스크립트, os.environ만 사용 |
| `anthropic-proxy.service` | ✅ | **쉬움** | 파이썬 스크립트, os.environ만 사용 |
| `gemini-openai-proxy.service` | ✅ | **쉬움** | 파이썬 스크립트, os.environ만 사용 |
| `ebook-watcher.service` | ✅ | **중간** | 외부 워크스페이스 (/opt/workspace/ebooklib) |
| `devforge-watchdog.service` | ✅ (optional) | **쉬움** | 파이썬 스크립트, os.environ만 사용 |
| `container-devforge-fastapi.container` | ✅ (--env-file) | **중간** | 컨테이너 환경변수, entrypoint 스크립트 확인 필요 |
| `container-devforge-mcp.container` | ✅ (--env-file) | **중간** | 컨테이너 환경변수, entrypoint 스크립트 확인 필요 |
| `container-postgres.container` | ✅ | **낮음** | DB 비밀번호만 필요, 단일 변수 |

**전환 완료 (참고용):**
- `openrouter-rr-proxy.service` ✅ (kv-fetch-env.py 경유)
- `or-rate-limiter.service` ✅ (kv-fetch-env.py 경유)

### 1.2 환경변수 사용 패턴 분석

**파이썬 스크립트 (6개 프록시 + watchdog):**
```python
# 모두 os.environ.get() 패턴 사용 — 환경변수 주입만으로 전환 가능
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
GUDOKPIN_API_KEY = os.environ.get("GUDOKPIN_API_KEY", "")
OPENROUTER_MESIDS_API_KEY = os.environ.get("OPENROUTER_MESIDS_API_KEY", "")
```
→ **secrets.env 직접 파싱 없음**, 환경변수만 참조

**컨테이너 (3개):**
- Quadlet `.container` 파일에서 `--env-file /home/opc/.config/devforge/secrets.env` 사용
- 컨테이너 내부 스크립트가 환경변수 참조

**외부 워크스페이스 (ebook-watcher):**
- `/opt/workspace/ebooklib/scripts/pipeline.py`가 환경변수 참조
- devforge 서버 외부 프로젝트 — 독립 전환 필요

---

## 2. 전환 전략

### 전략 A: systemd 서비스 (파이썬 스크립트 6개)
**적용 대상:** anthropic-{gudokpin,openrouter,proxy}, gemini-openai-proxy, devforge-watchdog

**방법:**
```ini
[Service]
# Before:
# EnvironmentFile=/home/opc/.config/devforge/secrets.env
# ExecStart=/usr/bin/python3 /opt/projects/server/scripts/proxies/anthropic_gudokpin.py

# After:
ExecStart=/opt/projects/server/scripts/deploy/kv-fetch-env.py \
          /usr/bin/python3 /opt/projects/server/scripts/proxies/anthropic_gudokpin.py
```

**장점:**
- 코드 변경 없음
- openrouter-rr-proxy와 동일 패턴 (검증 완료)
- 롤백 용이 (systemd 파일만 복원)

**단점:**
- 서비스 시작 시 매번 Key Vault API 호출 (56개 시크릿)
- 토큰 캐싱 없으면 1~2초 추가 지연

**권장:** ✅ 우선 적용 (안정성 > 성능)

---

### 전략 B: Quadlet 컨테이너 (3개)
**적용 대상:** devforge-{fastapi,mcp,worker}, postgres

#### B-1. ExecStartPre로 환경변수 파일 생성 (임시)
```ini
[Service]
# Key Vault → 임시 env 파일 생성 → 컨테이너에 주입 → 삭제
ExecStartPre=/opt/projects/server/scripts/deploy/kv-export-env.sh
ExecStart=/usr/bin/podman run ... --env-file /run/user/1000/kv-temp.env ...
ExecStopPost=/bin/rm -f /run/user/1000/kv-temp.env
```

**장점:**
- 기존 컨테이너 이미지/entrypoint 수정 불필요
- Quadlet 문법 유지

**단점:**
- 임시 파일이 잠깐 디스크에 존재 (메모리 tmpfs지만 여전히 평문)
- 스크립트 추가 필요 (kv-export-env.sh)

#### B-2. 컨테이너 entrypoint 래핑 (권장)
```ini
[Service]
# kv-fetch-env.py가 환경변수 주입 후 podman run 실행
ExecStart=/opt/projects/server/scripts/deploy/kv-fetch-env.py \
          /usr/bin/podman run ... \
          --env-file <(env | grep -E "^(DEVFORGE|AZURE|SLACK)") \
          localhost/devforge-fastapi:latest
```

**문제:** Quadlet은 ExecStart에서 process substitution `<()` 미지원

#### B-3. 환경변수 개별 전달 (최종 권장)
```bash
# 1. kv-fetch-env.py로 전체 환경변수 로드
# 2. 컨테이너 실행 시 --env로 필요한 변수만 전달
ExecStart=/opt/projects/server/scripts/deploy/kv-container-run.sh devforge-fastapi
```

**kv-container-run.sh 예시:**
```bash
#!/bin/bash
# Key Vault에서 환경변수 로드 후 컨테이너 실행
eval $(kv-fetch-env.py env)  # 환경변수 로드만 (프로세스 실행 안 함)
exec podman run --name "$1" \
  --env DEVFORGE_DATABASE_URL \
  --env AZURE_STORAGE_CONNECTION_STRING \
  --env SLACK_BOT_TOKEN_KEY \
  ...
  localhost/"$1":latest
```

**장점:**
- 디스크에 평문 파일 없음 (메모리만 사용)
- 컨테이너별 필요한 변수만 전달 (최소 권한)

**단점:**
- 래퍼 스크립트 필요
- Quadlet 파일 수정 복잡도 증가

**권장:** ✅ B-1 임시 파일 방식 먼저 적용 → 안정화 후 B-3로 개선

---

### 전략 C: 외부 워크스페이스 (ebook-watcher)
**적용 대상:** `/opt/workspace/ebooklib`

**방법 1: systemd 서비스에서 kv-fetch-env.py 경유**
```ini
[Service]
ExecStart=/opt/projects/server/scripts/deploy/kv-fetch-env.py \
          /opt/workspace/ebooklib/apps/backend/venv/bin/python3 \
          /opt/workspace/ebooklib/scripts/pipeline.py loop
```

**방법 2: ebooklib 프로젝트에 독립 kv-fetch 구현**
- `/opt/workspace/ebooklib`에 자체 시크릿 관리 구현
- devforge 서버 의존성 제거

**권장:** ✅ 방법 1 (서버 통합 관리)

---

## 3. 마이그레이션 순서 (추천)

### Phase 1: 저위험 프록시 서비스 (1~2시간)
1. `anthropic-gudokpin-proxy.service`
2. `anthropic-openrouter-proxy.service`
3. `gemini-openai-proxy.service`

**검증:**
- 서비스 재시작 후 정상 응답 확인
- 로그에서 Key Vault 연결 성공 메시지 확인
- 실제 API 요청 1회 테스트

### Phase 2: 핵심 프록시 + watchdog (2~3시간)
4. `anthropic-proxy.service` (Claude Code 메인 프록시)
5. `devforge-watchdog.service` (Slack 알림 의존)
6. `ebook-watcher.service`

**검증:**
- Claude Code 세션에서 실제 대화 테스트
- Watchdog 알림 Slack 전송 확인

### Phase 3: 컨테이너 서비스 (3~4시간)
7. `container-postgres.container` (DB — 영향도 낮음)
8. `container-devforge-worker.container`
9. `container-devforge-mcp.container`
10. `container-devforge-fastapi.container`

**검증:**
- Pod 헬스체크 통과 확인
- MCP 서버 응답 확인 (curl http://127.0.0.1:8000/health)
- FastAPI 엔드포인트 확인

### Phase 4: 정리 (1시간)
- ✅ `~/.config/devforge/secrets.env` 백업 → 삭제 (2026-09-18)
- ✅ 코드 레벨 리팩토링: 환경변수 우선 패턴 전환 (News, Timetable, Server 프록시)
- ⏭️ 로컬 전용 변수 (DATAIMPULSE_HOST 등) → 별도 파일로 분리 (필요시)
- ⏭️ 2주 안정화 기간 후 GitHub org 시크릿 정리

---

## 4. 위험 완화 전략

### 4.1 롤백 플랜
각 서비스 전환 시:
```bash
# Before 백업
cp ~/.config/containers/systemd/anthropic-gudokpin-proxy.service \
   ~/.config/containers/systemd/anthropic-gudokpin-proxy.service.bak

# 전환 실패 시 즉시 롤백
mv ~/.config/containers/systemd/anthropic-gudokpin-proxy.service.bak \
   ~/.config/containers/systemd/anthropic-gudokpin-proxy.service
systemctl --user daemon-reload
systemctl --user restart anthropic-gudokpin-proxy
```

### 4.2 Key Vault API 장애 대응
**문제:** Key Vault API가 다운되면 모든 서비스 재시작 불가

**완화책:**
1. **P2 개선 #1 (토큰 캐싱)** 적용 → API 호출 빈도 감소
2. **GPG 백업에서 자동 복구** 스크립트 작성:
   ```bash
   # Key Vault 실패 시 최신 GPG 백업 복호화 → secrets.env 복원
   /opt/projects/server/scripts/deploy/kv-restore-from-backup.sh
   ```
3. **Systemd Restart 정책**: `Restart=on-failure`, `RestartSec=60s`

### 4.3 단계별 검증 체크리스트
각 서비스 전환 후:
- [ ] `systemctl --user status <service>` — active 확인
- [ ] `journalctl --user -u <service> -n 50` — Key Vault 로드 로그 확인
- [ ] 실제 기능 테스트 (API 요청, Slack 알림 등)
- [ ] 24시간 모니터링 → 다음 서비스 전환

---

## 5. 필요한 신규 스크립트

### 5.1 kv-export-env.sh (전략 B-1용)
```bash
#!/bin/bash
# Key Vault → 임시 env 파일 생성 (컨테이너용)
set -euo pipefail
OUTPUT=${1:-/run/user/$(id -u)/kv-temp.env}
python3 /opt/projects/server/scripts/deploy/kv-fetch-env.py env > "$OUTPUT"
chmod 600 "$OUTPUT"
```

### 5.2 kv-fetch-env.py 확장 (env 서브커맨드)
```python
# 기존: kv-fetch-env.py <command> [args...] → execvpe로 프로세스 대체
# 추가: kv-fetch-env.py env → stdout에 KEY=VALUE 출력 후 종료

if sys.argv[1] == "env":
    token = get_token()
    secrets = list_secrets(token)
    for kv_name in secrets:
        env_name = kv_name.replace("-", "_")
        value = get_secret_value(token, kv_name)
        # shell eval 안전: 값을 single quote로 감싸고 내부 ' 이스케이프
        safe_value = value.replace("'", "'\\''")
        print(f"{env_name}='{safe_value}'")
    sys.exit(0)
```

### 5.3 kv-restore-from-backup.sh (DR)
```bash
#!/bin/bash
# GPG 백업에서 secrets.env 복원 (Key Vault 장애 시)
BACKUP_DIR=~/.config/devforge/backups
LATEST=$(ls -t "$BACKUP_DIR"/secrets-backup-*.gpg | head -1)
gpg --decrypt "$LATEST" > ~/.config/devforge/secrets.env
chmod 600 ~/.config/devforge/secrets.env
echo "✅ Restored from $LATEST"
```

---

## 6. 예상 소요 시간 & 리소스

| Phase | 소요 시간 | 리스크 | 롤백 시간 |
|-------|---------|--------|----------|
| Phase 1 (프록시 3개) | 1~2시간 | 낮음 | 5분 |
| Phase 2 (프록시 3개 + watchdog) | 2~3시간 | 중간 | 10분 |
| Phase 3 (컨테이너 4개) | 3~4시간 | 높음 | 15분 |
| Phase 4 (정리) | 1시간 | 낮음 | - |
| **총계** | **7~10시간** | - | **30분** |

**권장 일정:**
- Phase 1~2: 1일차 오전~오후 (Claude Code 메인 프록시 포함)
- Phase 3: 2일차 오전~오후 (컨테이너 재시작 필요, 서비스 중단 가능)
- Phase 4: 3일차 (모니터링 + 정리)

---

## 7. 최종 권장 사항

### 우선순위 0: P2 개선 (#1 토큰 캐싱) 먼저 구현
- Phase 1 시작 전에 토큰 캐싱 구현 → API 호출 빈도 90% 감소
- 서비스 재시작 시간 단축 (2초 → 0.1초)
- Key Vault API 부하 감소

### 우선순위 1: Phase 1~2 (프록시 6개)
- **리스크 낮음**, 롤백 용이
- openrouter-rr-proxy 패턴 검증 완료
- 일주일 안정화 후 Phase 3 진행

### 우선순위 2: Phase 3 (컨테이너)
- **리스크 높음**, 충분한 테스트 필요
- 전략 B-1 (임시 파일) 먼저 적용 → 안정 확인 후 B-3 (래퍼)로 개선

### 우선순위 3: Phase 4 (정리)
- secrets.env 삭제는 **2주 안정화 후**
- 로컬 전용 변수는 `/home/opc/.config/devforge/local.env`로 분리

---

## 8. 다음 액션

1. **P2 개선 #1 (토큰 캐싱)** 구현 요청 확인
2. **Phase 1 전환 스크립트 작성** (systemd 유닛 수정 자동화)
3. **kv-fetch-env.py env 서브커맨드 추가**
4. **Phase 1 테스트 환경 준비** (로컬 Key Vault 모킹 또는 staging 환경)

전환 시작 전 최종 승인 필요.
