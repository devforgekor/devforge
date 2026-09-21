# Option 2: Secret Injection Hardening 가이드

**작성일:** 2026-09-21  
**대상:** AI 에이전트 또는 시스템 관리자  
**소요 시간:** 1-2시간  
**난이도:** Medium  
**목적:** 환경변수 방식에서 LoadCredential 방식으로 전환하여 보안 강화

---

## 배경

**현재 상황:**
- KV 시크릿이 `kv-fetch-env.py` → EnvironmentFile → 프로세스 환경변수로 주입
- 문제: `podman exec <c> env`, `/proc/<pid>/environ`, `ps e`로 시크릿 노출 가능

**목표:**
- systemd LoadCredential 또는 podman --secret 사용
- 시크릿을 파일로 전달, 환경변수에 남기지 않음

**Origin:** refactoring-roadmap.md §5.1 (2026-09-19 WebObsidian 사건)

---

## 현재 상태 확인

### 단계 2 완료 여부 확인
```bash
# 서비스별 최소 주입 확인
systemctl --user cat cashbook.service | grep "kv-fetch-env.py"
# 예상: ExecStart=/opt/projects/server/scripts/deploy/kv-fetch-env.py ...
```

✅ 현재 cashbook은 단계 2 (최소 주입) 완료

---

## 전환 방법 선택

### Option A: systemd LoadCredential (권장)
- **장점:** systemd 표준 기능, 깔끔한 구현
- **단점:** systemd 244+ 필요 (확인 필요)

### Option B: podman --secret
- **장점:** 컨테이너 전용, Quadlet 통합
- **단점:** Quadlet 서비스에만 적용 가능

> **⚠️ 2026-09-21 검증 결과 — 이 가이드 원안(Task 1.2의 `ExecStartPre` + `LoadCredential`)은 동작하지 않습니다.**
> systemd는 `LoadCredential=`을 `ExecStartPre=`보다 **먼저** 처리하므로 소스 파일이 아직 없고, 유닛이
> `Failed at step CREDENTIALS ... (status=243/CREDENTIALS)`로 실패합니다 (systemd 252 실측).
> 올바른 방식은 **파일 방식**: `ExecStartPre`가 600 파일을 생성하고, 앱이 경로 env(`CASHBOOK_CREDENTIAL_FILE`)로
> 그 파일을 직접 읽습니다. 실제 적용본/검증 수치는 `docs/security/secret-injection-hardening.md` 참조.
> 진정한 `LoadCredential=` 격리가 필요하면 secret 생성용 **별도 oneshot 유닛을 서비스에 `Before=`로 선행** 배치해야 합니다.

**권장:** 파일 방식(위 정정안). Option B(podman --secret)는 컨테이너(postgres)에만 해당.

---

## 작업 계획

### Task 1: 시범 적용 — cashbook 서비스 (30분)

> **2026-09-21 완료.** 실제 적용은 아래 원안이 아니라 파일 방식으로 진행됨:
> `scripts/deploy/kv-to-credential.sh`(신규) + `cashbook/main.py`의 `_load_api_key()` +
> 유닛에서 `kv-fetch-env` 래퍼 제거 → `ExecStartPre` + `CASHBOOK_CREDENTIAL_FILE`.
> before/after: env 121→13, KV 시크릿 env 노출 전체→0, `?key=` 정상 200/오답 401.
> 아래 1.1~1.4는 원안(참고용)이며, `LoadCredential` 사용 부분은 위 경고대로 무효입니다.

