#!/bin/bash
# yearly_refresh.sh — Golden Image 갱신 (수동, 대화형 확인)
# SSOT: docs/runbook-golden-image.md §1.1-§1.7 (hardened)
# Usage: ./yearly_refresh.sh [--dry-run]
set -euo pipefail

RG="rg-devforge-prod-cin"
LOCATION="centralindia"
BUILDER="temp-golden-builder"
IMAGE="axis-golden-image"
GALLERY="NeuronGallery"
IMAGE_DEF="llm-qwen-27b-golden"
VM_SIZE="Standard_FX2ms_v2"
BASE_IMAGE="UbuntuMinimal2604"
LLAMA_VER="${LLAMA_VER:-b4432}"
VERSION="${VERSION:-$(date +%Y.%m.0)}"
DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=true; fi

run() { if $DRY_RUN; then echo "[dry-run] $*"; else eval "$@"; fi; }

echo "=== Golden Image Yearly Refresh ==="
echo "Version: $VERSION  LLAMA_VER: $LLAMA_VER  DRY_RUN: $DRY_RUN"
if ! $DRY_RUN; then read -p "Continue? (y/N) " ans; [[ "$ans" == "y" ]] || exit 0; fi

echo "[1/7] Builder VM 생성"
run "az vm create --resource-group $RG --name $BUILDER --location $LOCATION --image $BASE_IMAGE --size $VM_SIZE --admin-username azureuser --ssh-key-values ~/.ssh/id_rsa.pub --os-disk-size-gb 64 --os-disk-type StandardSSD_LRS --os-disk-delete-option Delete"

if ! $DRY_RUN; then
  IP=$(az vm show -d -g "$RG" -n "$BUILDER" --query publicIps -o tsv)
  echo "Builder IP: $IP"
  echo "[2/7] Provision (hardened) — SSH $IP"
  ssh -o StrictHostKeyChecking=no azureuser@"$IP" bash <<REMOTE
set -euo pipefail
sudo apt update && sudo apt upgrade -y
sudo apt install -y wget curl git python3-pip unattended-upgrades
sudo dpkg-reconfigure -f noninteractive unattended-upgrades
sudo tee /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
sudo systemctl enable --now unattended-upgrades

# llama.cpp
LLAMA_VER="$LLAMA_VER"
sudo mkdir -p /opt/llama
wget -O /tmp/llama.tar.gz "https://github.com/ggml-org/llama.cpp/releases/download/\${LLAMA_VER}/llama-server-linux-x64.tar.gz"
sudo tar -xzf /tmp/llama.tar.gz -C /opt/llama/
sudo chmod +x /opt/llama/llama-server /opt/llama/llama-cli
rm /tmp/llama.tar.gz

# Qwen
sudo mkdir -p /opt/models
pip3 install huggingface-hub -q
huggingface-cli download ggml-org/Qwen3.6-27B-GGUF --include "Qwen3.6-27B-Q8_0.gguf" --local-dir /opt/models
sudo ln -sf /opt/models/Qwen3.6-27B-Q8_0.gguf /opt/models/model.gguf

# hardened systemd (docs §1.2)
sudo tee /etc/systemd/system/llama-server.service <<'EOF'
[Unit]
Description=llama.cpp LLM Server
After=network.target
Wants=network-online.target
[Service]
Type=simple
ExecStart=/opt/llama/llama-server -m /opt/models/model.gguf -c 8192 --port 8080 --host 127.0.0.1 --n-gpu-layers 0 --api-key \${LLAMA_API_KEY}
Restart=always
RestartSec=5
StartLimitBurst=3
StartLimitIntervalSec=60
DynamicUser=yes
StateDirectory=llama-server
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
sudo systemctl enable llama-server

# Caddy (443 -> 127.0.0.1:8080)
sudo mkdir -p /etc/caddy
sudo tee /etc/caddy/Caddyfile <<'EOF'
:443 {
    reverse_proxy 127.0.0.1:8080
}
EOF

# cleanup
sudo apt remove -y python3-pip || true
sudo apt autoremove --purge -y && sudo apt autoclean && sudo apt clean
sudo journalctl --vacuum-size=10M || true
sudo rm -rf /var/log/*.log /var/log/apt/*.log /tmp/* /var/tmp/* /home/azureuser/.cache/* /root/.cache/* || true
rm -rf ~/.cache/huggingface/* || true
REMOTE
  echo "[3/7] Deprovision"
  ssh azureuser@"$IP" "sudo waagent -deprovision+user -force" || true
fi

echo "[4/7] Deallocate + Generalize"
run "az vm deallocate --resource-group $RG --name $BUILDER"
run "az vm generalize --resource-group $RG --name $BUILDER"

echo "[5/7] Managed Image 캡처"
run "az image create --resource-group $RG --name $IMAGE --source $BUILDER --os-type Linux"

echo "[6/7] Gallery 등록"
SUB=$(az account show --query id -o tsv 2>/dev/null || echo "SUB_PLACEHOLDER")
if $DRY_RUN; then
  echo "[dry-run] az sig image-version create --resource-group $RG --gallery-name $GALLERY --gallery-image-definition $IMAGE_DEF --gallery-image-version $VERSION --managed-image /subscriptions/\$SUB/resourceGroups/$RG/providers/Microsoft.Compute/images/$IMAGE --target-regions $LOCATION --replica-count 1"
else
  az sig image-version create --resource-group "$RG" --gallery-name "$GALLERY" --gallery-image-definition "$IMAGE_DEF" --gallery-image-version "$VERSION" --managed-image "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Compute/images/$IMAGE" --target-regions "$LOCATION" --replica-count 1
  # smoke test는 별도 VM으로 수동 수행 (docs §8)
fi

echo "[7/7] 임시 리소스 정리"
run "az vm delete --resource-group $RG --name $BUILDER --yes --force-deletion"
run "az image delete --resource-group $RG --name $IMAGE"

echo "Done. Version $VERSION — verify with: az sig image-version show -g $RG --gallery-name $GALLERY --gallery-image-definition $IMAGE_DEF --gallery-image-version $VERSION"
