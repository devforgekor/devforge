# DevForge 시크릿 관리 전환 핸드오버

작성일: 2026-09-18
최종 업데이트: 2026-09-20 (신규 테넌트/KV 마이그레이션 §12 + 감사 정정 §4/§8/§9)
작성자: opencode 세션
상태: **완료** — 평문 시크릿 제거 완료 (secrets.env + .env.local 모두 삭제)

> **2026-09-20 변경 요약**: 시크릿 소스를 구 테넌트(`b08cd1bf`)의 단일 KV에서
> **신규 테넌트(`9ec65251`)의 다중 KV**로 전환. 상세는 §12. 아래 §1~§11 중
> 구 테넌트/구 KV를 가리키는 서술은 역사 기록이며, **현행 구성은 §12가 SSOT**.

---

## 1. 작업 개요

서버의 평문 시크릿 저장(`~/.config/devforge/secrets.env`)을 **Azure Key Vault 기반**으로 전환.
목표: 서버 디스크에 평문 시크릿을 두지 않는다.

### 최종 아키텍처 (2026-09-20 갱신 — 다중 KV)

> 아래는 **현행** 구성이다. (§12에 마이그레이션 상세)

```
신규 테넌트 9ec65251 (sub a942e898) — Azure Key Vault 다중 소스
   │
   ├─ kv-common-prod-krc    : 공통 시크릿 (API 키, AZURE-SP-*, DI 등)
   ├─ kv-devforge-prod2-krc : devforge 전용 (DEVFORGE-*, OCI-DEVFORGE-*)
   └─ kv-onmydoc-prod-krc   : onmydoc 전용 (OCI-ONMYDOC-*)
   │
   ├─ 주 1회 자동 (일 03:00) → GPG 암호화 백업 → ~/.config/devforge/backups/
   │     (공개키: 서버 / 개인키: 로컬 PC — 서버는 복호화 불가)
   │
   └─ 서버 서비스 시작 시 → Key Vault API 조회(다중 KV 병합) → 환경변수 주입
         kv-fetch-env.py 래퍼 경유 (AZURE_KEYVAULT_URLS 순서대로, 뒤 KV가 우선)
```

- devforge: `common + devforge` 병합 (93개)
- onmydoc: `common + onmydoc` 병합 (88개)

---

## 2. Azure Key Vault 정보 (구 테넌트 — 역사 기록)

> ⚠️ 2026-09-20 이전 구성(구 테넌트 `b08cd1bf`). 현행은 §12 참조.

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

### ⚠️ 멀티라인 PEM 저장 주의 (2026-09-19)
Key Vault 시크릿 값은 저장/조회 시 **개행이 공백으로 치환**된다. PEM(개인키/공개키)을 그대로
등록하면 복원 시 파싱이 틀려 지문이 달라진다 — 실제로 OCI 업로드 401의 원인이었다.

| 코드/파일 | Key Vault 시크릿 |
|----------|------------------|
| `~/.oci/oci_api_key.pem` (개인키) | `OCI-DEVFORGE-RSA-API-KEY` |
| `~/.oci/oci_api_key_public.pem` (공개키) | `OCI-DEVFORGE-RSA-API-PUB-KEY` |
| `~/.oci/config` 지문 | `OCI-DEVFORGE-API-KEY-FINGERPRINT` |
| 테넌시/유저 OCID | `OCI-DEVFORGE-TENANCY-OCID` / `OCI-DEVFORGE-USER-OCID` |

- **복원 규칙**: `-----BEGIN X-----`~`-----END X-----` 구간을 잘라 base64의 공백을 모두 제거한 뒤 64자 단위로 재래핑.
- devforge 등록 키 지문: `e7:58:b7:c6:59:a0:de:8c:97:41:2f:00:cc:b1:b9:08` (청주 `ap-chuncheon-1`).
- `OCI-DEVFORGE-PRIVATE-KEY` / `OCI-DEVFORGE-PUBLIC-KEY`는 **SSH 키**(OpenSSH/ssh-rsa)로 OCI API 키와 별개다.

---

## 4. 구현된 구성 요소

### 4.1 서버 스크립트 (git 커밋됨)

