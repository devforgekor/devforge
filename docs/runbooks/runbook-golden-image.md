# Golden Image — Qwen3-30B-A3B (MoE) + Spot VM Runbook

**리소스 그룹**: `rg-devforge-prod-cin` (Central India)  
**목적**: DevForge 요청 시 Spot VM을 즉시 생성하여 LLM 추론 (코딩)  
**갱신 주기**: 매년 **2월 15일** 고정 (자동 알림 → 수동 실행)
> **설계 결정(2026-09-03)**: 안정성 우선 — 월 1회 재빌드 없음. 보안 패치는 이미지 재빌드 없이 `unattended-upgrades`로 보완. Spot 실패 시 폴백 체인 없이 다음 성공 시 재시도로 처리(best-effort).

> **⚠️ 필수 주석 — FX2ms_v2 부팅 호환 (2026-09-11 확정, 과거 세션 근거)**
> - **이미지 정의에 `DiskControllerTypes=SCSI,NVMe` 필수.** `az sig image-definition create`에 이 feature가 없거나 **NVMe만**이면 **FX2ms_v2 배포 시 부팅 실패**:
>   `InvalidParameter: cannot boot with OS image or disk. DiskControllerTypes supported: NVMe`
>   (직접 마켓플레이스 `Ubuntu2204` 이미지는 FX에서 부팅되나, **갤러리 캡처 이미지는 부팅 불가** — 2026-09-04 세션에서 확인)
> - **해결**: 이미지 정의 features에 **`SCSI, NVMe` 둘 다** 지정(현 `llm-qwen-27b` 정의에 반영됨). 확인: `az sig image-definition show -g rg-devforge-prod-cin --gallery-name gallery_devforge_prod_cin --gallery-image-definition llm-qwen-27b --query features`.
> - **과거 `Standard_E4s_v3` 기억**: 현재 이 구독에서 **`NotAvailableForSubscription`** → 사용 불가. 빌더·배포는 **`Standard_FX2ms_v2` 단일 SKU** 유지(§2 정책과 동일).
> - **현 배포 이미지 실제값**(게시 2026-09-04 07:09 UTC, `llm-qwen-27b:2026.09.2`): 서비스명 **`llm.service`**, 바이너리 **`/usr/local/bin/llama-server`**, 모델 **`/opt/models/qwen3.6-27b-q8_0.gguf`**, 포트 `8080`, `--n-gpu-layers 0`. → **2026-09-11 §1.2 recipe를 실제값으로 정합 완료**.
> - **`--jinja`**: 툴콜(function calling)에 필요(골든 이미지 기본 ExecStart엔 없음, 모듈이 런타임 자동 적용). 재빌드 시 baked-in 권장.
> - **차기 골든 이미지 모델 (2026-09-11 확정)**: **`Qwen3-30B-A3B-Q4_K_M`** (18.56GB, MoE·3B active) + `--jinja` + `--chat-template-kwargs '{"enable_thinking":false}'`. FX2ms_v2에서 **툴콜 정상**, 생성 **~6 tok/s**(Q6_K는 25GB·~3.3 tok/s, Q8_0은 ~2.2 tok/s로 비권장).

---

## 전체 아키텍처

```
DevForge (on-prem)
    │ HTTP req (model + prompt)
    ▼
┌──────────────────────────────────────┐
│  Spot VM — Central India             │
│  Standard_FX2ms_v2 (2코어, 42GB RAM) │
│                                      │
│  ┌────────────────────────────────┐  │
│  │ llama-server :8080            │  │
│  │   ↕ systemd auto-start        │  │
│  ├────────────────────────────────┤  │
│  │ /opt/models/qwen3-30b-a3b-q4_k_m.gguf│ ← 이미지 baked-in
│  │ /usr/local/bin/llama-server      │ ← 이미지 baked-in
│  ├────────────────────────────────┤  │
│  │ Standard SSD 64GB             │  │
│  │ 42GB RAM (모델 ~18.6GB)       │  │
│  └────────────────────────────────┘  │
│                                      │
│  Spot Eviction → 자동 Delete         │
└──────────────────────────────────────┘
    │ HTTP response
    ▼
DevForge ← 결과 수신
```

---

## 리소스 요약

