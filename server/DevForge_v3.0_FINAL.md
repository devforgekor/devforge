# DevForge v3.0 Final — 실행 계획서 (수정본, Historical)

**버전**: 3.0 Final (Corrected)
**대상**: OCI ARM 인스턴스, OL9.7, Rootless Podman 5.6, SELinux enforcing
**예상 소요 시간**: ~30분
**작성일**: 2026-05-11

---

## 실제 시스템 상태

```
ocivolume VG (44.5G, 0 free)          datavg VG (150G, 20G free)
├── root      20G  → /                ├── lv_db       30G  → /mnt/lv_db
├── lv_logs   10G  → /var/log         ├── lv_ai_data 100G  → /opt/ai_data
├── lv_tmp    10G  → /var/tmp         └── [FREE]      20G  ← 신규 LV 생성 가능
└── lv_dev   4.5G  → /opt/project     

현재 /opt/project 내용: server, seedling, litellm, common-lib, aider-env
```

**관리 방식 (현재 최신)**: Quadlet 기반 systemd user units (`~/.config/containers/systemd/`)
**참고**: 본 문서는 초기 마이그레이션 기록(Historical)이며, 최신 운영 정책은 `CLAUDE.yaml`/`handover.yaml`을 우선한다.

현재 실행 중인 유닛:
```
pod-data-pod.service  + container-postgres.service       → data-pod
pod-ai-pod.service    + container-devforge-llm.service   → ai-pod
                      + container-litellm.service
```

---

## 목표

| # | 내용 |
|---|------|
| 1 | `datavg` 여유 공간(20G)에서 새 10GB LV 생성 → `/opt/projects` |
| 2 | `ocivolume/lv_dev`(4.5G)를 `/opt/project`에서 분리 → `/mnt/secure_meta`로 재구성 |
| 3 | `/opt/project` 내용 전체 → `/opt/projects`로 이전 |
| 4 | DB 덤프 자동화: `pg_dump` + `gzip` + 7일 보관 + 무결성 검증 |
| 5 | systemd 유닛 + 컨테이너 마운트 경로 신규 구조에 맞게 갱신 |
| 6 | 호스트 Netdata 유지 (이미 설치됨, v2.2.6) |

---

## 최종 목표 구조

| 마운트 포인트 | LV | 크기 | 용도 |
|---------------|-----|------|------|
| `/opt/projects` | `datavg/lv_projects` (신규) | 10GB | 소스코드, Git 저장소 |
| `/mnt/secure_meta` | `ocivolume/lv_dev` (재활용) | 4.5GB | configs/ secrets/ snapshots/ tmp/ |
| `/opt/ai_data` | `datavg/lv_ai_data` | 100GB | GGUF 모델, 캐시 |
| `/mnt/lv_db` | `datavg/lv_db` | 30GB | PostgreSQL 클러스터 |

---

## 0단계: 사전 준비

```bash
# 0.1 현재 상태 기록
df -h | grep -E "/opt|/mnt"
sudo vgs
sudo lvs
podman ps --pod
ls -la /opt/project/

# 0.2 백업
sudo tar -czf /var/tmp/backup-opt-project-$(date +%Y%m%d).tar.gz -C /opt/project .
tar -czf ~/backup-systemd-units-$(date +%Y%m%d).tar.gz -C ~/.config/systemd/user container-*.service pod-*.service

# 0.3 SELinux 도구 확인
sudo dnf install -y policycoreutils-python-utils 2>/dev/null || true

# 0.4 linger 확인 (이미 설정됨)
loginctl show-user opc | grep Linger
```

---

## 1단계: 신규 10GB LV 생성 → `/opt/projects`

```bash
# 1.1 datavg에서 LV 생성
sudo lvcreate -n lv_projects -L 10G datavg

# 1.2 XFS 포맷
sudo mkfs.xfs /dev/datavg/lv_projects

# 1.3 마운트 포인트 생성 및 마운트
sudo mkdir -p /opt/projects
sudo mount /dev/datavg/lv_projects /opt/projects

# 1.4 권한 설정
sudo chown opc:opc /opt/projects

# 1.5 fstab 등록
UUID=$(sudo blkid -s UUID -o value /dev/datavg/lv_projects)
echo "UUID=$UUID /opt/projects xfs defaults,noatime,nofail 0 0" | sudo tee -a /etc/fstab
```

