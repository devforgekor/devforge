# DevForge 시크릿 관리 전환 핸드오버

작성일: 2026-09-18
최종 업데이트: 2026-09-18
작성자: opencode 세션
상태: 진행 중 (프록시 2개 전환 완료 + 백업 구축 완료)

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
| 시크릿 수 | 56개 (이름: 밑줄 `_` → 하이픈 `-` 변환됨) |
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

---

## 4. 구현된 구성 요소

### 4.1 서버 스크립트 (git 커밋됨)

| 파일 | 역할 |
|------|------|
| `scripts/deploy/kv-fetch-env.py` | Key Vault → 환경변수 주입 → 명령 실행 래퍼 |
| `scripts/deploy/kv-backup.py` | Key Vault → GPG 암호화 백업 |
| `.github/workflows/sync-kv.yml` | GitHub → Key Vault 이전 워크플로우 (수동) |
| `.github/workflows/sync-secrets.yml` | GitHub Secrets → 서버 동기화 (기존, 유지) |

### 4.2 systemd 서비스

**전환 완료 (Key Vault 기반):**
| 서비스 | 변경 내용 |
|--------|----------|
| `openrouter-rr-proxy.service` | `EnvironmentFile` 제거 → `kv-fetch-env.py` 경유 |
| `or-rate-limiter.service` | 동일 |

**아직 secrets.env 사용 (전환 대기):**
```
anthropic-proxy.service
anthropic-openrouter-proxy.service
anthropic-gudokpin-proxy.service
devforge-news.service
devforge-summary-retry.service
devforge-watchdog.service
ebook-watcher.service
gemini-openai-proxy.service
gemini-session.service
```

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

### 우선순위 1: 나머지 systemd 서비스 전환
- 9개 서비스의 `EnvironmentFile=secrets.env` 제거 → `kv-fetch-env.py` 경유로 변경
  ```
  anthropic-proxy.service
  anthropic-openrouter-proxy.service
  anthropic-gudokpin-proxy.service
  devforge-news.service
  devforge-summary-retry.service
  devforge-watchdog.service
  ebook-watcher.service
  gemini-openai-proxy.service
  gemini-session.service
  ```
- 각 서비스별 스크립트가 `os.environ`으로 읽는지, secrets.env 직접 파싱인지 확인 필요
- `anthropic_gudokpin.py`, `anthropic_openrouter.py`는 secrets.env 직접 파싱 → 환경변수 우선으로 수정 필요

### 우선순위 2: secrets.env 파일 제거
- 모든 서비스 전환 완료 후 `~/.config/devforge/secrets.env` 삭제
- 단, **로컬 전용 값** (DATAIMPULSE_HOST 등 비밀 아님)은 어디로 보관할지 결정 필요

### 우선순위 3: GitHub 시크릿 정리
- 안정 확인 후 (2주) GitHub org 시크릿에서 시크릿 값 삭제
- GitHub은 Azure 연동 자격증명 (`AZURE_MESIDS_*`)만 유지

### 우선순위 4: sync-kv.yml 워크플로우 정리
- 현재 GitHub → Key Vault 이전용 (수동). `tr` 충돌 + 시크릿 접근 문제로 GitHub Actions에서 불안정
- 서버 직접 등록 방식(`kv-backup.py` 로직)이 더 확실 — 워크플로우 대신 스크립트 활용 권장

---

## 9. Git 상태

### 관련 파일 (커밋됨)
| 파일 | 커밋 |
|------|------|
| `scripts/deploy/kv-fetch-env.py` | `f1160f1` |
| `scripts/deploy/kv-backup.py` | `40dc5d8` |
| `docs/handover-secrets-kv.md` | `f1160f1` |
| `.github/workflows/sync-kv.yml` | `1bde988` (이전) |
| `.github/workflows/sync-secrets.yml` | 수정 다수 (이전) |

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
- `secrets.env` 평문은 아직 서버에 존재 — 전환 완료 후 반드시 삭제
- GitHub org 시크릿에 시크릿 값이 아직 존재 — 안정 확인 후 삭제
- 백업 .gpg 파일은 서버 디스크에 있지만 복호화 불가 (개인키 없음) — 추가 오프사이트 복사 권장