| 파일 | 역할 | 최근 개선 (2026-09-18) |
|------|------|----------------------|
| `scripts/deploy/kv-fetch-env.py` | Key Vault → 환경변수 주입 → 명령 실행 래퍼 | P0+P1: 에러 처리 강화, retry 로직 (최대 3회, exponential backoff) |
| `scripts/deploy/kv-backup.py` | Key Vault → GPG 암호화 백업 | P0+P1: 에러 처리 강화, retry 로직, 임시 파일 보안 강화 (tempfile 사용) |
| `scripts/deploy/kv-export-env.sh` | 지정 키만 KV 조회 → 임시 EnvironmentFile 생성(서비스별 최소 주입) | quoting artifact 자동 정규화 (2026-09-19) |
| `.github/_deprecated/sync-kv.yml.deprecated` | GitHub → Key Vault 이전 워크플로우 (폐기, 서버 직접 등록 권장) | 2026-09-20 비활성 |
| `.github/workflows/sync-secrets.yml` | GitHub Secrets → 서버 동기화 (기존, 유지) | - |

**P0+P1 개선 상세 (커밋 4c28ef7):**
- **#2 에러 처리 강화 (P0)**: HTTP 상태 코드 명시적 검증, JSON 파싱 예외 구체화, curl 실패 감지
- **#4 Retry 로직 (P1)**: 429/5xx/네트워크 오류 시 자동 재시도 (1s → 2s → 4s backoff)
- **#5 임시 파일 보안 (P1)**: `/tmp` 대신 `BACKUP_DIR` 사용, tempfile 모듈로 race condition 방지, try-finally로 정리 보장

### 4.2 systemd 서비스 & 컨테이너

**전환 완료 (시스템드 서비스 11개 + 컨테이너 3개):**
| 서비스 | Phase | 변경 일자 | 비고 |
|--------|-------|----------|------|
| `openrouter-rr-proxy.service` | Pre-Phase | 2026-09-17 | 첫 전환 (검증 완료) |
| `or-rate-limiter.service` | Pre-Phase | 2026-09-17 | 첫 전환 (검증 완료) |
| `anthropic-gudokpin-proxy.service` | Phase 1 | 2026-09-18 | - |
| `anthropic-openrouter-proxy.service` | Phase 1 | 2026-09-18 | - |
| `gemini-openai-proxy.service` | Phase 1 | 2026-09-18 | - |
| `anthropic-proxy.service` | Phase 2 | 2026-09-18 | **메인 프록시** |
| `devforge-watchdog.service` | Phase 2 | 2026-09-18 | Slack 알림 |
| `ebook-api.service` | Phase 4 | 2026-09-18 | ebooklib 프록시 인증 |
| `devforge-summary-retry.service` | 후속 | 2026-09-18 | news 요약 재시도 |
| `ebook-watcher.service` | 후속 | 2026-09-18 | ebook 파이프라인 loop |
| `devforge-news.service` | 후속 | 2026-09-18 | news collector |
| `container-postgres.container` | Phase 3 | 2026-09-18 | ExecStartPre + kv-export-env.sh |
| `container-devforge-mcp.container` | Phase 3 | 2026-09-18 | ExecStartPre + kv-export-env.sh |
| `container-webobsidian.container` | Phase 3 | 2026-09-19 | ExecStartPre + kv-export-env.sh (WEBOBSIDIAN-PASSWORD) |

**전환 불필요 (1개):**
| 서비스 | 사유 |
|--------|------|
| `container-devforge-worker.container` | 환경변수 미사용 |

**전환 보류 (1개):**
| 서비스 | 사유 | 우선순위 |
|--------|------|---------|
| `container-devforge-fastapi.container` | `calendar_router`는 이미 try/except로 가드됨(2026-09-20 확인) — 전환 자체가 저우선 | P3 |

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

## 7. 스크립트 환경변수 / 서버 로컬 파일 (2026-09-20 갱신)

스크립트(`kv-fetch-env.py`, `kv-backup.py`)가 읽는 env 변수. 미설정 시 **신규 테넌트 기본값**이 적용된다.

| env 변수 | 기본값 | 용도 |
|----------|--------|------|
| `AZURE_KEYVAULT_TENANT_ID` | `9ec65251-a106-4dc3-9878-4278caa80b1b` | 토큰 테넌트 |
| `AZURE_KEYVAULT_CLIENT_ID` | `abc5aab0-5394-46e0-bf4d-daf4129d1d78` (SP `DevForge-llm-Qwen`) | 토큰 클라이언트 |
| `AZURE_KEYVAULT_URLS` | devforge: `common,devforge-prod2` / onmydoc: `common,onmydoc` (콤마 구분) | 다중 KV 병합(뒤 우선) |
| `AZURE_KEYVAULT_CLIENT_SECRET_FILE` | `~/.config/devforge/azure-client-secret` | Client Secret 파일 경로 |

