# DevForge — OCI Object Storage (스토리지 & 파일 교환)

> 최종 갱신: 2026-09-19 (리전 청주 정정 · OCI 인증 복구 · 백업 정상화 · lifecycle 적용)
> 관련: [system-architecture.md](./system-architecture.md) · [../CLAUDE.yaml](../CLAUDE.yaml)
> 상태: 백업 파이프라인 정상화(2026-09-19) / 파일 교환(Exchange) OCI 전환 **완료** (Droplr 단축)

---

## 1. 개요

DevForge의 원격 오브젝트 스토리지는 **OCI Object Storage**를 사용한다.
용도는 두 가지다.

1. **백업 저장소** — PostgreSQL 덤프 + 애플리케이션 코드/설정 (운영 중)
2. **파일 교환(Exchange)** — 사용자 ↔ 서버 간 임시 파일 송수신 (OCI 운영 중)

파일 교환 백엔드는 **OCI Object Storage**(`devforge-standard/uploads/*`)이며,
이전 Azure Blob(`stshareddevforgeprodkrc/devforge`)은 **2026-09-11 제거**됐다.
최종 공유 주소는 **Droplr 단축 URL**로 통일한다.

- 리전: `ap-chuncheon-1` · Namespace: `axgly0lmehyp` · 컴파트먼트: 테넌시 루트
  (도쿄 `ap-tokyo-1`/`nrhe1zafhd0v`는 onmydoc 전용 — devforge는 청주 사용. 2026-09-19 정정)
- 인증: OCI API 키, 지문 `e7:58:b7:c6:59:a0:de:8c:97:41:2f:00:cc:b1:b9:08`
  - 개인키: `~/.oci/oci_api_key.pem` (chmod 600), 프로파일 `~/.oci/config` (DEFAULT, region `ap-chuncheon-1`)
  - 원본: Azure Key Vault `OCI-DEVFORGE-RSA-API-KEY` / `OCI-DEVFORGE-API-KEY-FINGERPRINT`
  - ⚠️ KV 값은 **개행이 공백으로 치환**되어 저장됨 → 사용 시 `BEGIN`/`END` 사이 base64를 공백 제거 후 64자로 재래핑해 복원해야 한다(그대로 저장하면 지문이 달라져 401)

---

## 2. 버킷과 prefix 구조

```
devforge-standard/   (Storage tier: Standard, NoPublicAccess, 2026-09-19 청주 신규 생성)
├─ backups/
│  ├─ database/        osync_backup.py  (pg_dump -Fc, 매일)
│  └─ application/     osync_backup.py  (scripts+docs+systemd units, 매주)
├─ uploads/
│  ├─ images/          파일 교환 — 이미지
│  └─ documents/       파일 교환 — 문서
├─ releases/           파이프라인 산출물 (review bundle 등)
├─ archives/           콜드/장기 보관 (추후 Archive 전환 대상)
├─ logs/               로그 스냅샷 (30일 후 자동 삭제)
└─ tmp/                임시 교환 (7일 후 자동 삭제)

devforge-archive/    (Storage tier: Archive, NoPublicAccess)  ← 콜드 전용, 청주에는 미생성
```

폴더는 실제 디렉터리가 아니라 **객체 키 prefix**다. 청주 신규 버킷은 placeholder를 만들지
않았다(prefix는 첫 객체 업로드 시 콘솔에 표시). 도쿄 시절에는 빈 폴더 표시용 0바이트
placeholder(`.../`)를 두었다(저장비 0원, API 요청 수만 소폭 증가).

> `devforge-ia` 버킷은 만들었다가 삭제했다. OCI에서 **Infrequent Access는 버킷 등급이 아니라
> 객체 등급**이므로 별도 버킷이 필요 없다. (버킷 기본 등급은 Standard/Archive만 가능)

---

## 3. 등급 · 수명주기 · 예산

