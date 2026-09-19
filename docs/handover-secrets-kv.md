# DevForge 시크릿 관리 전환 핸드오버

작성일: 2026-09-18
최종 업데이트: 2026-09-19 (watchdog 외부 heartbeat 키 복구)
작성자: opencode 세션
상태: **완료** — 평문 시크릿 제거 완료 (secrets.env + .env.local 모두 삭제)

---

## 1. 작업 개요

서버의 평문 시크릿 저장(`~/.config/devforge/secrets.env`)을 **Azure Key Vault 기반**으로 전환.
목표: 서버 디스크에 평문 시크릿을 두지 않는다.

### 최종 아키텍처
```
Azure Key Vault (kv-devforge-prod-krc) — 단일 소스 (56개 시크릿)
   │
   ├─ 주 1회 자동 (일 03:00) → GPG 암호화 백업 → ~/.config/devforge/backups/
   │     (공개키: 서버 / 개인키: 로컬 PC — 서버는 복호화 불가)
   │
   └─ 서버 서비스 시작 시 → Key Vault API 조회 → 환경변수 주입 (디스크 저장 없음)
         kv-fetch-env.py 래퍼 경유
```

---

## 2. Azure Key Vault 정보

| 항목 | 값 |
|------|-----|
| Key Vault URL | `https://kv-devforge-prod-krc.vault.azure.net` |
| Key Vault 이름 | `kv-devforge-prod-krc` |
| 시크릿 수 | 94개 (이름: 밑줄 `_` → 하이픈 `-` 변환됨) |
| 테넌트 ID | `b08cd1bf-7952-489c-8fbb-aa907bb74709` |
| 구독 ID | `e71711e2-5df5-4259-bd0d-4bd58fd1ca67` (또는 d0a7db48) |
| Service Principal | 이름: `kv-app-devforge-prod-krc` |
|  - 애플리케이션 ID | `169a8e1e-9bd1-4023-a78a-785e2fec321d` |
|  - 개체 ID (SP) | `5f6d81eb-9a58-4cac-9eea-b31f64e900d4` |
|  - App Registration 개체 ID | `d4077db7-e7ad-4787-8849-d4bfd3c7b9f6` |
| RBAC 역할 | Key Vault 비밀 사용자 + 비밀 책임자 (이 리소스 범위) |
| Client Secret | `~/.config/devforge/azure-client-secret` (chmod 600) |

### ⚠️ 주의: RBAC 멤버 선택
- RBAC 할당 시 **서비스 사용자(SP) 개체 ID `5f6d81eb`** 로 선택해야 함
- App Registration 개체 ID(`d4077db7`)로 선택하면 접근 안 됨 (403)
- 멤버 검색 시 표시 이름 `kv-app-devforge-prod-krc` 로 검색

---

## 3. Key Vault 시크릿 이름 규칙

Key Vault는 시크릿 이름에 **밑줄(`_`)을 허용하지 않음** → 하이픈(`-`)으로 변환.

| GitHub/env 이름 | Key Vault 이름 |
|----------------|---------------|
| `OPENROUTER_MESIDS_API_KEY` | `OPENROUTER-MESIDS-API-KEY` |
| `BRAVE_API_KEYS` | `BRAVE-API-KEYS` |
| `DEVFORGE_DATABASE_URL` | `DEVFORGE-DATABASE-URL` |

**복원 규칙**: Key Vault 조회 시 하이픈 → 밑줄 변환 (`kv-fetch-env.py`가 처리)

**API 키 rotation 전략**: 계정별 분리 (2026-09-18 업데이트)
- **BRAVE, CONTEXT7, EXA, GEMINI, TAVILY, YOUCOM**: 각 4개 계정 분리
  - `HYEONMINPARK4U`, `MESIDS`, `MINIPARK4U`, `PLAYPARK4U`
  - TAVILY/YOUCOM의 MESIDS는 `-GITHUB` 접미사 사용
- **OPENROUTER**: 3개 계정 (`HYEONMINPARK4U`, `MESIDS`, `MINIPARK4U`)
- 목적: rate limit 분산, quota 격리, 장애 격리

### ⚠️ 비밀 아닌 config 값 (2026-09-19 추가)
Key Vault는 "시크릿" 저장소지만, 아래 값들은 **자격증명이 아닌 설정값**이라
Key Vault 단일 소스로 관리한다(서버 디스크 평문 방지). 코드는 `DEVFORGE_` 접두어로 읽는다.