---

## 2단계: 프로젝트 이전 (`/opt/project` → `/opt/projects`)

```bash
# 2.1 Pod 중지 (마운트 변경 전 필수)
systemctl --user stop container-litellm.service container-devforge-llm.service pod-ai-pod.service
systemctl --user stop container-postgres.service pod-data-pod.service

# 2.2 전체 복사
sudo rsync -avh --progress /opt/project/ /opt/projects/

# 2.3 소유권 재확인
sudo chown -R opc:opc /opt/projects

# 2.3b SELinux 컨텍스트 (신규 마운트 포인트)
sudo semanage fcontext -a -t container_file_t "/opt/projects(/.*)?" 2>/dev/null || true
sudo restorecon -Rv /opt/projects

# 2.4 심볼릭 링크 (하위 호환)
sudo ln -sf /opt/projects /opt/project
```

---

## 3단계: `lv_dev` 재구성 → `/mnt/secure_meta`

```bash
# 3.1 기존 마운트 해제 (심볼릭 링크 너머의 실제 마운트)
sudo umount /opt/project

# 3.2 새 마운트 포인트 생성
sudo mkdir -p /mnt/secure_meta
sudo mount /dev/ocivolume/lv_dev /mnt/secure_meta

# 3.3 디렉토리 구조
sudo mkdir -p /mnt/secure_meta/{configs,secrets,snapshots,tmp}
sudo chmod 700 /mnt/secure_meta/secrets
sudo chmod 755 /mnt/secure_meta/{configs,snapshots,tmp}
sudo chown -R opc:opc /mnt/secure_meta/{configs,snapshots,tmp}
sudo chown root:opc /mnt/secure_meta/secrets

# 3.4 SELinux 영구 컨텍스트
sudo semanage fcontext -a -t container_file_t "/mnt/secure_meta(/.*)?" 2>/dev/null || true
sudo restorecon -Rv /mnt/secure_meta

# 3.5 fstab 갱신
sudo sed -i '\|/dev/mapper/ocivolume-lv_dev|s|/opt/project|/mnt/secure_meta|' /etc/fstab
sudo mount -a
```

---

## 4단계: 컨테이너 경로 갱신 및 서비스 재시작

### 4.1 systemd 유닛 파일 수정

litellm 컨테이너는 `/opt/project/litellm/config.yaml`을 마운트합니다.
이제 실제 경로는 `/opt/projects/litellm/config.yaml`입니다.

```bash
# container-litellm.service 수정
sed -i 's|/opt/project/litellm|/opt/projects/litellm|g' \
  ~/.config/systemd/user/container-litellm.service

# daemon-reload
systemctl --user daemon-reload
```

### 4.2 서비스 재시작

```bash
# data-pod 먼저
systemctl --user start pod-data-pod.service
sleep 5
systemctl --user status pod-data-pod.service --no-pager | grep Active

# ai-pod + LLM + litellm
systemctl --user start pod-ai-pod.service
echo "모델 로딩 대기 중..."
```

### 4.3 모델 로딩 확인

```bash
for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:4000/v1/models -H "Authorization: Bearer devforge-litellm-key" 2>/dev/null; then
    echo "전체 스택 준비 완료"
    break
  fi
  [ $i -eq 60 ] && echo "ERROR: LiteLLM did not become ready within 180s" && exit 1
  sleep 3
done
```

---

## 5단계: DB 덤프 자동화

### 5.1 `.pgpass` 생성

```bash
cat > ~/.pgpass << 'EOF'
localhost:5432:litellm:postgres:devforge_secret_2026
EOF
chmod 600 ~/.pgpass
```

### 5.2 덤프 스크립트