### 3.1 등급 (Oracle 공식)
| Tier | 저장비 | 최소보관 | 검색료 | 접근 |
|---|---|---|---|---|
| Standard | 최고 | 없음 | 없음 | 즉시 |
| Infrequent Access | 저렴 | 31일 | 있음 | 즉시 |
| Archive | 최저 | 90일 | 복원 필요 | restore ≤1시간 |

- Standard 버킷은 객체별로 IA/Archive 혼재 가능. Archive 버킷은 Archive 객체만.
- `devforge-archive`에 뜨거운 데이터 업로드 금지(즉시 archived + restore 필요).

### 3.2 Lifecycle 규칙 (`devforge-standard`)
| 이름 | 동작 | 대상 | 기간 |
|---|---|---|---|
| `abort-incomplete-multipart-3d` | ABORT | 미완료 멀티파트 업로드 | 3일 |
| `delete-tmp-7d` | DELETE | `tmp/` | 7일 |
| `delete-logs-30d` | DELETE | `logs/` | 30일 |

- Lifecycle은 **1일 1회 실행**, 변경 반영에 최대 24시간 소요.
- 2026-09-19 적용 완료. IAM 정책 `devforge-storage-service`:
  `Allow service objectstorage-ap-chuncheon-1 to manage object-family in tenancy`
  (정책 생성 직후엔 `InsufficientServicePermissions` → 전파 후 반영됨)

### 3.3 예산
- Budget `devforge-monthly`: **USD 1 / MONTHLY**
- Alert: `ACTUAL 80%`, `FORECAST 100%` → `minipark4u@gmail.com`
- 실데이터가 10GB 무료 한도 미만이라 **현재 예상 과금은 0원**. 과금을 인위적으로 만들지 않는다.

### 3.4 Always Free (유료 계정 기준)
Standard 10GB + IA 10GB + Archive 10GB + API 50,000건/월.

---

## 4. 백업 파이프라인 (운영 중)

```
devforge-backup-safety.timer (매일 23:00 UTC = 08:00 KST)
  └─ devforge-backup.service
       └─ python3.11 scripts/osync_backup.py all
            ├─ DB   : pg_dump -Fc devforge_app → backups/database/devforge_YYYY-MM-DD.dump
            └─ APP  : tar(scripts+docs+systemd-user) → backups/application/devforge_app_YYYY-MM-DD.tgz

devforge-restore-test.timer (매월 1일 20:30 UTC)
  └─ devforge-restore-test.service
       └─ python3.11 scripts/osync_restore_test.py  (scratch DB 복원 후 테이블 수 검증 → drop)
```

- **로컬 스테이징**: `/opt/ai_data/backups/` (`db/`, `app/`, `osync.log`)
- **보존**: 원격 DB 30일 / app 56일, 로컬 DB 7일 / app 4일
- **sentinel**: DB 하루 1회, app ISO 주 1회 중복 방지 (`.db_done_*`, `.app_done_*`)
- **secrets 제외**: 앱 tar에서 `.env`/`.pem`/`secret`/`credential` 미포함
- 수동 실행: `python3.11 /opt/projects/server/scripts/osync_backup.py all [--force]`
- 감시: watchdog이 `devforge-backup-safety.timer`(stale→kick)와 `devforge-backup.service` 실행결과(`ActiveState/Result`)를 감시하고, 실패 시 **자동 재실행**(self-heal, backoff/circuit)합니다.
- 검증: 복원 테스트 결과 `48 tables OK` (2026-09-11)
- 2026-09-19: OCI 키 지문 불일치/미설치로 업로드 실패 → 등록 키(`e7:58…`) 복원·설치로 정상화.
  sentinel 생성으로 watchdog 재실행 루프 중단, 원격 `backups/database/devforge_2026-09-19.dump`(105.9MB) 업로드 확인.

> 레거시: `/usr/local/bin/dump_postgres.sh` → `/mnt/secure_meta/snapshots`(현재 빈 디렉터리)는
> 폐기됨. 위 osync 파이프라인이 대체.

---

