# Golden Image — Qwen 3.6 27B + Spot VM Runbook

**리소스 그룹**: `rg-devforge-prod-cin` (Central India)  
**목적**: DevForge 요청 시 Spot VM을 즉시 생성하여 LLM 추론 (코딩)  
**갱신 주기**: 1년 1회 또는 필요 시

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
| 이미지 버전 | `1.0.0` | Central India | 갱신 시 증가 |
| 빌더 VM (임시) | `temp-golden-builder` | Central India | 빌드 후 삭제 |
| Managed Image (임시) | `axis-golden-image` | Central India | Gallery 등록 후 삭제 |
| 배포 VM | `llm-qwen-27b` | Central India | Spot, Delete 정책 |

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

# --- llama.cpp prebuilt binary ---
sudo mkdir -p /opt/llama
wget -O /tmp/llama.tar.gz \
  https://github.com/ggml-org/llama.cpp/releases/latest/download/llama-server-linux-x64.tar.gz
sudo tar -xzf /tmp/llama.tar.gz -C /opt/llama/
sudo chmod +x /opt/llama/llama-server /opt/llama/llama-cli
rm /tmp/llama.tar.gz

# --- Qwen 3.6 27B Q8_0 GGUF ---
sudo mkdir -p /opt/models
pip3 install huggingface-hub -q
huggingface-cli download ggml-org/Qwen3.6-27B-GGUF \
  --include "Qwen3.6-27B-Q8_0.gguf" \
  --local-dir /opt/models

# --- llama-server systemd service ---
sudo tee /etc/systemd/system/llama-server.service << 'EOF'
[Unit]
Description=llama.cpp LLM Server
After=network.target

[Service]
Type=simple
ExecStart=/opt/llama/llama-server \
  -m /opt/models/Qwen3.6-27B-Q8_0.gguf \
  -c 8192 \
  --port 8080 \
  --host 0.0.0.0 \
  --n-gpu-layers 0
Restart=on-failure
User=azureuser

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

# 이미지 버전 생성 (갱신 시 버전 번호 증가)
SUB=$(az account show --query id -o tsv)
az sig image-version create \
  --resource-group rg-devforge-prod-cin \
  --gallery-name NeuronGallery \
  --gallery-image-definition llm-qwen-27b-golden \
  --gallery-image-version 1.0.0 \
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
az vm create \
  --resource-group rg-devforge-prod-cin \
  --name llm-qwen-27b \
  --location centralindia \
  --image "/subscriptions/$(az account show --query id -o tsv)/resourceGroups/rg-devforge-prod-cin/providers/Microsoft.Compute/galleries/NeuronGallery/images/llm-qwen-27b-golden/versions/1.0.0" \
  --size Standard_FX2ms_v2 \
  --admin-username azureuser \
  --ssh-key-values ~/.ssh/id_rsa.pub \
  --priority Spot \
  --eviction-policy Delete \
  --os-disk-delete-option Delete \
  --public-ip-sku Standard
```

**예상 소요 시간**: VM 생성 2~3분 + 모델 mmap 로드 ~1분 = **총 ~3~4분**

---

## 3. 갱신 (1년 1회 또는 필요 시)

### 3.1 현재 버전 확인

```bash
az sig image-version list \
  --resource-group rg-devforge-prod-cin \
  --gallery-name NeuronGallery \
  --gallery-image-definition llm-qwen-27b-golden \
  --query "[].{Version:name, State:provisioningState}" \
  -o table
```

### 3.2 갱신 절차

1. 섹션 **1.1 ~ 1.7**을 반복
2. 갤러리/정의는 기존 것을 재사용
3. 이미지 버전만 증가: `1.0.0` → `1.1.0` → `1.2.0` ...

**갱신이 필요한 경우**:
- Qwen 새 버전 출시 (3.7, 3.8 등)
- Ubuntu 26.04 주요 보안 패치
- llama.cpp 중요한 성능 개선

**갱신이 필요 없는 경우**:
- 대부분의 상황. 1년간 방치해도 추론 성능에 영향 없음.

---

## 4. 비용

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

## 5. 네트워크 보안

| 방향 | 규칙 |
|------|------|
| DevForge → VM :8080 | NSG 허용 (DevForge 공인 IP 대역) |
| SSH :22 | 키 전용, DevForge IP 대역 |
| 그 외 | 전부 차단 |