#### 1.1 시크릿 파일 생성 스크립트 작성
```bash
# /opt/projects/server/scripts/deploy/kv-to-credential.sh
#!/bin/bash
# KV에서 시크릿을 가져와 credential 파일로 저장

set -e

KEY_NAME="$1"  # e.g., CASHBOOK-API-KEY
OUTPUT_FILE="$2"  # e.g., /run/user/1000/credentials/cashbook_key

if [[ -z "$KEY_NAME" || -z "$OUTPUT_FILE" ]]; then
  echo "Usage: $0 <KEY_NAME> <OUTPUT_FILE>"
  exit 1
fi

# KV에서 시크릿 가져오기
SECRET_VALUE=$(python3 /opt/projects/server/scripts/deploy/kv-fetch-env.py env "$KEY_NAME" 2>/dev/null | grep "^CASHBOOK_API_KEY=" | cut -d= -f2-)

if [[ -z "$SECRET_VALUE" ]]; then
  echo "Error: Failed to fetch $KEY_NAME from Key Vault"
  exit 1
fi

# 파일로 저장 (600 권한)
mkdir -p "$(dirname "$OUTPUT_FILE")"
echo -n "$SECRET_VALUE" > "$OUTPUT_FILE"
chmod 600 "$OUTPUT_FILE"

echo "✅ Credential saved to $OUTPUT_FILE"
```

```bash
chmod +x /opt/projects/server/scripts/deploy/kv-to-credential.sh
```

#### 1.2 systemd 서비스 수정

**현재 (cashbook.service):**
```ini
[Service]
ExecStart=/opt/projects/server/scripts/deploy/kv-fetch-env.py /usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8100
```

**변경 후:**
```ini
[Service]
WorkingDirectory=/opt/projects/server/cashbook
ExecStartPre=/opt/projects/server/scripts/deploy/kv-to-credential.sh CASHBOOK-API-KEY /run/user/1000/credentials/cashbook_key
LoadCredential=cashbook_key:/run/user/1000/credentials/cashbook_key
ExecStart=/bin/bash -c 'export CASHBOOK_API_KEY=$(cat ${CREDENTIALS_DIRECTORY}/cashbook_key) && exec /usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8100'
ExecStopPost=/bin/rm -f /run/user/1000/credentials/cashbook_key
Restart=always
RestartSec=5
```

**적용:**
```bash
cat > ~/.config/systemd/user/cashbook.service << 'EOF'
[Unit]
Description=Cashbook FastAPI
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/projects/server/cashbook
ExecStartPre=/opt/projects/server/scripts/deploy/kv-to-credential.sh CASHBOOK-API-KEY /run/user/1000/credentials/cashbook_key
LoadCredential=cashbook_key:/run/user/1000/credentials/cashbook_key
ExecStart=/bin/bash -c 'export CASHBOOK_API_KEY=$(cat ${CREDENTIALS_DIRECTORY}/cashbook_key) && exec /usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8100'
ExecStopPost=/bin/rm -f /run/user/1000/credentials/cashbook_key
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user restart cashbook.service
sleep 3
systemctl --user status cashbook.service
```

#### 1.3 보안 검증 (10분)
```bash
# 1. 서비스 정상 동작 확인
curl -s "http://localhost:8100/api/cashbook?key=4280873" | jq . | head -5

# 2. 환경변수 노출 확인 (없어야 정상)
PID=$(systemctl --user show -p MainPID --value cashbook.service)
sudo cat /proc/$PID/environ | tr '\0' '\n' | grep CASHBOOK_API_KEY
# 예상: 여전히 노출됨 (ExecStart에서 export 사용)

# 3. CREDENTIALS_DIRECTORY 확인
sudo ls -la /proc/$PID/fd/ | grep credentials
```

**문제 발견:** `export CASHBOOK_API_KEY=...` 방식은 여전히 환경변수에 노출

#### 1.4 앱 코드 수정 (완전한 해결책, 20분)

**cashbook/main.py 수정:**
```python
# Before
import os
API_KEY = os.getenv("CASHBOOK_API_KEY")

# After
import os
from pathlib import Path

def load_api_key() -> str:
    """Load API key from credential file or env."""
    creds_dir = os.getenv("CREDENTIALS_DIRECTORY")
    if creds_dir:
        key_file = Path(creds_dir) / "cashbook_key"
        if key_file.exists():
            return key_file.read_text().strip()
    # Fallback to env
    return os.getenv("CASHBOOK_API_KEY", "")

API_KEY = load_api_key()
```

