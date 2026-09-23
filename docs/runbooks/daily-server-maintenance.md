# 일반 서버 관리 작업 가이드

**작성일:** 2026-09-21  
**대상:** AI 에이전트 또는 시스템 관리자  
**소요 시간:** 약 30분  
**난이도:** Low  
**목적:** 일상적인 서버 상태 점검 및 유지보수

---

## 전제 조건

- 서버: devforge-444795 (Azure Korea Central)
- OS: Oracle Linux 9 (aarch64)
- 사용자: opc (sudo 권한)
- 주요 경로: `/opt/projects/server`

---

## 체크리스트 개요

1. ✅ 시스템 리소스 확인 (5분)
2. ✅ systemd 서비스 상태 (5분)
3. ✅ 디스크 용량 (5분)
4. ✅ 로그 확인 (5분)
5. ✅ 업데이트 체크 (5분)
6. ✅ 백업 상태 (5분)

---

## 1. 시스템 리소스 확인 (5분)

### 1.1 CPU, 메모리, 부하

```bash
# CPU 사용률 (top 5 프로세스)
ps aux --sort=-%cpu | head -6

# 메모리 사용률
free -h

# 시스템 부하 (1분, 5분, 15분 평균)
uptime

# 디스크 I/O 확인
iostat -x 1 3
```

**판단 기준:**
- CPU > 80% 지속: ⚠️ 조사 필요
- 메모리 사용률 > 90%: ⚠️ 메모리 누수 의심
- Load average > CPU 코어 수: ⚠️ 과부하

**조치:**
- 이상 발견 시: `journalctl -xe --since "1 hour ago"` 로그 확인
- 필요 시 서비스 재시작

---

## 2. systemd 서비스 상태 (5분)

### 2.1 핵심 서비스 확인

```bash
# 모든 사용자 서비스 상태
systemctl --user list-units --type=service --state=failed

# 핵심 서비스 개별 확인
for svc in cashbook devforge-fastapi mcp postgres webobsidian; do
  echo "=== $svc ==="
  systemctl --user status $svc.service --no-pager | head -10
  echo ""
done
```

**체크 항목:**
- ✅ Active: active (running) — 정상
- ⚠️ Active: failed — 재시작 필요
- ⚠️ Active: inactive (dead) — 시작 필요

**복구 명령:**
```bash
# 실패한 서비스 재시작
systemctl --user restart <service-name>.service

# 로그 확인
journalctl --user -u <service-name>.service -n 50
```

### 2.2 컨테이너 상태 (Quadlet)

```bash
# Quadlet 컨테이너 상태
podman ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# 컨테이너 리소스 사용량
podman stats --no-stream
```

**판단 기준:**
- Status: Up — 정상
- Status: Exited — ⚠️ 재시작 필요

**복구 명령:**
```bash
# 컨테이너 재시작 (systemd 방식)
systemctl --user restart devforge-postgres.service
systemctl --user restart devforge-swap.service
```

---

## 3. 디스크 용량 (5분)

### 3.1 파일시스템 사용률

```bash
# 디스크 사용률 (root는 45G boot LV)
df -h

# 대용량 디렉토리 (top 10)
du -sh /opt/projects/server/* | sort -rh | head -10
du -sh /opt/ai_data/* | sort -rh | head -10

# 자동 정리 타이머 상태 (일일, root 전용)
systemctl status root-volume-daily-clean.timer --no-pager
journalctl -u root-volume-daily-clean.service -n 5 --no-pager

# root offload bind 마운트 생존 확인 (14곳 + opencode 이전 후 15곳)
findmnt -n -o TARGET | grep -E 'system-savings|/opt/netdata|\.cache|opencode' | head -20
```

**판단 기준:**
- 사용률 > 85%: ⚠️ 정리 필요 (타이머가 자동 경고도 발송)
- 사용률 > 95%: 🚨 긴급 정리

**정리 대상:**
```bash
# 자동 정리가 이미 수행 (sandbox tmp, dnf, pip, journal 200M)
sudo /usr/local/sbin/root-volume-daily-clean.sh

# 임시 파일 (수동 보조)
find /tmp -name "claude-*" -mtime +7 -ls
find /var/tmp -name "*.tmp" -mtime +7 -ls

# /var/tmp bulk (cache/dnf, temp clone, playwright profile 등 재생성 가능분)
sudo rm -rf /var/tmp/cache/dnf /var/tmp/cache/uptrack /var/tmp/cache/PackageKit
sudo rm -rf /var/tmp/playwright_chromiumdev_profile-* /var/tmp/opencode-src /var/tmp/webobsidian-src
sudo du -sh /var/tmp   # 목표 < 500MB (tor·node-compile-cache는 보존 가능)

# Docker/Podman 정리
podman system prune -af --volumes

# data LV 여유 (offload 대상이 남아 있으면 system-savings로 이동 가능)
df -h /opt/ai_data
```

### 3.2 inode 사용률

