# 런북 — DataImpulse 사용량 모니터 (공식 API)
# > Status: active · Date: 2026-09-23 · Owner: devforge · Related: `lib/dataimpulse_monitor.py`, `docs/handover-secrets-kv.md`
# DataImpulse Gateway 공식 API로 사용량을 실측하고 로컬 트래픽 추적과 비교한다.

---

## 0. 트리거 (언제)

- **주기**: `pipeline.py` — toki31 챕터 저장 시마다(`_check_dataimpulse_usage()`)
- **수동**:
  ```bash
  python3 -c "from lib.dataimpulse_monitor import check_dataimpulse_sync; print(check_dataimpulse_sync())"
  ```
- API 응답 <1초이므로 매 화 호출해도 비용 없음 (과거 Playwright 스크래핑 60초/10화 폐기)

---

## 1. 현황

- **목적**: DataImpulse 실제 과금 트래픽(API)을 일일 한도의 **SSOT**로 사용하고, 로컬 응답바디 추적(traffic_guard)은 폴백/실시간 추정으로 병행
- **API Base**: `https://gw.dataimpulse.com:777`
- **엔드포인트**: `/api/stats`, `/api/stats_with_history`
- **인증**: Basic Auth (프록시 자격증명과 동일 user/pass)
- **실측 갭** (2026-09-23, 격리 테스트 20화): API/TG ≈ **0.75~0.86** (API < TG). TG가 압축 해제 본문까지 계상. 과거 +37%는 구 monitor의 프록시 자체 트래픽 때문이었음.
- **한도 판정**: `effective = API 오늘 실측값` 우선, 없으면 `TG×보정계수` 폴백 (`used_mb_source` = `api`|`tg_calibrated`)
- **보정계수**: 정당한 일 단위 변동 → **가장 최근 완료된 수집일 총량**(API total / TG total) 기준 **1일 1회** EWMA(α=0.3) 갱신. 단구간(수십 화) 비단조·청크 노이즈는 사용하지 않음
- **API 한계**: `traffic_used`/오늘값은 **비단조·청크 갱신**(~1.5MB) → per-chapter/단구간 델타는 신뢰 불가, **일일 누적만** 사용


---

## 2. 시크릿 (Azure Key Vault)

> SSOT: `/opt/projects/server/docs/handover-secrets-kv.md` §12 (KV/SP), §13 (최소 주입)

