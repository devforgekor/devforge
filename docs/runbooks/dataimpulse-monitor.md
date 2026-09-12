# 런북 — DataImpulse 대시보드 모니터
# > Status: active · Date: 2026-09-12 · Owner: devforge · Related: `specs/dataimpulse-monitor.yaml`, `reports/dataimpulse-usage-20260912.md`
# DataImpulse 대시보드 사용량 실측 및 로컬 트래픽 추적 비교 모니터링

---

## 0. 트리거 (언제)

- **주기**: `pipeline.py` — 10화마다 자동 호출
- **실행**: `python3 scripts/pipeline.py check_dataimpulse_sync`
- **수동**: `python3 -c "from lib.dataimpulse_monitor import check_dataimpulse_sync; check_dataimpulse_sync()"`
- **타이밍**: `DATAIMPULSE_CHECK_INTERVAL = 10` (collect 단계 기준)

---

## 1. 현황

- **목적**: DataImpulse 대시보드 누적 사용량(GB)과 로컬 트래픽 추적(chapter·byte)을 비교
- **데이터 흐름**: 프록시(인증 또는 화이트리스트) → 대시보드 → 사용량 파싱 → 비교 로깅
- **현재 상태** (2026-09-12 기준):
  - 누적 바이트: **48,761,672 bytes** (~46.5 MB)
  - 수집 화: **114화**
  - 일일 한도: **200 MB** (남은: ~153 MB)
  - 자동 리셋: 자정(00:00) 누적 리셋
- **프록시**: `161.33.199.207` (서버 IP) + DataImpulse 대시보드 IP 화이트리스트 필수

### 모드

| 항목 | 프록시 인증 모드 | IP 화이트리스트 모드 |
|---|---|---|
| `DATAIMPULSE_IP_WHITELIST` | 미설정 (기본) | `=1` |
| 인증 필요 | ✅ user:pass 필요 | ❌ 불필요 |
| DataImpulse 로그인 | Turnstile 필요 | 불필요 (IP 직접접속) |
| fallback 메시지 | "프록시 인증 확인 필요" | "IP 화이트리스트 확인하세요" |

---

## 2. 선행조건

1. **DataImpulse 대시보드 IP 화이트리스트 등록**
   - `https://app.dataimpulse.com/dashboard` 접속
   - **Manage Whitelist IPs** → `161.33.199.207` 추가
2. **환경변수 확인**
   ```bash
   cat ~/.config/devforge/secrets.env | grep DATAIMPULSE
   # DATAIMPULSE_USER=fa04f846bd08b59ef691
   # DATAIMPULSE_PASS=38687aa65730d426
   ```
3. **프록시 연결 테스트**
   ```bash
   curl --proxy http://gw.dataimpulse.com:823 http://ifconfig.me
   # → 161.33.199.207 반환 성공 시 정상
   ```

---

## 3. 절차

### 3-1. 자동 실행 (파이프라인)

```bash
# pipeline.py가 10화마다 자동 호출
python3 scripts/pipeline.py loop --source newtoki
# 또는 전체 루프
python3 scripts/pipeline.py all <wr_id> --source newtoki
```

### 3-2. 수동 실행

```bash
# monit.py 직접 실행
python3 -c "
import os
os.environ['DATAIMPULSE_IP_WHITELIST'] = '1'  # 또는 미설정
from lib.dataimpulse_monitor import check_dataimpulse_sync
result = check_dataimpulse_sync()
print(result)
"
```

### 3-3. 모드별 실행 시나리오

**① IP Whitelist 모드** (추천, credentials 불필요)

```bash
export DATAIMPULSE_IP_WHITELIST=1
python3 -c "from lib.dataimpulse_monitor import check_dataimpulse_sync; print(check_dataimpulse_sync())"
```
- 대시보드 IP 등록 전: 로그인 페이지 리다이렉트 → fallback + 로컬 추적 계속
- 대시보드 IP 등록 후: 사용량 파싱 → 비교 로깅

**② 프록시 인증 모드** (기본, credentials 필요)

```bash
# env vars가 .env.local에 있다면 자동 로드됨
python3 -c "from lib.dataimpulse_monitor import check_dataimpulse_sync; print(check_dataimpulse_sync())"
```
- credentials 잘못됨: 로그인 페이지 → fallback + 로컬 추적
- credentials 정상이면: 사용량 파싱 → 비교 로깅

---

## 4. 검증

### 4-1. 로그 확인

```bash
# pipelinw 실행 후 확인
tail -50 /opt/ai_data/flaresolverr/ebook_watcher/watcher.log | grep -i dataimpulse
```
- 기대 로그: `📊 DataImpulse 비교: ...`
- fallback 로그: `📊 DataImpulse fallback: ...`

### 4-2. 수동 검증 명령어

```bash
python3 -c "
import os, sys
sys.path.insert(0, '/opt/workspace/ebooklib/apps/backend')
from lib.dataimpulse_monitor import (
    _is_ip_whitelist_mode, _load_proxy_credentials,
    _proxy_url, _build_proxy_config, _parse_usage
)

# 모드 검증
print('whitelist mode:', _is_ip_whitelist_mode())

# env vars 우선순위 검증
os.environ['DATAIMPULSE_USER'] = 'override_user'
from lib.dataimpulse_monitor import _load_proxy_credentials as lpc
user, _, _, _ = lpc()
print('user after env override:', user)  # override_user여야 함

# 파싱 패턴 검증
print('_parse_usage tests:')
print('  2.0 GB used ->', _parse_usage('2.0 GB used'))
print('  0.5 GB left ->', _parse_usage('0.5 GB left'))
print('  1.5 GB ->', _parse_usage('1.5 GB'))
print('  no data ->', _parse_usage('no data'))
"
```

---

## 5. 롤백

```bash
# 1) IP whitelist 비활성화
unset DATAIMPULSE_IP_WHITELIST

# 2) 환경변수 초기화
unset DATAIMPULSE_USER DATAIMPULSE_PASS DATAIMPULSE_HOST DATAIMPULSE_PORT

# 3) 로컬 트래픽 상태 강제 리셋 (자정이 지나면 자동, 수동으로도 가능)
python3 -c "
from lib.traffic_guard import reset_if_new_day
reset_if_new_day()
print('traffic state reset for', __import__('datetime').date.today())
"
```

- **백업**: `/opt/ai_data/flaresolverr/ebook_watcher/traffic_state.json.bak_(날짜)` 유지 권장
- **유일한 되돌리기**: traffic_state.json 백업 파일 복원

---

## 6. 주의

- IP 화이트리스트는 DataImpulse 대시보드에서 **한 번만 등록**하면 지속됨
- `DATAIMPULSE_IP_WHITELIST=1`을 켜도 IP가 대시보드에 등록되어 있지 않으면 로그인 페이지로 리다이렉트됨
- 프록시 인증 모드(user:pass)는 `~/.config/devforge/secrets.env`에 상주하므로 분실하지 않도록 함
- 일일 트래픽 한도(200 MB)를 초과하면 다음 날 자정에 자동 리셋되지만, 과다 사용 시 즉시 점검 요망
- 대시보드 HTML 구조가 변경되면 `_parse_usage()` regex가 갱신 필요 (pattern: `X.XX GB left/used` 또는 `X.XX GB`)