```bash
# inode 사용률 확인
df -i

# 많은 파일이 있는 디렉토리 (inode 고갈 원인)
find /opt/projects/server -type d -exec sh -c 'echo "$(find "$1" -maxdepth 1 | wc -l) $1"' _ {} \; | sort -rn | head -10
```

---

## 4. 로그 확인 (5분)

### 4.1 systemd 로그

```bash
# 최근 1시간 에러 로그
journalctl --user --since "1 hour ago" --priority=err -n 50

# 특정 서비스 최근 로그
journalctl --user -u cashbook.service -n 20
journalctl --user -u devforge-fastapi.service -n 20
```

### 4.2 애플리케이션 로그

```bash
# FastAPI 로그 (최근 50줄)
tail -50 /opt/projects/server/logs/fastapi.log

# MCP 서버 로그
tail -50 /opt/projects/server/logs/mcp_server.log

# Watchdog 로그
tail -50 /opt/projects/server/logs/watchdog.log
```

**에러 패턴 검색:**
```bash
# 최근 1시간 에러/경고
journalctl --user --since "1 hour ago" | grep -iE "error|warning|failed|exception" | tail -20

# OOM (Out of Memory) 확인
dmesg -T | grep -i "out of memory"
```

---

## 5. 업데이트 체크 (5분)

### 5.1 시스템 패키지

```bash
# 업데이트 가능한 패키지 확인
sudo dnf check-update | head -20

# 보안 업데이트 확인
sudo dnf updateinfo list security
```

**조치:**
- 보안 업데이트 있으면: 적용 권장 (다음 유지보수 윈도우)
- 일반 업데이트: 월 1회 적용

### 5.2 Python 패키지

```bash
# pip 업데이트 확인 (프로젝트 환경)
cd /opt/projects/server
python3 -m pip list --outdated | head -10
```

**조치:**
- 보안 관련 패키지 (requests, urllib3, cryptography): 즉시 업데이트
- 기타 패키지: 테스트 후 업데이트

### 5.3 컨테이너 이미지

```bash
# 로컬 이미지 목록
podman images

# 최근 빌드 날짜 확인
podman inspect --format='{{.Created}}' localhost/devforge-postgres:latest
```

**조치:**
- 이미지 > 3개월: 재빌드 검토
- 보안 취약점 발견 시: 즉시 재빌드

---

## 6. 백업 상태 (5분)

### 6.1 PostgreSQL 백업

```bash
# 최근 백업 파일 확인
ls -lht /opt/ai_data/backups/pg_dumps/ | head -5

# 백업 파일 크기 (급격한 변화 감지)
du -sh /opt/ai_data/backups/pg_dumps/*.sql.gz | tail -5
```

**판단 기준:**
- 최근 백업: < 24시간 — 정상
- 최근 백업: > 24시간 — ⚠️ 백업 실패 의심

**수동 백업:**
```bash
# PostgreSQL 수동 백업
/opt/projects/server/scripts/backup/pg_dump_daily.sh
```

### 6.2 Git 커밋 상태

```bash
# 최근 커밋 (자동 커밋 포함)
cd /opt/projects/server
git log --oneline -10

# 커밋되지 않은 변경사항
git status --short
```

**조치:**
- 미커밋 변경사항 > 10개 파일: 정리 권장
- 자동 커밋 실패: `auto_commit_guard.py` 확인

---

## 7. 정기 유지보수 작업 (선택)

### 7.1 로그 로테이션 확인

```bash
# journald 로그 크기 (상한: SystemMaxUse=200M, SystemKeepFree=2G, MaxRetentionSec=2week)
journalctl --disk-usage

# 강제 정리 (타이머가 이미 daily vacuum — 수동은 비상용)
sudo journalctl --vacuum-size=200M
# 확인: /etc/systemd/journald.conf.d/size.conf (단일 SSOT)
# 라벨 이상 시: sudo restorecon -RF /opt/netdata /home/opc/.cache …
```

### 7.2 임시 파일 정리

```bash
# tmpfs 사용량
df -h | grep tmpfs

# 오래된 임시 파일 정리
find /tmp -type f -mtime +7 -delete
find /var/tmp -type f -mtime +7 -delete
```

### 7.3 Docker/Podman 정리

```bash
# 미사용 이미지/볼륨 정리
podman system prune -f

# 정리 후 용량 확인
du -sh ~/.local/share/containers/storage/
```

---

## 8. 상태 리포트 생성

### 8.1 요약 리포트

```bash
cat > /tmp/server-health-report.txt << EOF
=== DevForge Server Health Report ===
Date: $(date)
Hostname: $(hostname)

## System Resources
Uptime: $(uptime -p)
Load: $(uptime | awk -F'load average:' '{print $2}')
Memory: $(free -h | awk 'NR==2 {print $3 "/" $2}')
Disk: $(df -h / | awk 'NR==2 {print $5 " used"}')

## Service Status
$(systemctl --user list-units --type=service --state=running | grep -E "cashbook|fastapi|mcp|postgres|webobsidian" || echo "No services running")

## Recent Errors
$(journalctl --user --since "1 hour ago" --priority=err -n 10 || echo "No errors")

## Backup Status
Latest backup: $(ls -t /opt/ai_data/backups/pg_dumps/*.sql.gz 2>/dev/null | head -1 | xargs ls -lh | awk '{print $6, $7, $8, $9}')

EOF

cat /tmp/server-health-report.txt
```