| 코드가 읽는 env | Key Vault 시크릿 이름 | 예시 값 | 읽는 위치 |
|----------------|----------------------|---------|----------|
| `DEVFORGE_WATCHDOG_PING_SSH` | `DEVFORGE-WATCHDOG-PING-SSH` | `onmydoc` | `scripts/lib/watchdog/orchestrator.py:_ping_external` |
| `DEVFORGE_WATCHDOG_PING_URL` | `DEVFORGE-WATCHDOG-PING-URL` | (미사용, HTTP 핑 시) | 동일 |

**규칙**: 비밀/비밀아님 모두 KV에 넣고, 코드에서는 `DEVFORGE_` 접두어로 읽는다.
`secrets.env` 삭제(2026-09-18) 이후 비밀 아닌 키를 KV에 미등록하면 조용히 skip되어
기능이 죽는다(이번 watchdog heartbeat 정지 사례). 신규 키 추가 시 KV 등록을 누락하지 말 것.

---

## 4. 구현된 구성 요소

### 4.1 서버 스크립트 (git 커밋됨)

| 파일 | 역할 | 최근 개선 (2026-09-18) |
|------|------|----------------------|
| `scripts/deploy/kv-fetch-env.py` | Key Vault → 환경변수 주입 → 명령 실행 래퍼 | P0+P1: 에러 처리 강화, retry 로직 (최대 3회, exponential backoff) |
| `scripts/deploy/kv-backup.py` | Key Vault → GPG 암호화 백업 | P0+P1: 에러 처리 강화, retry 로직, 임시 파일 보안 강화 (tempfile 사용) |
| `.github/workflows/sync-kv.yml` | GitHub → Key Vault 이전 워크플로우 (수동) | - |
| `.github/workflows/sync-secrets.yml` | GitHub Secrets → 서버 동기화 (기존, 유지) | - |

**P0+P1 개선 상세 (커밋 4c28ef7):**
- **#2 에러 처리 강화 (P0)**: HTTP 상태 코드 명시적 검증, JSON 파싱 예외 구체화, curl 실패 감지
- **#4 Retry 로직 (P1)**: 429/5xx/네트워크 오류 시 자동 재시도 (1s → 2s → 4s backoff)
- **#5 임시 파일 보안 (P1)**: `/tmp` 대신 `BACKUP_DIR` 사용, tempfile 모듈로 race condition 방지, try-finally로 정리 보장

### 4.2 systemd 서비스 & 컨테이너

**전환 완료 (Key Vault 기반, 10개):**
| 서비스 | Phase | 변경 일자 | 비고 |
|--------|-------|----------|------|
| `openrouter-rr-proxy.service` | Pre-Phase | 2026-09-17 | 첫 전환 (검증 완료) |
| `or-rate-limiter.service` | Pre-Phase | 2026-09-17 | 첫 전환 (검증 완료) |
| `anthropic-gudokpin-proxy.service` | Phase 1 | 2026-09-18 | - |
| `anthropic-openrouter-proxy.service` | Phase 1 | 2026-09-18 | - |
| `gemini-openai-proxy.service` | Phase 1 | 2026-09-18 | - |
| `anthropic-proxy.service` | Phase 2 | 2026-09-18 | **메인 프록시** |
| `devforge-watchdog.service` | Phase 2 | 2026-09-18 | Slack 알림 |
| `container-postgres.container` | Phase 3 | 2026-09-18 | ExecStartPre + kv-export-env.sh |
| `container-devforge-mcp.container` | Phase 3 | 2026-09-18 | ExecStartPre + kv-export-env.sh |
| `ebook-api.service` | Phase 4 | 2026-09-18 | ebooklib 프록시 인증 |

**전환 불필요 (1개):**
| 서비스 | 사유 |
|--------|------|
| `container-devforge-worker.container` | 환경변수 미사용 |

**전환 보류 (1개):**
| 서비스 | 사유 | 우선순위 |
|--------|------|---------|
| `container-devforge-fastapi.container` | 기존 코드 버그 (`calendar_router` undefined), Key Vault 전환 무관 | P3 |

### 4.3 GPG 백업