| 리소스 | 이름 | 위치 | 비고 |
|--------|------|------|------|
| 리소스 그룹 | `rg-devforge-prod-cin` | Central India | 모든 리소스 통일 |
| Compute Gallery | `gallery_devforge_prod_cin` | Central India | 최초 1회 생성 |
| 이미지 정의 | `llm-qwen-27b` | Gallery 내 | 최초 1회 생성 |
| 이미지 버전 | `YYYY.MM.0` | Central India | 연 1회 증가 (예: 2026.02.0) |
| 빌더 VM (임시) | `temp-golden-builder` | Central India | 빌드 후 삭제 |
| Managed Image (임시) | `axis-golden-image` | Central India | Gallery 등록 후 삭제 |
| 배포 VM | `llm-qwen-27b-*` | Central India | Spot, Delete 정책 |

---

## 1. 이미지 빌드

### 1.1 임시 VM 생성

```bash
az vm create \
  --resource-group rg-devforge-prod-cin \
  --name temp-golden-builder \
  --location centralindia \
  --image Ubuntu2204 \
  --size Standard_FX2ms_v2 \
  --admin-username azureuser \
  --ssh-key-values ~/.ssh/id_rsa.pub \
  --os-disk-size-gb 64 \
  --storage-sku StandardSSD_LRS \
  --os-disk-delete-option Delete

# 공용 IP 확인
az vm show -d -g rg-devforge-prod-cin -n temp-golden-builder --query publicIps -o tsv
```

### 1.2 VM 내부 설정 (SSH 접속)

```bash
ssh azureuser@<VM_IP>

# --- 시스템 패키지 ---
sudo apt update && sudo apt upgrade -y
sudo apt install -y wget curl git python3-pip unattended-upgrades

# --- 보안 패치 자동 적용 (이미지 재빌드 없이 CVE 창 축소) ---
# 연 1회 full rebuild를 유지하되, 그 사이 보안 업데이트는 자동 적용
sudo dpkg-reconfigure -f noninteractive unattended-upgrades
# 보안 repo만 자동, 자동 재부팅 비활성(수동 재부팅, ephemeral VM 특성상 재빌드 시 반영)
sudo tee /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
sudo systemctl enable --now unattended-upgrades

# --- llama.cpp prebuilt binary (버전 고정 권장) ---
# 최신 태그 확인: https://github.com/ggml-org/llama.cpp/releases
LLAMA_VER="b4432"  # 갱신 시 최신 stable로 변경
sudo mkdir -p /opt/llama
wget -O /tmp/llama.tar.gz \
  "https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_VER}/llama-server-linux-x64.tar.gz"
sudo tar -xzf /tmp/llama.tar.gz -C /opt/llama/
sudo chmod +x /opt/llama/llama-server /opt/llama/llama-cli
# 실제 이미지와 정합: 바이너리를 /usr/local/bin 에 배치 (모듈 ensure_tool_calling이 /usr/local/bin/llama-server 탐색)
sudo install -m 0755 /opt/llama/llama-server /usr/local/bin/llama-server
sudo install -m 0755 /opt/llama/llama-cli /usr/local/bin/llama-cli
rm /tmp/llama.tar.gz

# --- Qwen3-30B-A3B Q4_K_M GGUF (MoE, 3B active — 2 vCPU에서 툴콜 검증됨) ---
sudo mkdir -p /opt/models
pip3 install huggingface-hub -q
# 비공개 모델일 경우 HUGGINGFACE_HUB_TOKEN 환경변수 필요
huggingface-cli download Qwen/Qwen3-30B-A3B-GGUF \
  --include "Qwen3-30B-A3B-Q4_K_M.gguf" \
  --local-dir /opt/models

# --- 실제 이미지와 정합: 파일명을 소문자 경로로 정규화 ---
mv /opt/models/Qwen3-30B-A3B-Q4_K_M.gguf /opt/models/qwen3-30b-a3b-q4_k_m.gguf

# --- llama-server systemd service (hardened, context7 verified) ---
# systemd: PrivateTmp/ProtectSystem은 2차 방어선으로 유효(systemd.io/TEMPORARY_DIRECTORIES)
# llama.cpp: --api-key는 LLAMA_API_KEY env로 주입, X-Api-Key/Bearer 둘 다 검증(server/README.md)
sudo tee /etc/systemd/system/llm.service << 'EOF'
[Unit]
Description=llama.cpp LLM Server
After=network.target
Wants=network-online.target

[Service]
Type=simple
# --host 127.0.0.1 로 바인딩 후 Caddy가 443에서 TLS 종단 (평문 0.0.0.0 노출 제거)
ExecStart=/usr/local/bin/llama-server \
  -m /opt/models/qwen3-30b-a3b-q4_k_m.gguf \
  -c 8192 \
  --port 8080 \
  --host 127.0.0.1 \
  --n-gpu-layers 0 \
  --jinja \
  --chat-template-kwargs '{"enable_thinking":false}' \
  --api-key ${LLAMA_API_KEY}
Restart=always
RestartSec=5
StartLimitBurst=3
StartLimitIntervalSec=60
DynamicUser=yes
StateDirectory=llm
# --- systemd sandbox (최소 하드닝, CIS L1 대신) ---
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
DevicePolicy=closed
ProtectSystem=strict
ProtectHome=read-only
ProtectControlGroups=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
LockPersonality=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
RestrictRealtime=yes
SystemCallArchitectures=native
ReadWritePaths=/opt/models
MemoryMax=38G
CPUQuota=180%

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable llm

# --- Caddy reverse proxy (127.0.0.1:8080 → :443, TLS는 DevForge Caddy가 종단) ---
# ephemeral VM 특성상 인증서 자동 발급 불필요 — DevForge 측 Caddy(host network, auto-HTTPS)가
# 이미 외부 TLS를 종단하므로 VM 내부 Caddy는 127.0.0.1 프록시만 수행. 또는 NSG에서 8080을
# DevForge IP/32 로만 허용하고 Caddy 없이 127.0.0.1+API key 조합만으로도 P0 해소 가능.
sudo tee /etc/caddy/Caddyfile << 'EOF'
:443 {
    reverse_proxy 127.0.0.1:8080
    # header_up Authorization {http.request.header.Authorization}
}
EOF
# API key는 Key Vault(Managed Identity) 또는 secrets.env에서 주입 — 평문 커밋 금지
# 예: export LLAMA_API_KEY=$(az keyvault secret show --vault-name kv-devforge --name llama-api-key --query value -o tsv)
# 검증: systemd-analyze security llm.service (score >= 70 목표)

# --- 캐시 정리 (이미지 크기 최적화) ---
sudo apt remove -y python3-pip
sudo apt autoremove --purge -y && sudo apt autoclean && sudo apt clean
sudo journalctl --vacuum-size=10M
sudo rm -rf /var/log/*.log /var/log/apt/*.log
sudo rm -rf /tmp/* /var/tmp/*
sudo rm -rf /home/azureuser/.cache/* /root/.cache/*
rm -rf ~/.cache/huggingface/*
```