## 5. 파일 교환 (Exchange) — OCI 구현 완료

### 5.1 (구) Azure 구조 — 참고용 (2026-09-11 이전)
```
브라우저 ── /send, /receive ──▶ Caddy ──▶ 127.0.0.1:8085 (Blob Explorer)
                                              └─ Azure Blob stshareddevforgeprodkrc/devforge
```
- `scripts/blob_explorer/` — 업로드/다운로드 웹 UI (FastAPI lifespan 백그라운드 스레드)
- `scripts/lib/blob_uploader.py` — 파이프라인 산출물 업로드 + **7일 SAS** 링크
- 다운로드는 Azure SAS(1~168시간), 최종 단축은 Droplr

### 5.2 구현 구조 (OCI)
```
브라우저 ── /send, /receive ──▶ Caddy ──▶ FastAPI(:8002) / Exchange
                                              ├─ 목록/업로드: OCI SDK (서버측)
                                              └─ 공유 링크 : OCI PAR → Droplr 단축
```
- **PAR (Pre-Authenticated Request)**: Azure SAS의 OCI 대응. TTL·범위(객체/prefix)·읽기/쓰기를 세밀 제어.
- 공개 공유는 public 버킷이 아니라 **PAR**가 표준. (만료까지 공개 URL이므로 짧은 TTL + prefix 한정)
- **write-PAR**로 브라우저 → OCI 직접 PUT → 서버 프록시/메모리 적재 제거
  (현재 `handler.py`는 `item.file.read()`로 파일 전체를 메모리에 적재)
- prefix 매핑: 문서→`uploads/documents`, 이미지→`uploads/images`, 임시→`tmp/`(7일 자동 삭제)

### 5.3 최종 주소 = Droplr
- 긴 OCI PAR URL을 **Droplr로 단축**해 단일 주소 체계로 제공.
- 기존 자산 재사용: `scripts/lib/blob_uploader._shorten_with_droplr()`, `scripts/droplr_upload.py`
  (`drplr link --porcelain`, 자격증명은 `~/.config/devforge/secrets.env`의 `DROPLR_*`)
- 파이프라인 산출물(review bundle)도 `releases/` 업로드 후 Droplr 단축 → Notion 메모로 공유.

---

### 5.4 구현 내역 (2026-09-11)

| 항목 | 변경 |
|---|---|
| 신규 | `scripts/lib/oci_storage.py` — list / put / delete / PAR 생성 |
| 신규 | `scripts/lib/droplr.py` — Droplr HTTP API(Basic) 단축, Node CLI 불필요 |
| 교체 | `scripts/blob_explorer/blob.py` — Azure → OCI (`_list_blobs`/`_upload_blob`/`_generate_sas`/`_share_url`) |
| 수정 | `scripts/blob_explorer/handler.py` — 업로드 완료 시 `_share_url`(PAR→Droplr) 표시 |
| 이미지 | `containers/fastapi/Containerfile` — `jinja2`, `oci` 추가 (재빌드) |
| quadlet | `container-devforge-fastapi.container` — `BLOB_EXPLORER_LISTEN=0.0.0.0:8085`, `~/.oci:/root/.oci:ro` 마운트 |
| pod | `svc.pod` — `PublishPort=127.0.0.1:8085:8085` (Caddy `/send`,`/receive` 도달) |
| 동작 | `POST /send` → `uploads/{images|documents}/<ts>_<file>` 업로드 후 **`https://d.pr/...`** 반환 |
| write-PAR | `GET /presign?name=FILE` → `{object_name, upload_url, download_url}`. **브라우저 직접 업로드 구현**(`/send` JS → PUT, 실패 시 서버 경유 자동 fallback) |
| CORS | OCI는 버킷 CORS 설정 기능이 없지만 **PAR 응답에 `Access-Control-Allow-Origin: *` + PUT 허용을 자동 반환** → 별도 설정 불필요(검증됨) |