| 항목 | 값 |
|------|-----|
| KV | `kv-common-prod-krc` (tenant `9ec65251`, sub `a942e898`) |
| 읽기 SP | `DevForge-llm-Qwen` (`abc5aab0-5394-46e0-bf4d-daf4129d1d78`, get/list) |
| 쓰기 SP | `sp-aiagent-rbac-prod-krc` (`fcf857e3-...`, get/list/**set**) |
| 시크릿 이름 | `DATAIMPULSE-API-KEY` (login), `DATAIMPULSE-PROXY-KEY` (user:pass@host:port 결합형) |

> 2026-09-23 정리: `DATAIMPULSE-LOGIN/PASS/HOST/PORT`는 purge됨(결합형으로 충분). MaskProxy도 `MASKPROXY-PROXY-KEY/API-KEY`만 유지.

**코드가 읽는 env** (KV 하이픈 → 밑줄 변환, `kv-fetch-env.py` 주입):
```
DATAIMPULSE_API_KEY, DATAIMPULSE_PROXY_KEY
```
- 우선순위: `DATAIMPULSE_PROXY_KEY`(결합형) → `DATAIMPULSE_API_KEY`/`DATAIMPULSE_LOGIN`/`DATAIMPULSE_USER` → `.env.local`(로컬 전용 템플릿)
- **서버 하드코딩 금지**. `.env.local`은 템플릿이며 실제 값은 KV로만 주입한다.

### 시크릿 등록 (쓰기 SP 사용, 값 미출력)
```bash
# 쓰기 SP로 로그인 (값은 KV에서 읽어 사용, 화면 출력 금지)
CID=$(az keyvault secret show --vault-name kv-common-prod-krc --name AZ-20137133-SP-AIAGENT-CLIENT-ID --query value -o tsv)
CSEC=$(az keyvault secret show --vault-name kv-common-prod-krc --name AZ-20137133-SP-AIAGENT-CLIENT-SECRET --query value -o tsv)
az login --service-principal -u "$CID" -p "$CSEC" --tenant 9ec65251-a106-4dc3-9878-4278caa80b1b --allow-no-subscriptions -o none

# 시크릿 등록 (kv-safe.py 권장: 값 미출력)
DATAIMPULSE_API_KEY='...' /opt/projects/server/scripts/deploy/kv-safe.py set-from-env kv-common-prod-krc DATAIMPULSE-API-KEY DATAIMPULSE_API_KEY
# 또는 파일 경유
/opt/projects/server/scripts/deploy/kv-safe.py set-from-file kv-common-prod-krc DATAIMPULSE-API-KEY /path/to/value
```

### 서비스 주입 (이미 구성됨)
`~/.config/systemd/user/ebook-watcher.service`는
`kv-fetch-env.py ... --keys DATAIMPULSE-*,VERCEL-*,BRAVE-*` 로 실행된다.
`ebook-api.service`는 `--keys DATAIMPULSE-*,VERCEL-*,BRAVE-*,EBOOK-*`.
새 `DATAIMPULSE-*` 등록 시 `--keys DATAIMPULSE-*` 에 자동 포함된다.

---

## 3. 절차

### 3-1. 수동 확인 (KV 주입 하에)
```bash
/opt/projects/server/scripts/deploy/kv-fetch-env.py \
  python3 -c "from lib.dataimpulse_monitor import check_dataimpulse_sync; print(check_dataimpulse_sync())" \
  --keys DATAIMPULSE-*
```

### 3-2. 직접 API 호출 (디버깅)
```bash
# Basic Auth. 값은 KV 주입 환경변수에서 온다(하드코딩 금지).
http -a "$DATAIMPULSE_API_KEY:$DATAIMPULSE_PASS" \
  --verify=no --timeout=10 GET https://gw.dataimpulse.com:777/api/stats
```

### 3-3. 자동 (파이프라인)
`pipeline.py`의 `_check_dataimpulse_usage()`가 toki31 저장 성공마다 호출.
- 갱신: EWMA 보정 계수, SPC 상태, `dataimpulse_api_state.json` 비교 이력

---

## 4. 검증

### 4-1. 로그
```bash
journalctl --user -u ebook-watcher.service -n 200 | grep -E "DataImpulse|SPC|보정계수"
```
- 기대: `📊 DataImpulse API 비교: API=.. TG=.. 차이=..%` + `SPC 갱신: ...`
- 일일 보정: `보정계수 갱신(수집일 YYYY-MM-DD): API=.. / TG=.. = ..`
- 경고: `⚠️ TG-API 격차 큼`, `⚠️ SPC Rule 1/2`, `⚠️ SPC Rule 3 ... 경고만(autostop=off)`
- 비상(autostop=on일 때만): `🚨 SPC Rule 3 ... 자동중지 플래그 설정`

### 4-2. 상태 파일
```bash
python3 -m json.tool /opt/ai_data/flaresolverr/ebook_watcher/dataimpulse_api_state.json | head
cat /opt/ai_data/flaresolverr/ebook_watcher/traffic_state.json
python3 -c "import sys;sys.path.insert(0,'/opt/workspace/minihome/apps/ebooklib/apps/backend');from lib.traffic_guard import summary_calibrated;print(summary_calibrated())"
```

---

## 5. 보정계수 & 이상 감지 (SPC)

### 보정계수 (API/TG)
- **갱신**: 가장 최근 **완료된 수집일**(`traffic_state.json.prev_day`)의 `API total / TG total`로 **1일 1회** EWMA(α=0.3).
- 같은 수집일에 중복 갱신하지 않음(`dataimpulse_api_state.json.calibration_last_day`).
- 하루 수집이 없으면 기준이 그대로 유지(이전 기록 사용).
- 기본값 0.85, 클램프 **[0.5, 3.0]** (API < TG도 정당).

### SPC (중심선 = 이동 EWMA)
| 규칙 | 조건 | 액션 |
|------|------|------|
| Rule 1 | raw 가 현재 EWMA ±20% 밖 | Warning |
| Rule 2 | EWMA 대비 ±5% 데드밴드 밖 5연속 | Warning |
| Rule 3 | 경고 누적 4 | **WARNING만** (자동중지 기본 OFF) |

- 자동중지 활성화(선택): `EBOOK_CALIBRATION_AUTOSTOP=1` 환경변수 + 서비스 재시작 → Rule 3 시 `calibration_emergency_stop=true`, 파이프라인 중지
- **왜 기본 OFF**: 보정계수는 일 단위로 정당하게 이동하므로 잘못된 자동중지가 파이프라인을 반복 정지시켰음(2026-09-23)

**복구** (autostop 사용 중 중지된 경우):
```bash
python3 -c "import sys;sys.path.insert(0,'/opt/workspace/minihome/apps/ebooklib/apps/backend');from lib.traffic_guard import clear_calibration_emergency;clear_calibration_emergency()"
```

---

## 6. 롤백

```bash
# 일일 한도 SSOT 롤백: traffic_guard.effective_used_bytes()에서 API 우선 제거 → TG×보정계수만
# 모니터 중단: pipeline.py `_refresh_dataimpulse_periodic()` / `_check_dataimpulse_usage()` 호출 주석
```

- **백업**: `/opt/ai_data/flaresolverr/ebook_watcher/traffic_state.json` 유지 권장
- **유일한 되돌리기**: traffic_state.json 백업 복원

---

## 7. 주의

- 과거 Playwright 대시보드 스크래핑은 **폐기** — 대시보드는 웹 로그인 세션 필요(프록시 인증과 무관)하여 자동화 불가였음. 공식 API로 대체.
- 시크릿은 **KV 단일 소스**. `.env.local`에 실제 값을 커밋하지 않는다. 값 조회/비교는 `kv-safe.py` 사용(값 미출력).
- KV 시크릿 값은 저장/조회 시 개행이 공백으로 치환됨(PEM 주의) — 일반 API 키는 영향 없음.
- 일일 한도(200 MB, `EBOOK_DAILY_TRAFFIC_LIMIT_MB`)는 **API 오늘 실측값** 기준으로 판정된다(없으면 TG×보정계수 폴백).
- API 카운터는 **비단조·청크 갱신** → per-chapter/단구간 갭 측정에 쓰지 말 것(일일 누적만).