**systemd 서비스 재수정:**
```ini
[Service]
ExecStartPre=/opt/projects/server/scripts/deploy/kv-to-credential.sh CASHBOOK-API-KEY /run/user/1000/credentials/cashbook_key
LoadCredential=cashbook_key:/run/user/1000/credentials/cashbook_key
ExecStart=/usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8100
# export 제거!
```

---

### Task 2: 다른 서비스 전환 계획 (30분)

#### 2.1 전환 우선순위

| 서비스 | 민감도 | 코드 수정 필요 | 우선순위 |
|--------|--------|---------------|----------|
| postgres | High | Yes (POSTGRES_PASSWORD_FILE 지원) | 1 |
| webobsidian | High | Yes (마스터 비밀번호) | 2 |
| fastapi | Medium | Yes | 3 |
| mcp | Medium | Yes | 4 |
| cashbook | Low | Yes (완료) | ✅ |

#### 2.2 postgres 전환 (참고)

PostgreSQL은 `POSTGRES_PASSWORD_FILE` 환경변수 지원:

```ini
# containers/devforge-postgres.container
[Service]
ExecStartPre=/opt/projects/server/scripts/deploy/kv-to-credential.sh POSTGRES-PASSWORD /run/user/1000/credentials/postgres_pw
Environment=POSTGRES_PASSWORD_FILE=/run/credentials/postgres_pw
# Quadlet이 자동으로 /run/credentials/로 마운트
```

---

### Task 3: 문서화 (20분)

```markdown
# docs/security/secret-injection-hardening.md

## 개요
- 배경: 2026-09-19 WebObsidian EnvironmentFile quoting 버그
- 목표: 환경변수 노출 제거

## 전환 방법
1. ExecStartPre에서 credential 파일 생성
2. LoadCredential= 로 systemd에 등록
3. 앱 코드에서 $CREDENTIALS_DIRECTORY/filename 읽기

## 전환 완료 서비스
- [x] cashbook (2026-09-21)
- [ ] postgres
- [ ] webobsidian
- [ ] fastapi
- [ ] mcp

## 롤백 절차
1. 기존 서비스 정의 복원
2. systemctl --user daemon-reload
3. systemctl --user restart <service>
```

---

## 검증 체크리스트

- [ ] cashbook 서비스 정상 동작
- [ ] `/proc/<pid>/environ`에 시크릿 없음
- [ ] API 응답 정상
- [ ] 재시작 후에도 정상 동작
- [ ] 문서화 완료
- [ ] Git 커밋

---

## Git 커밋

```bash
cd /opt/projects/server
git add \
  .config/systemd/user/cashbook.service \
  cashbook/main.py \
  scripts/deploy/kv-to-credential.sh \
  docs/security/secret-injection-hardening.md

git commit -m "security(cashbook): migrate to LoadCredential for secret injection

Changes:
- Add kv-to-credential.sh: fetch KV secret to file
- Update cashbook.service: use LoadCredential= instead of EnvironmentFile
- Update cashbook/main.py: read from CREDENTIALS_DIRECTORY
- Remove secret from process environment variables

Security improvement:
- Before: secret visible in /proc/<pid>/environ
- After: secret only in credential file (600 permissions)

Next: postgres, webobsidian, fastapi, mcp (planned)

Origin: refactoring-roadmap.md §5.1 (WebObsidian incident 2026-09-19)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## 리스크 및 완화

| 리스크 | 완화 방안 |
|--------|-----------|
| 앱 코드 수정 실패 | 단계적 전환 (cashbook → postgres → ...) |
| LoadCredential 미지원 | systemd 버전 확인, 필요 시 수동 파일 전달 |
| 재시작 시 credential 손실 | ExecStartPre에서 매번 재생성 |

---

**다음 단계:** postgres, webobsidian 전환 (별도 작업)