```bash
sudo tee /usr/local/bin/dump_postgres.sh << 'SCRIPT'
#!/bin/bash
set -e
export XDG_RUNTIME_DIR=/run/user/$(id -u)

DUMP_DIR="/mnt/secure_meta/snapshots"
DATE=$(date +%Y%m%d_%H%M%S)
CONTAINER="postgres"
DB="litellm"
USER="postgres"
RETENTION_DAYS=7

# 7일 초과 삭제 → 공간 확보
find "$DUMP_DIR" -name "db_*.sql.gz" -mtime +${RETENTION_DAYS} -delete 2>/dev/null || true

# pg_dump (--no-owner --no-acl: rootless 복원 시 소유권 충돌 방지)
podman exec -i "$CONTAINER" pg_dump -U "$USER" --no-owner --no-acl "$DB" \
  | gzip > "$DUMP_DIR/db_${DATE}.sql.gz"

# 무결성 검증
if gzip -t "$DUMP_DIR/db_${DATE}.sql.gz" 2>/dev/null; then
    logger -t pg-dump "OK: db_${DATE}.sql.gz ($(du -h "$DUMP_DIR/db_${DATE}.sql.gz" | cut -f1))"
else
    logger -t pg-dump "FAIL: db_${DATE}.sql.gz corrupted, removed"
    rm -f "$DUMP_DIR/db_${DATE}.sql.gz"
    exit 1
fi
SCRIPT
sudo chmod +x /usr/local/bin/dump_postgres.sh
```

### 5.3 systemd timer 활성화

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now devforge-backup.timer
```

### 5.4 수동 테스트

```bash
/usr/local/bin/dump_postgres.sh
ls -lh /mnt/secure_meta/snapshots/
```

### 5.5 journald 로그 보존 기간 설정

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
cat << 'EOF' | sudo tee /etc/systemd/journald.conf.d/60-retention.conf
[Journal]
MaxRetentionSec=30day
EOF
sudo systemctl restart systemd-journald
```

```bash
# 확인: pg-dump 로그 (30일간 보존)
journalctl -t pg-dump --no-pager -n 5
```

---

## 6단계: 검증

```bash
# 6.1 Pod/컨테이너 상태
podman ps --pod
podman pod ls

# 6.2 마운트 확인
df -h /mnt/secure_meta /opt/projects /opt/ai_data /mnt/lv_db

# 6.3 LiteLLM API
curl -sf http://127.0.0.1:4000/v1/models -H "Authorization: Bearer devforge-litellm-key"

# 6.4 PostgreSQL
podman exec postgres pg_isready -U postgres

# 6.5 E2E 추론
curl -s -X POST http://127.0.0.1:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer devforge-litellm-key" \
  -d '{"model":"qwen2.5-coder-7b","messages":[{"role":"user","content":"1+1="}],"max_tokens":10}' \
  | jq -r '.choices[0].message.content'

# 6.6 덤프 디렉토리
ls -lh /mnt/secure_meta/snapshots/

# 6.7 Netdata
curl -sf http://127.0.0.1:19999/api/v1/info | jq -r '.version'
```

---

## 7단계: 덤프 복원 테스트 (월 1회, systemd timer)

```bash
sudo tee /usr/local/bin/test_dump_restore.sh << 'SCRIPT'
#!/bin/bash
set -e
export XDG_RUNTIME_DIR=/run/user/$(id -u)

CONTAINER="postgres"
DUMP=$(ls -t /mnt/secure_meta/snapshots/db_*.sql.gz 2>/dev/null | head -1)

if [ -z "$DUMP" ]; then
    logger -t dump-test "No dump found to test"
    exit 1
fi

DB_TEST="test_restore_$(date +%m%d)"

podman exec -i "$CONTAINER" psql -U postgres -c "DROP DATABASE IF EXISTS $DB_TEST;" 2>/dev/null || true
podman exec -i "$CONTAINER" psql -U postgres -c "CREATE DATABASE $DB_TEST;"

gunzip -c "$DUMP" | podman exec -i "$CONTAINER" psql -U postgres -d "$DB_TEST"

TABLE_COUNT=$(podman exec -i "$CONTAINER" psql -U postgres -d "$DB_TEST" -t -c \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" | tr -d ' ')

podman exec -i "$CONTAINER" psql -U postgres -c "DROP DATABASE IF EXISTS $DB_TEST;"

logger -t dump-test "Restore OK: $DUMP → ${DB_TEST} (${TABLE_COUNT} tables)"
echo "Restore OK: ${TABLE_COUNT} tables"
SCRIPT
sudo chmod +x /usr/local/bin/test_dump_restore.sh

sudo tee /etc/systemd/system/devforge-restore-test.service << 'SCRIPT'
[Unit]
Description=DevForge Monthly PostgreSQL Restore Test
Wants=devforge-backup.service
After=devforge-backup.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/test_dump_restore.sh
User=opc
SCRIPT

sudo tee /etc/systemd/system/devforge-restore-test.timer << 'SCRIPT'
[Unit]
Description=DevForge Monthly PostgreSQL Restore Test Timer (04:13 UTC)

[Timer]
OnCalendar=*-*-01 04:13:00
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
SCRIPT

sudo systemctl daemon-reload
sudo systemctl enable --now devforge-restore-test.timer
```