> 파이프라인 산출물(`lib/blob_uploader.py`)도 OCI `releases/` + PAR + Droplr로 이관 완료(2026-09-11).
> 이미지에서 `azure-storage-blob` 제거 → Azure 스토리지 의존 0.

---

## 6. 전체 구조 관점 — 적용하면 좋은 점

| # | 통합 포인트 | 이점 |
|---|---|---|
| 1 | **Blob Explorer 백엔드 OCI 교체** | Caddy 라우트(`/send`,`/receive`)·UI 유지, Azure 비용/의존 제거, 스토리지 단일화 |
| 2 | **최종 링크 Droplr 통일** | `d.pr/...` 단일 주소 체계, 기존 `_shorten_with_droplr` 재사용 |
| 3 | **prefix 역할 분리 + lifecycle** | `tmp/`(7일)·`logs/`(30일) 자동 정리 → 임시 공유 파일 위생 확보 |
| 4 | **파이프라인 산출물 통일** | `blob_uploader.upload_review_bundle/upload_raw` → `releases/` + PAR + Droplr |
| 5 | **백업/복원** | DB·앱 원격 보관 + 월간 복원 검증(이미 운영) → 서버 장애 시 복구 경로 |
| 6 | **watchdog/알림 연계** | 백업 실패/복원 실패를 `scripts/lib/notify.py`로 Slack/Telegram 알림 (현재 로그만) |
| 7 | **cli.py status 노출** | OCI 사용량·최근 백업 성공 여부를 라이브 상태에 추가 |
| 8 | **PAR 보안 강화** | Azure SAS 7일 고정 → OCI PAR 짧은 TTL·범위 제한, write-PAR 직접 업로드 |
| 9 | **컨테이너 경량화** | FastAPI 이미지에서 `azure-storage-blob` 의존 제거 가능 |
| 10 | **비용 거버넌스** | 예산 알림(USD1) + Always Free 한도 내 운영, Cost Analysis 추적 |

---

## 7. 운영 명령

```bash
# 버킷/객체 확인
export PATH="$HOME/.local/bin:$PATH"; export SUPPRESS_LABEL_WARNING=True
oci os bucket list --compartment-id "$(grep '^tenancy=' ~/.oci/config | cut -d= -f2)"
oci os object list --bucket-name devforge-standard --prefix backups/ --fields name,size,timeCreated

# 인증/네임스페이스·lifecycle 확인 (지문 e7:58…)
oci os ns get
oci os object-lifecycle-policy get --bucket-name devforge-standard

# 백업 / 복원 테스트 수동 실행
python3.11 /opt/projects/server/scripts/osync_backup.py all --force
python3.11 /opt/projects/server/scripts/osync_restore_test.py

# 타이머 확인
systemctl --user list-timers | grep -E "backup|restore"
```

---

## 8. 관련 파일

| 구분 | 파일 |
|---|---|
| 백업 | `scripts/osync_backup.py`, `scripts/osync_restore_test.py` |
| 타이머/서비스 | `~/.config/systemd/user/devforge-backup.service`, `devforge-backup-safety.timer`, `devforge-restore-test.service`, `devforge-restore-test.timer` |
| 파일 교환(OCI) | `scripts/blob_explorer/` (blob.py, handler.py), `scripts/lib/oci_storage.py` |
| 단축(Droplr) | `scripts/lib/droplr.py` (HTTP API), `scripts/droplr_upload.py`, `scripts/lib/blob_uploader._shorten_with_droplr` |
| 파이프라인 산출물 | `scripts/lib/blob_uploader.py` (OCI `releases/` + PAR + Droplr) |
| 로컬 스테이징 | `/opt/ai_data/backups/` |

### 출처 (Oracle 공식)
- [Storage Tiers](https://docs.oracle.com/en-us/iaas/Content/Object/Concepts/understandingstoragetiers.htm)
- [Lifecycle Management](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/usinglifecyclepolicies.htm)
- [Managing Buckets (public/PAR)](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/managingbuckets.htm)
- [Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
