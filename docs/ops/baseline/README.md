# W1 Daily Baseline Timer

> **Status:** ready · **Date:** 2026-09-14
> **용도:** W1 D2-D7 일일 baseline 자동 측정

## 사용법

```bash
# 1. 서비스 파일 설치
cp scripts/hooks/baseline-daily.service /home/opc/.config/systemd/user/

# 2. 타이머 설치
cp scripts/hooks/baseline-daily.timer /home/opc/.config/systemd/user/

# 3. 활성화
systemctl --user daemon-reload
systemctl --user enable --now baseline-daily.timer

# 4. 상태 확인
systemctl --user status baseline-daily.timer

# 5. 수동 실행
systemctl --user start baseline-daily.service
```

## 서비스 파일

### baseline-daily.service
```ini
[Unit]
Description=DevForge W1 Daily Baseline Measurement
After=devforge-turn-watcher.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/projects/server/scripts/hooks/baseline-daily.py
WorkingDirectory=/opt/projects/server
User=opc
Environment=PATH=/usr/local/bin:/usr/bin:/bin
```

### baseline-daily.timer
```ini
[Unit]
Description=DevForge W1 Daily Baseline Timer
Requires=baseline-daily.service

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

## 측정 일정

| 날짜 | 파일 | 상태 |
|---|---|---|
| W1 D1 | `docs/ops/baseline/2026-09-14.json` | ✅ 완료 |
| W1 D2 | `docs/ops/baseline/2026-09-15.json` | 🔄 D2 자동 생성 |
| W1 D3 | `docs/ops/baseline/2026-09-16.json` | 🔄 D3 자동 생성 |
| W1 D4 | `docs/ops/baseline/2026-09-17.json` | 🔄 D4 자동 생성 |
| W1 D5 | `docs/ops/baseline/2026-09-18.json` | 🔄 D5 자동 생성 |
| W1 D6 | `docs/ops/baseline/2026-09-19.json` | 🔄 D6 자동 생성 |
| W1 D7 | `docs/ops/baseline/2026-09-20.json` | 🔄 D7 자동 생성 |

## D1 결과 (측정 완료)

| 지표 | 값 | 판정 |
|---|---|---|
| Gate 1: 계약 검증 | diff=0 | ✅ |
| Gate 6: provenance | 7163건 legacy:pre-2026-09 + 코드 수정 | ✅ |
| turns/24h | 143 | 기록 |
| turns.source | legacy:pre-2026-09 (100%) | ✅ 마커 적용 |
| netdata | active | ✅ |
| MCP health | 200 OK | ✅ |
