# Option 4: 일반 서버 관리 가이드 (요약)

**작성일:** 2026-09-21  
**대상:** AI 에이전트 또는 시스템 관리자  
**소요 시간:** 30분  
**난이도:** Low  
**목적:** 일상적인 서버 상태 점검 및 유지보수

---

## 빠른 시작 (5분 체크)

```bash
# 1. 시스템 리소스
uptime && free -h && df -h /

# 2. 서비스 상태
systemctl --user list-units --type=service --state=failed

# 3. 최근 에러
journalctl --user --since "1 hour ago" --priority=err -n 10

# 4. 디스크 사용률
df -h | awk '$5 > 85 {print}'

# 5. 최근 백업
ls -lht /opt/ai_data/backups/db/devforge_*.dump | head -1
```

---

## 체크리스트 (30분 전체)

### 1. 시스템 리소스 (5분)
- CPU 사용률 < 80%
- 메모리 사용률 < 90%
- Load average < CPU 코어 수

### 2. systemd 서비스 (5분)
- cashbook, devforge-fastapi, mcp, postgres, webobsidian 상태 확인
- 실패한 서비스 재시작

### 3. 디스크 용량 (5분)
- 파일시스템 < 85%
- inode 사용률 확인
- 필요 시 로그/임시 파일 정리

### 4. 로그 확인 (5분)
- systemd 로그: 최근 1시간 에러
- 애플리케이션 로그: tail -50
- OOM 확인: dmesg -T | grep "out of memory"

### 5. 업데이트 체크 (5분)
- 시스템 패키지: sudo dnf check-update
- Python 패키지: pip list --outdated
- 컨테이너 이미지 날짜 확인

### 6. 백업 상태 (5분)
- PostgreSQL 백업 < 24시간
- Git 커밋 상태 확인

---

## 상세 가이드

전체 상세 가이드는 다음 문서 참조:
- **docs/runbooks/daily-server-maintenance.md** (12개 섹션, 완전한 가이드)

---

## 자동화 스크립트

```bash
# 빠른 헬스 체크
/opt/projects/server/scripts/health-check.sh

# 또는 수동 실행:
bash << 'SCRIPT'
echo "=== DevForge Health Check ==="
echo "Time: $(date)"
echo "Load: $(uptime | awk -F'load average:' '{print $2}')"
echo "Memory: $(free -h | awk 'NR==2 {print $3 "/" $2}')"
echo "Disk: $(df -h / | awk 'NR==2 {print $5}')"
systemctl --user list-units --type=service --state=failed || echo "All services OK"
journalctl --user --since "1 hour ago" --priority=err -n 5 || echo "No errors"
ls -lht /opt/ai_data/backups/db/devforge_*.dump | head -1 | awk '{print "Backup:", $6, $7, $8}'
echo "=== Check Complete ==="
SCRIPT
```

---

## 문제 발생 시 대응

### 서비스 다운
```bash
journalctl --user -u <service>.service -n 100
systemctl --user restart <service>.service
```

### 디스크 풀
```bash
sudo journalctl --vacuum-size=100M
rm -rf /tmp/* /var/tmp/*
podman system prune -af --volumes
```

### 메모리 누수
```bash
ps aux --sort=-%mem | head -11
systemctl --user restart <service>.service
```

---

## 완료 체크리스트

- [ ] 시스템 리소스 정상
- [ ] 모든 서비스 running
- [ ] 에러 로그 없음
- [ ] 디스크 용량 충분
- [ ] 최근 백업 존재

---

**참고 문서:**
- docs/runbooks/daily-server-maintenance.md (상세 가이드)
- docs/OPERATIONS_GUIDE.md (운영 가이드)

**소요 시간:** 5분 (빠른 체크) ~ 30분 (전체 체크)
