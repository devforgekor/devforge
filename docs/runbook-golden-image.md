# Golden Image — Qwen 3.6 27B + Spot VM Runbook

**리소스 그룹**: `rg-devforge-prod-cin` (Central India)  
**목적**: DevForge 요청 시 Spot VM을 즉시 생성하여 LLM 추론 (코딩)  
**갱신 주기**: 매년 **2월 15일** 고정 (자동 알림 → 수동 실행)

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
│  │ /opt/models/qwen-27b-q8_0.gguf│  │  ← 이미지 baked-in
│  │ /opt/llama/llama-server       │  │  ← 이미지 baked-in
│  ├────────────────────────────────┤  │
│  │ Standard SSD 64GB             │  │
│  │ 42GB RAM (모델 ~28.6GB)       │  │
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
| Compute Gallery | `NeuronGallery` | Central India | 최초 1회 생성 |
| 이미지 정의 | `llm-qwen-27b-golden` | Gallery 내 | 최초 1회 생성 |
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
  --image UbuntuMinimal2604 \
  --size Standard_FX2ms_v2 \
  --admin-username azureuser \
  --ssh-key-values ~/.ssh/id_rsa.pub \
  --os-disk-size-gb 64 \
  --os-disk-type StandardSSD_LRS \
  --os-disk-delete-option Delete

# 공용 IP 확인
az vm show -d -g rg-devforge-prod-cin -n temp-golden-builder --query publicIps -o tsv
```

### 1.2 VM 내부 설정 (SSH 접속)

```bash
ssh azureuser@<VM_IP>

# --- 시스템 패키지 ---
sudo apt update && sudo apt upgrade -y
sudo apt install -y wget curl git python3-pip

# --- llama.cpp prebuilt binary (버전 고정 권장) ---
# 최신 태그 확인: https://github.com/ggml-org/llama.cpp/releases
LLAMA_VER="b4432"  # 갱신 시 최신 stable로 변경
sudo mkdir -p /opt/llama
wget -O /tmp/llama.tar.gz \
  "https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_VER}/llama-server-linux-x64.tar.gz"
sudo tar -xzf /tmp/llama.tar.gz -C /opt/llama/
sudo chmod +x /opt/llama/llama-server /opt/llama/llama-cli
rm /tmp/llama.tar.gz

# --- Qwen 3.6 27B Q8_0 GGUF ---
sudo mkdir -p /opt/models
pip3 install huggingface-hub -q
# 비공개 모델일 경우 HUGGINGFACE_HUB_TOKEN 환경변수 필요
huggingface-cli download ggml-org/Qwen3.6-27B-GGUF \
  --include "Qwen3.6-27B-Q8_0.gguf" \
  --local-dir /opt/models

# --- 모델 심볼릭 링크 (갱신 시 경로 변경 불필요) ---
ln -sf /opt/models/Qwen3.6-27B-Q8_0.gguf /opt/models/model.gguf

# --- llama-server systemd service (User 템플릿화) ---
sudo tee /etc/systemd/system/llama-server.service << 'EOF'
[Unit]
Description=llama.cpp LLM Server
After=network.target

[Service]
Type=simple
ExecStart=/opt/llama/llama-server \
  -m /opt/models/model.gguf \
  -c 8192 \
  --port 8080 \
  --host 0.0.0.0 \
  --n-gpu-layers 0
Restart=on-failure
DynamicUser=yes
StateDirectory=llama-server

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable llama-server

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
  --os-type Linux
```

### 1.6 Compute Gallery 등록

```bash
# Gallery 생성 (최초 1회)
az sig create \
  --resource-group rg-devforge-prod-cin \
  --gallery-name NeuronGallery \
  --location centralindia

# 이미지 정의 생성 (최초 1회)
az sig image-definition create \
  --resource-group rg-devforge-prod-cin \
  --gallery-name NeuronGallery \
  --gallery-image-definition llm-qwen-27b-golden \
  --publisher AxisPublisher \
  --offer AxisOffer \
  --sku AxisSku \
  --os-type Linux