| 항목 | 값 |
|------|-----|
| 공개키 (서버 보유) | `~/.config/devforge/backup-public-key.asc` |
| 개인키 (로컬 PC) | `backup-private-key.asc` (텔레그램으로 전송됨) |
| GPG 수신자 | `DevForge Secrets Backup` |
| 백업 디렉토리 | `~/.config/devforge/backups/` |
| 백업 주기 | 매주 일요일 03:00 (systemd 타이머 `kv-backup.timer`) |
| 보관 기간 | 60일 |
| 파일 형식 | `secrets-backup-YYYYMMDDTHHMMSS.gpg` |

**⚠️ 중요:**
- 개인키는 서버에서 **삭제 완료** (gpg 키링에서 제거됨)
- 서버에서 복호화 시도 → "No secret key" (정상, 확인됨)
- 개인키는 **로컬 PC에만** 보관 — 분실 시 백업 복구 불가
- 로컬에서 복호화: `gpg --import backup-private-key.asc` 후 `gpg --decrypt <file>.gpg`

---

## 5. 이전 진행 과정 (GitHub → Key Vault)

### GitHub 조직 정보
- **조직명**: `devforgekor` (https://github.com/devforgekor)
- **저장소**: 8개 (devforge, kuhwa, timetable, cashbook, ebook, azure, pdf-converter, oci-arm-grabber)
- **위치**: Korea, South

### 이전 과정
1. GitHub org 시크릿에 있는 시크릿 값들 → 서버 `github-secrets.env` 파일로 동기화 (기존 sync-secrets 워크플로우)
2. 서버에서 `kv-backup.py` 로직으로 56개 시크릿 조회 → REST API로 Key Vault에 등록
3. Key Vault 시크릿 이름 변환: `_` → `-`
4. 검증: 56개 등록 확인

### ⚠️ 시크릿 등록 중 겪은 문제 (기록)
- **`tr` 명령 충돌**: 서버에 `/home/opc/.local/bin/tr` 커스텀 바이너리가 시스템 `tr`을 가림 → `/usr/bin/tr` 사용해야 함
- **RBAC 개체 ID 혼동**: App Registration 개체 ID vs SP 개체 ID 다름 → SP 개체 ID(`5f6d81eb`)로 할당
- **Key Vault 시크릿 이름 밑줄 불허**: 하이픈으로 변환

---

## 6. 검증 결과

| 항목 | 결과 |
|------|------|
| Key Vault 시크릿 등록 | ✅ 56개 |
| 백업 GPG 암호화 | ✅ `secrets-backup-20260918T002629.gpg` (3.5KB) |
| 백업 복호화 (로컬) | ✅ 성공 확인 (개인키 삭제 전) |
| 서버 복호화 시도 | ✅ 실패 ("No secret key") — 개인키 없음 |
| openrouter-rr-proxy | ✅ Key Vault에서 56개 로드 → 3개 OpenRouter 키 → 포트 8451 동작 |
| OpenRouter 실제 요청 | ✅ `deepseek-v4-flash` 정상 응답 |
| or-rate-limiter | ✅ active |
| kv-backup.timer | ✅ active (일 03:00, 다음 2026-09-20) |

---

## 7. GitHub 시크릿 (org 레벨) — Azure 연동용

| 시크릿 | 값/용도 |
|--------|---------|
| `AZURE_MESIDS_CLIENT_SECRET_ID` | `169a8e1e-9bd1-4023-a78a-785e2fec321d` (SP 앱 ID) |
| `AZURE_MESIDS_CLIENT_SECRET_VALUE` | Client Secret 값 |
| `AZURE_MESIDS_TENANT_ID` | `b08cd1bf-7952-489c-8fbb-aa907bb74709` |
| `AZURE_MESIDS_KEYVAULT_URL` | `https://kv-devforge-prod-krc.vault.azure.net` |
| `AZURE_MESIDS_SUBSCRIPTION_ID` | `d4077db7-e7ad-4787-8849-d4bfd3c7b9f6` (⚠️ 이 값은 개체 ID — 실제 구독은 `e71711e2...` 또는 `d0a7db48...`. 검증 필요) |

### ⚠️ 서버 로컬 파일 (GitHub 외)
- `~/.config/devforge/azure-client-secret` — Client Secret 값 (chmod 600)
- 서버 스크립트가 이 파일에서 읽음 (환경변수 설정 불필요)

---

## 8. 남은 작업 (다음 세션)

### ✅ 완료: 2026-09-18 세션 2
1. **평문 시크릿 제거 완료**
   - `~/.config/devforge/secrets.env` 삭제 (백업: `secrets.env.backup.20260918`)
   - `/opt/workspace/minihome/apps/news/.env.local` 삭제 (백업: `.env.local.backup.20260918`)
   
2. **코드 리팩터링 완료 (6개 파일)**
   - News 프로젝트: `translator.py`, `digest.py`, `exa_extractor.py`, `multilingual_processor.py`
   - Timetable 프로젝트: `main.py`, `calendar_sync/oauth_service.py`
   - 패턴: 환경변수 우선 → secrets.env fallback 제거
   
3. **kuhwa 워크플로우 수정**
   - `.github/workflows/kuhwa.yaml`: `secrets.env` 참조 제거
   - 환경변수 직접 주입 방식으로 변경
   
4. **ebook-api.service 전환**
   - Key Vault 통합 (kv-fetch-env.py 래퍼)
   - WorkingDirectory 경로 문제 수정 중

### ✅ 완료: P0+P1 개선 (2026-09-18 세션 1)
- #2 에러 처리 강화 (P0) ✅
- #4 Retry 로직 (P1) ✅
- #5 임시 파일 보안 (P1) ✅

### 우선순위 0: P2 개선 (선택)
- **#1 토큰 캐싱 (P2)**: Azure AD 토큰 1시간 유효 → 메모리/파일 캐싱으로 서비스 재시작 시 1~2초 절약
- **#3 부분 시크릿 로드 (P2)**: `kv-fetch-env.py --keys KEY1,KEY2` 옵션 추가 (현재 56개 전체 로드)
- **#6 GPG import 중복 제거 (P3)**: `gpg --list-keys` 체크 후 없을 때만 import
- **#7 동시 실행 보호 (P3)**: PID 파일 lock (systemd timer는 중복 방지 내장)

### 우선순위 0.5: 누락 config 키 감사 (2026-09-19 추가)
`secrets.env` 삭제 시 KV에 미등록된 비밀 아닌 config가 다수 존재. 현재 KV에 **없는** 키(사용처 확인 후 등록 필요):

| 누락 키 | 사용처(추정) | 비고 |
|---------|-------------|------|
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` | news collector, golden_image, kuhwa | `GMAIL_SMTP_MINIPARK4U`(KV)와 별개 |
| `DUCKDNS_ACCOUNT` / `DUCKDNS_DOMAIN` / `DUCKDNS_*_IP` | duckdns 갱신 스크립트 | `DUCKDNS_TOKEN_KEY`만 KV에 있음 |
| `OCI_REGION` / `OCI_HOME_REGION` / `OCI_IDCS_URL` | OCI 자동화 | |
| `DEVFORGE_SERVER_HOST` / `_USER` / `_SSH_KEY` | 서버 자기참조 자동화 | SSH 키는 비밀 |
| `NEWS_WEB_URL` / `VERCEL_*` | news/vercel 재배포 | |
| `EBOOK_DAILY_TRAFFIC_LIMIT_MB`, `DATAIMPULSE_*`, `MASKPROXY_*`, `NEON_DATABASE_URL` 등 | 각 프로젝트 | 사용 여부 확인 필요 |

→ KV 미등록 상태로 코드가 env를 읽으면 조용히 skip/기본값 사용 → 기능 정지 가능.
   사용처를 grep으로 확인해 실제 필요한 키만 KV에 등록한다.

### 우선순위 1: 나머지 systemd 서비스 전환
- **완료**: 10개 서비스 전환 완료 (2026-09-18)
- **남은 작업**: `container-devforge-fastapi.container` (기존 버그로 인해 보류)

### 우선순위 2: secrets.env 파일 제거
- **완료**: `~/.config/devforge/secrets.env` 삭제 완료 (2026-09-18)
- **완료**: `/opt/workspace/minihome/apps/news/.env.local` 삭제 완료 (2026-09-18)
- **완료**: 백업 생성 (`*.backup.20260918`)

### 우선순위 3: GitHub 시크릿 정리
- 안정 확인 후 (2주) GitHub org 시크릿에서 시크릿 값 삭제
- GitHub은 Azure 연동 자격증명 (`AZURE_MESIDS_*`)만 유지

### 우선순위 4: sync-kv.yml 워크플로우 정리
- 현재 GitHub → Key Vault 이전용 (수동). `tr` 충돌 + 시크릿 접근 문제로 GitHub Actions에서 불안정
- 서버 직접 등록 방식(`kv-backup.py` 로직)이 더 확실 — 워크플로우 대신 스크립트 활용 권장

---

## 9. Git 상태

### 2026-09-19: watchdog 외부 heartbeat 복구 (커밋 대상)
| 파일 | 변경 내용 |
|------|----------|
| `scripts/lib/watchdog/orchestrator.py` | `_ping_external()`가 `DEVFORGE_WATCHDOG_PING_SSH` / `DEVFORGE_WATCHDOG_PING_URL`을 읽도록 변경 (기존 `WATCHDOG_PING_*` 제거) |
| `docs/handover-secrets-kv.md` | 누락 config 키 감사 + 비밀 아님 키 규칙 추가 |

- KV `DEVFORGE-WATCHDOG-PING-SSH` 값: `c9146961-...`(SP id, 오등록) → `onmydoc`으로 교정
- 검증: watchdog 재시작 후 onmydoc `~/wd_monitor/last_ping` 갱신 확인(age 54s)

### 관련 파일 (커밋됨)
| 파일 | 최근 커밋 | 변경 내용 |
|------|----------|----------|
| `scripts/deploy/kv-fetch-env.py` | `4c28ef7` | P0+P1 개선: +184줄 (에러 처리, retry, HTTP 상태 검증) |
| `scripts/deploy/kv-backup.py` | `4c28ef7` | P0+P1 개선: +235줄 (에러 처리, retry, tempfile 보안) |
| `docs/handover-secrets-kv.md` | `4c28ef7` | 개선 내역 업데이트 |
| `.github/workflows/sync-kv.yml` | `1bde988` (이전) | - |
| `.github/workflows/sync-secrets.yml` | 수정 다수 (이전) | - |

### 푸시 상태 (2026-09-18)
```
✅ origin/main: b9305b3..4c28ef7 (7 커밋 푸시 완료)
```

### 미푸시 커밋 (2026-09-18 기준)
```
f1160f1 docs: 시크릿 관리 전환 핸드오버 + kv-fetch-env 래퍼
36bddfe auto: sync 2026-09-18
40dc5d8 feat: Azure Key Vault → GPG 암호화 백업 스크립트 (주 1회 systemd 타이머)
```

### ⚠️ 미커밋/미푸시 상태
- `_archive/seedling`, `collect_checkpoint.json` — 로컬 변경 있음 (무관)
- origin에 푸시 여부: 위 3개 커밋은 origin보다 앞섬 → `git push` 필요

---

## 10. 복원 절차 (DR)

### Key Vault 분실/삭제 시
1. 로컬 PC에서 GPG 백업 복호화
   ```bash
   gpg --import backup-private-key.asc
   gpg --decrypt ~/.config/devforge/backups/secrets-backup-*.gpg
   ```
2. 새 Key Vault 생성
3. `kv-backup.py` 로직 반대로: env 파일 → Key Vault REST API PUT (이름 변환: `_` → `-`)
4. 서비스 재시작

### 서버 재구축 시
1. `kv-fetch-env.py` + `kv-backup.py` 배포
2. `azure-client-secret` 파일 배치 (chmod 600)
3. `backup-public-key.asc` 배치
4. systemd 서비스 유닛 적용 + 재시작

---

## 11. 보안 노트

- 개인키는 **로컬 PC에서만** 보관 (서버/이메일/클라우드 저장 금지)
- `azure-client-secret`은 서버에서만, chmod 600
- `secrets.env` 평문은 삭제 완료(2026-09-18). 백업 `secrets.env.backup.20260918`만 잔존 — 2주 안정화 후 삭제
- GitHub org 시크릿에 시크릿 값이 아직 존재 — 안정 확인 후 삭제
- 백업 .gpg 파일은 서버 디스크에 있지만 복호화 불가 (개인키 없음) — 추가 오프사이트 복사 권장
- 프로세스 env 덤프 시 KV 값이 노출될 수 있음 — `tr`/`xargs`로 `/proc/<pid>/environ` 전체 출력 금지, 필요한 키만 grep