### 1.3 VM 일반화 (Deprovision)

```bash
# SSH 세션 내부에서 실행
sudo waagent -deprovision+user
# → 접속 자동 종료 (정상)
```

### 1.4 VM 할당 취소 및 일반화 (로컬 터미널)

```bash
az vm deallocate --resource-group rg-devforge-prod-cin --name temp-golden-builder
az vm generalize  --resource-group rg-devforge-prod-cin --name temp-golden-builder
```

### 1.5 Managed Image 캡처

```bash
az image create \
  --resource-group rg-devforge-prod-cin \
  --name axis-golden-image \
  --source temp-golden-builder \
  --os-type Linux --hyper-v-generation V2
```

### 1.6 Compute Gallery 등록

```bash
# Gallery 생성 (최초 1회)
az sig create \
  --resource-group rg-devforge-prod-cin \
  --gallery-name gallery_devforge_prod_cin \
  --location centralindia

# 이미지 정의 생성 (최초 1회)
az sig image-definition create \
  --resource-group rg-devforge-prod-cin \
  --gallery-name gallery_devforge_prod_cin \
  --gallery-image-definition llm-qwen-27b \
  --publisher AxisPublisher \
  --offer AxisOffer \
  --sku AxisSku \
  --os-type Linux --hyper-v-generation V2 \
  --features "DiskControllerTypes=SCSI,NVMe"

# 이미지 버전 생성 (연 1회: YYYY.MM.0 형식)
VERSION=$(date +%Y.%m.0)  # 예: 2026.02.0
SUB=$(az account show --query id -o tsv)
az sig image-version create \
  --resource-group rg-devforge-prod-cin \
  --gallery-name gallery_devforge_prod_cin \
  --gallery-image-definition llm-qwen-27b \
  --gallery-image-version ${VERSION} \
  --managed-image "/subscriptions/${SUB}/resourceGroups/rg-devforge-prod-cin/providers/Microsoft.Compute/images/axis-golden-image" \
  --target-regions centralindia \
  --replica-count 1
```

### 1.7 임시 리소스 정리