# 이미지 버전 생성 (연 1회: YYYY.MM.0 형식)
VERSION=$(date +%Y.%m.0)  # 예: 2026.02.0
SUB=$(az account show --query id -o tsv)
az sig image-version create \
  --resource-group rg-devforge-prod-cin \
  --gallery-name NeuronGallery \
  --gallery-image-definition llm-qwen-27b-golden \
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
  --gallery-name NeuronGallery \
  --gallery-image-definition llm-qwen-27b-golden \
  --query "sort_by(@, &name)[-1].name" -o tsv)

az vm create \
  --resource-group rg-devforge-prod-cin \
  --name llm-qwen-27b-$(date +%s) \
  --location centralindia \
  --image "/subscriptions/$(az account show --query id -o tsv)/resourceGroups/rg-devforge-prod-cin/providers/Microsoft.Compute/galleries/NeuronGallery/images/llm-qwen-27b-golden/versions/${VERSION}" \
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

---

## 3. 갱신 (매년 2월 15일 고정)

### 3.1 자동 알림 (시스템드 타이머)

- **타이머**: `golden-image-yearly-check.timer` — 매년 2월 15일 03:00 KST 실행
- **동작**: 업스트림 변경 사항(Qwen/llama.cpp/Ubuntu 보안) 감지 → 변경 시 **이메일 발송**
- **시크릿 위치**: `~/.config/devforge/secrets.env` (`SMTP_*`, `ALERT_EMAIL_TO`)

```ini
# ~/.config/systemd/user/golden-image-yearly-check.timer
[Unit]
Description=Annual golden image refresh check (Feb 15)

[Timer]
OnCalendar=Feb 15 03:00
Persistent=true
Timezone=Asia/Seoul

[Install]
WantedBy=timers.target
```

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

---

## 4. 운영 자동화 (최소 구성)

### 4.1 시스템드 타이머 (2개)

| 타이머 | 주기 | 역할 |
|--------|------|------|
| `golden-image-deploy-check.timer` | 15분 | **배포 중인 VM만** 헬스체크 + 타임아웃(10분) 감시 |
| `golden-image-yearly-check.timer` | 연 1회 (2/15) | 업스트림 변경 감지 → 이메일 알림 |

### 4.2 필수 알림만 (이메일 + Slack `#devforge-alerts`)

| 이벤트 | 조건 | 채널 |
|--------|------|------|
| **배포 실패** | VM 생성 10분 초과 또는 헬스체크 3회 연속 실패 | 이메일 + Slack |
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
/opt/projects/server/scripts/golden_image/
├── refresh_cycle.py      # 15분 주기: 배포 동기화 + 헬스체크 + 타임아웃 + eviction 처리
├── yearly_check.py       # 연 1회: 업스트림 변경 감지 → 이메일 발송
├── yearly_refresh.sh     # 갱신 실행 스크립트 (수동 호출)
├── azure_client.py       # Azure CLI 래퍼 (재시도, 인증)
└── models.py             # SQLAlchemy 모델
```

---

## 5. 비용

### 월 저장 비용 (Gallery)

| 항목 | 산식 | 금액 |
|------|------|------|
| 모델 + OS (32.7 GB) | 32.7 GB × $0.05/GB | **~$1.63/월** |

### 실행 비용

| 항목 | 금액 |
|------|------|
| Spot VM (Standard_FX2ms_v2) | 약 $0.004~0.012/시 (60~90% 할인) |
| Standard SSD 64GB | 약 $0.32/월 (할당 시) |

---

## 6. 네트워크 보안

| 방향 | 규칙 |
|------|------|
| DevForge → VM :8080 | NSG 허용 (DevForge 공인 IP 대역) |
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
  --gallery-name NeuronGallery \
  --gallery-image-definition llm-qwen-27b-golden \
  --query "[?provisioningState=='Succeeded'].{Version:name}" -o table

# 2. 이전 버전으로 새 VM 배포 (섹션 2 명령어에서 VERSION만 변경)
VERSION=2025.02.0  # 이전 버전 지정

# 3. 스모크 테스트 수동 확인
curl -X POST http://<new_ip>:8080/completion \
  -H "Content-Type: application/json" \
  -d '{"prompt": "test", "n_predict": 16}'

# 4. 정상 시 DB에서 이전 버전 active, 실패 버전 deprecated 업데이트
```