> 구 변수명 `AZURE_MESIDS_*`는 더 이상 사용하지 않는다(2026-09-20 폐지).

### ⚠️ 서버 로컬 파일
- `~/.config/devforge/azure-client-secret` — 신규 SP Client Secret (chmod 600)
- 구 SP secret 백업: `azure-client-secret.mesids.bak.*`
- 서버 스크립트가 이 파일에서 읽음 (환경변수 설정 불필요)

---

## 8. 남은 작업 (다음 세션)

### ✅ 완료: 2026-09-18 세션 2
1. **평문 시크릿 제거**
   - `~/.config/devforge/secrets.env` 삭제. 단 백업 `secrets.env.backup.20260918`는 **감사(2026-09-20) 시 디스크에 없음**.
   - `/opt/workspace/minihome/apps/news/.env.local` 삭제. 백업 `.env.local.backup.20260918`는 news가 아닌 `ebooklib/`에만 존재.
   - `~/.claude/secrets.env`(NOTION_TOKEN 평문, 당시 mode 644) 잔존 → **2026-09-20 삭제 완료**(§11 보안 노트).
   
2. **코드 리팩터링 (6개 파일) — 환경변수 우선으로 변경**
   - News 프로젝트: `translator.py`, `digest.py`, `exa_extractor.py`, `multilingual_processor.py`
   - Timetable 프로젝트: `main.py`, `calendar_sync/oauth_service.py`
   - 패턴: 환경변수 우선. 단 `secrets.env` fallback은 **호환성 유지용으로 잔존**(2026-09-20 감사 확인).
     `secrets.env` 파일 자체는 삭제됐으므로 런타임 영향 없음. 서버+워크스페이스 14개 .py에 잔존.
   
3. **kuhwa 워크플로우 수정**
   - `.github/workflows/update-schedule.yml` (문서 초판 오기: `kuhwa.yaml`): `secrets.env` 참조 없음
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
- **#3 부분 시크릿 로드 (P2)**: ✅ 완료 — `kv-fetch-env.py env --keys` + `kv-export-env.sh`(서비스별 최소 주입), 컨테이너/일부 서비스에 적용(2026-09-19)
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
- **완료**: 시스템드 서비스 11개 + 컨테이너 3개 전환 완료 (2026-09-18~19)
- **남은 작업**: `container-devforge-fastapi.container` (KV 미적용, §4.2 참조)

### 우선순위 2: secrets.env 파일 제거
- **완료**: `~/.config/devforge/secrets.env` 삭제 완료 (2026-09-18). 백업 파일은 감사 시 디스크에 없음.
- **완료**: `/opt/workspace/minihome/apps/news/.env.local` 삭제 완료 (2026-09-18). 백업은 `ebooklib/.env.local.backup.20260918`에만 존재.
- **완료**: `~/.claude/secrets.env`(NOTION_TOKEN 평문) 삭제 완료 (2026-09-20, §11 참조).

### 우선순위 3: GitHub 시크릿 정리
- 안정 확인 후 (2주) GitHub org 시크릿에서 시크릿 값 삭제
- GitHub은 Azure 연동 자격증명 (`AZURE_MESIDS_*`)만 유지

### 우선순위 4: sync-kv.yml 워크플로우 정리
- `.github/workflows/sync-kv.yml` → **2026-09-20 `.github/_deprecated/sync-kv.yml.deprecated`로 폐기 이동**
- 기존 GitHub → Key Vault 이전용(수동)이었고 `tr` 충돌 + 시크릿 접근 문제로 불안정 → 서버 직접 등록(`kv-backup.py` 로직) 권장

---

## 9. Git 상태 (역사 기록)

- 2026-09-19: `orchestrator.py`가 `DEVFORGE_WATCHDOG_PING_SSH`/`_URL`을 읽도록 변경(구 `WATCHDOG_PING_*` 제거). KV 값 오등록(`c9146961…`) → `onmydoc` 교정, watchdog 재시작 후 핑 갱신 확인.
- 2026-09-18: `kv-fetch-env.py`/`kv-backup.py` P0+P1 개선(에러 처리·retry·tempfile 보안), 커밋 `4c28ef7`.
- 2026-09-20: 다중 KV 지원 변경분은 §12. 감사 결과 `kv-fetch-env.py`/`kv-backup.py` 변경 커밋 `0ec3855`가 **미푸시**였음 → **2026-09-20 push 완료**(`main` = `origin/main`).

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
- `secrets.env` 평문은 삭제 완료(2026-09-18). 단 감사(2026-09-20) 결과 `secrets.env.backup.20260918` 백업은 디스크에 없음.
- **`~/.claude/secrets.env` (NOTION_TOKEN 평문)**: 감사(2026-09-20)에서 발견. 당시 mode 644 → 600 조치 후 **삭제 완료**. KV(`NOTION-TOKEN-KEY`)와 중복이었고 `~/.claude/mcp.json`에 notion 서버 설정도 없어(비활성) 안전하게 제거.
- GitHub org 시크릿에 시크릿 값이 아직 존재 — 안정 확인 후 삭제
- 백업 .gpg 파일은 서버 디스크에 있지만 복호화 불가 (개인키 없음) — 추가 오프사이트 복사 권장
- 프로세스 env 덤프 시 KV 값이 노출될 수 있음 — `tr`/`xargs`로 `/proc/<pid>/environ` 전체 출력 금지, 필요한 키만 grep