```bash
az vm delete \
  --resource-group rg-devforge-prod-cin \
  --name temp-golden-builder \
  --yes --force-deletion

az image delete \
  --resource-group rg-devforge-prod-cin \
  --name axis-golden-image
```

---

## 2. 배포 (DevForge 요청 시)

```bash
# 버전은 최신 active 버전 조회 후 사용
VERSION=$(az sig image-version list \
  --resource-group rg-devforge-prod-cin \
  --gallery-name gallery_devforge_prod_cin \
  --gallery-image-definition llm-qwen-27b \
  --query "sort_by(@, &name)[-1].name" -o tsv)

az vm create \
  --resource-group rg-devforge-prod-cin \
  --name llm-qwen-27b-$(date +%s) \
  --location centralindia \
  --image "/subscriptions/$(az account show --query id -o tsv)/resourceGroups/rg-devforge-prod-cin/providers/Microsoft.Compute/galleries/gallery_devforge_prod_cin/images/llm-qwen-27b/versions/${VERSION}" \
  --size Standard_FX2ms_v2 \
  --admin-username azureuser \
  --ssh-key-values ~/.ssh/id_rsa.pub \
  --priority Spot \
  --eviction-policy Delete \
  --os-disk-delete-option Delete \
  --public-ip-sku Standard \
  --public-ip-dns-name llm-qwen-$(date +%s)
```

**예상 소요 시간**: VM 생성 2~3분 + 모델 mmap 로드 ~1분 = **총 ~3~4분**

> **Spot 실패 정책(best-effort)**: 단일 SKU(`Standard_FX2ms_v2`)/단일 리전(`centralindia`) 유지. 폴백 체인(다중 SKU/리전, Spot→Regular) 없음. 배포 실패 시 큐에 적재 후 다음 15분 주기(`golden-image-deploy-check.timer`)에 재시도. 급하지 않은 워크로드는 다음 성공 시점까지 대기.

---

## 3. 갱신 (매년 2월 15일 고정 — 안정성 우선, 월간 재빌드 없음)

### 3.1 자동 알림 (시스템드 타이머)

- **타이머**: `golden-image-yearly-check.timer` — 매년 2월 15일 03:00 KST 실행
- **동작**: 업스트림 변경 사항(Qwen/llama.cpp/Ubuntu 보안) 감지 → 변경 시 **이메일 발송**
- **시크릿 위치**: `~/.config/devforge/secrets.env` (`SMTP_*`, `ALERT_EMAIL_TO`)

```ini
# ~/.config/systemd/user/golden-image-yearly-check.timer
[Unit]
Description=Annual golden image refresh check (Feb 15 03:00 KST)

[Timer]
OnCalendar=*-02-14 18:00:00
Persistent=true
AccuracySec=1h

[Install]
WantedBy=timers.target
```
> **시간대**: 서버는 `GMT(UTC)` 고정. 문서상 `KST 03:00`은 `UTC 18:00(전일)`로 변환하여 `OnCalendar=*-02-14 18:00:00` 으로 구현. `Timezone=` 키는 user timer에서 미지원.

### 3.2 갱신 실행 절차 (수동, 알림 수신 후)

```bash
# 1. 스크립트 실행 (대화형 확인 포함)
/opt/projects/server/scripts/golden_image/yearly_refresh.sh

# 2. 스크립트 내부 동작:
#    - 빌더 VM 생성 → 설정 → 일반화 → 캡처 → 갤러리 등록 (섹션 1.1~1.6)
#    - 버전: YYYY.MM.0 (예: 2026.02.0)
#    - 테스트 배포 → 스모크 테스트(추론 1회) 통과 시에만 active 전환
#    - 이전 버전 deprecated 표시
#    - 임시 리소스 정리 (섹션 1.7)
```

### 3.3 갱신 판단 기준 (자동 체크 스크립트가 평가)

| 변경 유형 | 갱신 필요 여부 |
|-----------|----------------|
| Qwen 메이저/마이너 버전 업 (3.7, 3.8 등) | **필수** |
| llama.cpp 릴리스 태그 변경 (성능/버그픽스 포함) | **권장** |
| Ubuntu 26.04 포인트 릴리스 (.1, .2 등 보안 패치) | **권장** |
| 그 외 (패치 버전만, 문서 업데이트 등) | 불필요 |