---

## 롤백 절차

```bash
# 1. 서비스 중지
systemctl --user stop container-litellm.service container-devforge-llm.service pod-ai-pod.service
systemctl --user stop container-postgres.service pod-data-pod.service

# 2. fstab 복원
sudo cp /etc/fstab /etc/fstab.v3-backup
# /etc/fstab에서 lv_projects 행 주석 처리
# /mnt/secure_meta 행을 원래 /opt/project로 복원
sudo sed -i '/lv_projects/s/^/#/' /etc/fstab
sudo sed -i 's|/mnt/secure_meta|/opt/project|' /etc/fstab

# 3. 마운트 복구
sudo umount /mnt/secure_meta
sudo mount /opt/project

# 4. systemd 유닛 복원
tar -xzf ~/backup-systemd-units-*.tar.gz -C ~/.config/systemd/user/
systemctl --user daemon-reload

# 5. 서비스 재시작
systemctl --user start pod-data-pod.service
systemctl --user start pod-ai-pod.service
```

---

## 실행 체크리스트

```
[ ] 0단계: 백업 완료 (opt-project.tar.gz, systemd-units.tar.gz)
[ ] 1단계: datavg/lv_projects 10GB 생성, XFS 포맷, /opt/projects 마운트, fstab
[ ] 2단계: 서비스 중지 → rsync /opt/project → /opt/projects → 심볼릭 링크
[ ] 3단계: lv_dev 언마운트 → /mnt/secure_meta 마운트 → 디렉토리 구조 → SELinux → fstab
[ ] 4단계: container-litellm.service 경로 수정 → daemon-reload → 서비스 재시작
[ ] 5단계: .pgpass → dump_postgres.sh → systemd timer 활성화 → 수동 테스트
[ ] 6단계: pod ps, df, curl API, pg_isready, E2E 추론
[ ] 7단계: test_dump_restore.sh → systemd timer 활성화
[ ] 선택: sudo reboot 후 6단계 반복
```

---

## 원안 대비 주요 수정 사항

| 항목 | 원안 | 수정본 | 이유 |
|------|------|--------|------|
| 신규 LV 소스 | `ocivolume` (0 free) | `datavg` (20G free) | 실제 VG 여유 공간 |
| 4.5GB LV 위치 | `/mnt/small_vol` (존재하지 않음) | `ocivolume/lv_dev` → `/opt/project` | 실제 LVM 구성 |
| 관리 방식 | Quadlet `.container` 파일 | Quadlet `.container/.pod` 유닛 | 최신 운영 정책(2026-05-13) |
| `Pod=` | `Pod=ai-pod` | `Pod=pod-ai-pod.pod` | Quadlet 기준 참조 정합 |
| `Memory=` in `[Pod]` | 사용 | `--memory=2g` (ExecStartPre) | Quadlet `[Pod]` 미지원 키 |
| `CPUQuota=` | `CPUQuota=350000` | 해당 없음 | Quadlet `[Pod]` 미지원 키 |
| 볼륨 마운트 | `:ro` | `:Z,ro` | SELinux enforcing |
| `TimeoutStartSec` | 누락 | `TimeoutStartSec=300` (기본값으로 충분) | 모델 로딩 시간 |
| `loginctl enable-linger` | 누락 | 0.4단계에서 확인 | 이미 설정됨 |
| `PGPASSWORD` env | 스크립트 내 하드코딩 | `.pgpass` 파일 | 보안 |
| `pg_dump` 옵션 | 없음 | `--no-owner --no-acl` | rootless 복원 호환성 |
| 덤프 복원 테스트 | 없음 | `devforge-restore-test.timer` 추가 | 운영 안정성 |
| `:Z` 플래그 | `:ro`만 사용 | 모든 마운트에 `:Z` 또는 `:Z,ro` | SELinux |