---

## 12. 신규 테넌트/KV 마이그레이션 (2026-09-20) — 현행 SSOT

### 배경
Azure 계정을 신규 계정(20137133, tenant `9ec65251`)으로 통일. 시크릿 소스를 구 테넌트(`b08cd1bf`)의
단일 KV(`kv-devforge-prod-krc`)에서 신규 테넌트의 **다중 KV**로 전환.

### 현행 구성

| 항목 | 값 |
|------|-----|
| 테넌트 ID | `9ec65251-a106-4dc3-9878-4278caa80b1b` |
| 구독 ID | `a942e898-e1ee-47f4-b9b3-d9475672ff4e` |
| Service Principal | `DevForge-llm-Qwen` (앱 ID `abc5aab0-5394-46e0-bf4d-daf4129d1d78`) |
| devforge KV | `kv-common-prod-krc` + `kv-devforge-prod2-krc` (다중 병합, 93개) |
| onmydoc KV | `kv-common-prod-krc` + `kv-onmydoc-prod-krc` (다중 병합, 88개) |
| Document Intelligence | `di-common-prod-krc` (F0, rg-server-common-prod-krc) |
| KV 접근 방식 | Access Policy (get/list) — SP에 부여 |

### 스크립트 변경
| 파일 | 변경 |
|------|------|
| `scripts/deploy/kv-fetch-env.py` | 다중 KV 병합(`AZURE_KEYVAULT_URLS`), 신규 테넌트/SP 기본값 |
| `scripts/deploy/kv-backup.py` | 동일 다중 KV 지원 |
| onmydoc `~/.local/bin/kv-fetch-env.py` | 동일 버전(기본 URL: common+onmydoc) |
| onmydoc `~/.local/bin/git-credential-kv.py` | 신규 테넌트/SP, URL→`kv-common-prod-krc` |

### 전환/검증 결과
- devforge: KV 사용 systemd 서비스 11개 + 컨테이너 3개(postgres/mcp/webobsidian) 재기동 → 시크릿 93개 로드
  (fastapi 컨테이너는 KV 미적용 — §4.2 참조)
- onmydoc: `git credential get` 인증 OK, `kv-fetch-env.py env` 88개
- 시크릿 이관 검증: 구 KV 101개 → 신규 KV 값 해시 **MATCH 101 / MISMATCH 0**
  (2026-09-20 마이그레이션 세션 검증치, 이후 감사에서는 재검증 안 함)
- GPG 백업 재생성 확인

### 구 KV 처리 (보류)
- 구 `kv-devforge-prod-krc`(sub `89c6a8ee`, tenant `b08cd1bf`)는 **purge protection**으로
  **2026-12-09까지 퍼지 불가** → 이름 재사용 보류.
- 런타임은 신규 KV로 완전 전환되어 구 KV 미사용(영향 없음).
- **2026-12-20** 재생성·`prod2` 이관 예정: task **#38**, handover `KV-NAME-REUSE-2026-12`.

### 롤백 자산
- 구 SP secret: `~/.config/devforge/azure-client-secret.mesids.bak.*` (양 서버)
- 구 스크립트 백업: `*.mesids.bak` (onmydoc)
- GPG 백업: `~/.config/devforge/backups/secrets-backup-20260920T035400.gpg` (구 101개 포함)

### 부수 이슈
- `container-webobsidian`: stale child cgroup(2026-09-19 생성)으로 재기동 실패 →
  `container-webobsidian.service.d/10-slice.conf`의 `Slice=webobsidian-workaround.slice`로 우회 복구.
  근본 해결은 재부팅. handover `WEBOBSIDIAN-CGROUP-2026-09-20`.