> **기본 정책**: 변경 사항 없으면 갱신 생략. 알림 메일에 "변경 사항 없음" 명시.
> **보안 보완**: 이미지 재빌드 주기는 연 1회이나, 그 사이 CVE는 `unattended-upgrades`(보안 repo 자동 적용)로 완화. Gallery 이미지 `endOfLifeDate` 미사용 — ephemeral 특성상 재시작 시 최신 패치가 반영되므로 별도 EOL 차단 불필요. 감사 시 `risk accepted with compensating control(unattended-upgrades)`로 문서화.

---

## 4. 운영 자동화 (최소 구성)

### 4.1 시스템드 타이머 (2개)

| 타이머 | 주기 | 역할 |
|--------|------|------|
| `golden-image-deploy-check.timer` | 15분 (`OnCalendar=*:0/15`) | **배포 중인 VM만** 헬스체크 + 타임아웃(10분) 감시 |
| `golden-image-yearly-check.timer` | 연 1회 (2/15) | 업스트림 변경 감지 → 이메일 알림 (월간 재빌드 없음) |

### 4.2 필수 알림만 (이메일 + Slack `#devforge-alerts`)

| 이벤트 | 조건 | 채널 |
|--------|------|------|
| **배포 실패** | VM 생성 10분 초과 또는 헬스체크 3회 연속 실패 → 큐 적재 후 다음 주기 재시도 | 이메일 + Slack |
| **Spot Eviction** | Event Grid 수신 시 | 이메일 + Slack |
| **연 1회 갱신 필요** | 2/15 체크 시 업스트림 변경 감지 | 이메일 + Slack |

> **알림 안 함**: 배포 성공, 헬스체크 정상, 롤백, 비용 리포트 등

### 4.3 DB 기록 (PostgreSQL `devforge_app`)

```sql
-- 3개 테이블만
golden_image_versions  -- 버전, 이미지ID, 상태(active/deprecated), 메모
deployment_logs        -- 배포 이력, VM명, 상태, 공인IP, 에러
health_checks          -- 헬스체크 결과 (성공/실패, 레이턴시)
```

### 4.4 자동화 스크립트 위치

```
/opt/projects/server/scripts/golden_image/  # 예정 경로 — 현재 미생성(Code is SSOT 위반 해소 필요)
├── refresh_cycle.py      # 15분 주기: 배포 동기화 + 헬스체크 + 타임아웃 + eviction 처리
├── yearly_check.py       # 연 1회: 업스트림 변경 감지 → 이메일 발송
├── yearly_refresh.sh     # 갱신 실행 스크립트 (수동 호출)
├── azure_client.py       # Azure CLI 래퍼 (재시도, 인증)
└── models.py             # SQLAlchemy 모델
# TODO: 위 경로는 문서상 예정이며 실제 디렉터리는 없음. 구현 시 생성하거나
# 문서 경로를 scripts/pipelines/ 로 정정 필요. (Deep Dive Step 5)
```

---

## 5. 비용

### 월 저장 비용 (Gallery)

| 항목 | 산식 | 금액 |
|------|------|------|
| 모델 + OS (~24 GB) | ~24 GB × $0.05/GB | **~$1.2/월** |

### 실행 비용

| 항목 | 금액 |
|------|------|
| Spot VM (Standard_FX2ms_v2) | 약 $0.004~0.012/시 (60~90% 할인) |
| Standard SSD 64GB | 약 $0.32/월 (할당 시) |

---

## 6. 네트워크 보안

| 방향 | 규칙 |
|------|------|
| DevForge → VM :443 (Caddy) | NSG 허용 (DevForge 공인 IP/32) + `Authorization: Bearer ${LLAMA_API_KEY}` 필수 |
| SSH :22 | 키 전용, DevForge IP 대역 |
| Azure Event Grid 웹훅 | Caddy 경유 `POST /webhook/azure/eventgrid/*` → `localhost:8001` (devforge-mcp) |
| 그 외 | 전부 차단 |

---

## 7. 시크릿 관리 (`~/.config/devforge/secrets.env`)

```bash
# Azure 인증 (Managed Identity 권장, SPN 대체용)
AZURE_CLIENT_ID=
AZURE_TENANT_ID=
AZURE_CLIENT_SECRET=

# 이메일 알림 (연 1회 갱신 알림 + 장애 알림)
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
ALERT_EMAIL_TO=admin@example.com
ALERT_EMAIL_FROM=devforge@example.com

# Slack 웹훅 (선택)
SLACK_WEBHOOK_URL=
```

> **주의**: 이 파일은 `chmod 600` 권한 유지. Git 커밋 금지.