### 8.2 리포트 저장

```bash
# 리포트를 문서화
cp /tmp/server-health-report.txt /opt/projects/server/docs/reports/health-$(date +%Y%m%d).txt

# Git 커밋 (선택)
cd /opt/projects/server
git add docs/reports/health-$(date +%Y%m%d).txt
git commit -m "docs(health): daily server health report $(date +%Y-%m-%d)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## 9. 문제 발생 시 대응

### 9.1 서비스 다운

**증상:** systemctl status shows "failed"

**진단:**
```bash
# 로그 확인
journalctl --user -u <service>.service -n 100

# 설정 파일 검증
<service> --check-config  # (서비스에 따라 다름)

# 포트 충돌 확인
ss -tlnp | grep <port>
```

**복구:**
```bash
# 재시작 시도
systemctl --user restart <service>.service

# 실패 시 수동 실행으로 에러 확인
/path/to/service --verbose
```

### 9.2 디스크 풀

**증상:** df -h shows 100%

**긴급 조치:**
```bash
# 1. 로그 정리
sudo journalctl --vacuum-size=100M

# 2. 임시 파일 정리
rm -rf /tmp/* /var/tmp/*

# 3. Podman 정리
podman system prune -af --volumes

# 4. 오래된 백업 삭제 (30일 이상)
find /opt/ai_data/backups -name "*.gz" -mtime +30 -delete
```

### 9.3 메모리 누수

**증상:** free -h shows low available memory

**진단:**
```bash
# 메모리 사용량 top 10
ps aux --sort=-%mem | head -11

# 특정 프로세스 메모리 증가 추적
watch -n 5 'ps aux --sort=-%mem | head -6'
```

**복구:**
```bash
# 의심 프로세스 재시작
systemctl --user restart <service>.service

# 최후 수단: 시스템 재부팅 (사전 승인 필요)
# sudo reboot
```

---

## 10. 자동화 스크립트 (선택)

### 10.1 헬스 체크 스크립트

```bash
#!/bin/bash
# /opt/projects/server/scripts/health-check.sh

set -e

echo "=== DevForge Health Check ==="
echo "Time: $(date)"
echo ""

# CPU/Memory
echo "## Resources"
echo "Load: $(uptime | awk -F'load average:' '{print $2}')"
echo "Memory: $(free -h | awk 'NR==2 {print $3 "/" $2}')"
echo "Disk: $(df -h / | awk 'NR==2 {print $5}')"
echo ""

# Services
echo "## Services"
systemctl --user list-units --type=service --state=failed || echo "All services OK"
echo ""

# Recent errors
echo "## Recent Errors (last hour)"
journalctl --user --since "1 hour ago" --priority=err -n 5 || echo "No errors"
echo ""

echo "=== Check Complete ==="
```

### 10.2 cron 설정 (일일 체크)

```bash
# cron 작업 추가 (매일 오전 9시)
crontab -e

# 추가 라인:
0 9 * * * /opt/projects/server/scripts/health-check.sh > /tmp/health-check-$(date +\%Y\%m\%d).log 2>&1
```

---

## 11. 완료 체크리스트

작업 완료 후 아래 항목을 확인하세요:

- [ ] 시스템 리소스 정상 (CPU < 80%, Memory < 90%, Disk < 85%)
- [ ] 모든 핵심 서비스 running 상태
- [ ] 최근 1시간 내 critical 에러 없음
- [ ] 최근 백업 < 24시간
- [ ] 디스크 용량 충분 (> 15% 여유) — root는 `root-volume-daily-clean.timer`가 daily 실행 중
- [ ] root offload bind 마운트 정상 (`findmnt -n -o SOURCE | grep -c system-savings` = 15; 14 bind + opencode)
- [ ] `journalctl --disk-usage` ≤ 200M
- [ ] `/var/tmp` < 500MB (`du -sh /var/tmp` — cache/dnf·temp clone 제거 후 기준)
- [ ] 로그에 이상 패턴 없음
- [ ] (재부팅 직후) rollback-guide §6.1 체크리스트 완료 (bind·label·WebObsidian drop-in·승인 재검토)
- [ ] (선택) 상태 리포트 생성 및 저장

---

## 12. 참고 문서

- `/opt/projects/server/docs/OPERATIONS_GUIDE.md` — 운영 가이드
- `/opt/projects/server/docs/system-architecture.md` — 시스템 아키텍처 (스토리지 §5: system-savings bind)
- `/opt/projects/server/docs/operations/rollback-guide.md` — fstab bind 롤백 (§6)
- `/opt/projects/server/docs/handover-secrets-kv.md` — Key Vault 시크릿 관리

---

**작성자:** Claude Code (devforge-444795)  
**최종 업데이트:** 2026-09-23  
**버전:** 1.1