---

## 8. 롤백 절차 (장애 시 수동)

```bash
# 1. 이전 active 버전 확인
az sig image-version list \
  --resource-group rg-devforge-prod-cin \
  --gallery-name gallery_devforge_prod_cin \
  --gallery-image-definition llm-qwen-27b \
  --query "[?provisioningState=='Succeeded'].{Version:name}" -o table

# 2. 이전 버전으로 새 VM 배포 (섹션 2 명령어에서 VERSION만 변경)
VERSION=2025.02.0  # 이전 버전 지정

# 3. 스모크 테스트 수동 확인 (API key 필수 — §1.2 LLAMA_API_KEY)
curl -X POST https://<new_ip>/completion \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${LLAMA_API_KEY}" \
  -d '{"prompt": "test", "n_predict": 16}'

# 4. 정상 시 DB에서 이전 버전 active, 실패 버전 deprecated 업데이트

---

## 9. 개정 이력

| 일자 | 변경 | 사유 |
|------|------|------|
| 2026-09-03 | 갱신 주기: 연 1회 유지 명시, `unattended-upgrades` 보완 추가 (§1.2, §3) | 안정성 우선 — 월간 재빌드 불필요, 보안은 자동 패치로 완화 (갭 분석 보고서 `golden-image-gap-report-2026-09-03.md` §2.2 반영) |
| 2026-09-03 | Spot 폴백 체인 없음 명시, 실패 시 다음 주기 재시도 정책 추가 (§2) | best-effort 워크로드 — 다중 SKU/리전 과설계 방지 (갭 보고서 §2.6, §2.12 반영) |
| 2026-09-03 | Deep Dive(context7): systemd 샌드박스 15종 + llama-server 127.0.0.1/API key + Caddy, 네트워크 443/API key, 롤백 curl TLS/API key 보정 (§1.2, §6, §8) | context7 검증 — systemd.io(PrivateTmp/ProtectSystem), ggml-org/llama.cpp(--api-key/LLAMA_API_KEY), Azure(갤러리/Spot Scheduled Events) (dp-20260903-golden-image-deep-dive) |
| 2026-09-04 | 15분 타이머 복구 + orphan VM 강제 종료 안전장치 추가: `_check_orphan_vms()` + `azure_client.list_vms_by_prefix()` + symlink 복구 + `claude-mode` 기본값 `deepseek` | `golden-image-deploy-check.service` 경로 불일치로 실행 안 됨. symlink 생성, VM 잔존 시 강제 삭제 로직 추가, 기본 모드 `deepseek`로 변경 |
| 2026-09-09 | `azure_client.list_vms_by_prefix()`에 `--show-details` 추가 (`publicIps`/`powerState` 필드 보정) | orphan 감지 쿼리가 `--show-details` 없이 조회해 실제 VM 존재 시 IP/상태가 누락됨. `claude-mode`(`.bashrc.d/claude-mode:28`)와 패리티 유지 — 강제 종료는 정상이나 로그 정확도 개선 |
| 2026-09-11 | **FX2ms_v2 부팅 호환 주석 추가 + 이미지 정의에 `--features "DiskControllerTypes=SCSI,NVMe"` 추가 + ExecStart `--jinja` + 이미지 정의명·§1.2 recipe 실제값 정합** | 과거 세션(2026-09-04) "FX 호환성 불일치": 갤러리 캡처 이미지(NVMe)가 FX2ms_v2에서 `cannot boot ... DiskControllerTypes supported: NVMe`로 부팅 실패 → `SCSI, NVMe` 병기로 해결(현 이미지 반영). `E4s_v3`는 현재 `NotAvailableForSubscription`. `--jinja`=툴콜 필수. §1.2를 실제 이미지와 정합(서비스 `llm.service`, 바이너리 `/usr/local/bin/llama-server`, 모델 `/opt/models/qwen3.6-27b-q8_0.gguf`) |
| 2026-09-11 | 모델 확정: `Qwen3.6-27B-Q8_0` → **`Qwen3-30B-A3B-Q4_K_M`(MoE)** + `--chat-template-kwargs '{"enable_thinking":false}'` (§1.2·ExecStart·아키텍처) | 2 vCPU spot에서 27B dense는 툴콜 타임아웃. **MoE(3B active)는 툴콜 정상·~6 tok/s**로 검증(Q4 18.56GB > Q6 25GB·3.3tok/s > Q8 비권장). 차기 이미지 재빌드에 반영 |
```
