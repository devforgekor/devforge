#!/bin/bash
# yearly_refresh.sh — Golden Image 갱신 (수동/자동)
# SSOT: docs/runbooks/runbook-golden-image.md §1 (MoE baked-in, llm.service, --jinja)
# Usage: ./yearly_refresh.sh [--dry-run] [--yes] [--version YYYY.MM.N]
set -euo pipefail

RG="rg-devforge-prod-cin"
LOCATION="centralindia"
BUILDER="temp-golden-builder"
IMAGE="axis-golden-image"
GALLERY="gallery_devforge_prod_cin"
IMAGE_DEF="llm-qwen-27b"
VM_SIZE="Standard_FX2ms_v2"
BASE_IMAGE="Ubuntu2204"
LLAMA_VER="${LLAMA_VER:-b10919}"
MODEL_REPO="Qwen/Qwen3-30B-A3B-GGUF"
MODEL_FILE="Qwen3-30B-A3B-Q4_K_M.gguf"
MODEL_PATH="/opt/models/qwen3-30b-a3b-q4_k_m.gguf"
VERSION="${VERSION:-2026.09.3}"
PROVISION_TIMEOUT="${PROVISION_TIMEOUT:-5400}"
DRY_RUN=false
ASSUME_YES=false
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=true ;;
    --yes) ASSUME_YES=true ;;
  esac
done

run() { if $DRY_RUN; then echo "[dry-run] $*"; else eval "$@"; fi; }

echo "=== Golden Image Yearly Refresh ==="
echo "Version: $VERSION  LLAMA_VER: $LLAMA_VER  Model: $MODEL_FILE  DRY_RUN: $DRY_RUN"
if ! $DRY_RUN && ! $ASSUME_YES; then
  read -r -p "Continue? (y/N) " ans
  [[ "$ans" == "y" ]] || exit 0
fi

echo "[1/7] 기존 빌더 정리 + 생성"
# NOTE: regular FX quota=0 (StandardFXmsv2Family limit 0); only lowPriorityCores=3 available.
# Builder MUST be Spot (eviction-policy Deallocate preserves the OS disk/build progress).
run "az vm delete --resource-group $RG --name $BUILDER --yes 2>/dev/null || true"
run "az image delete --resource-group $RG --name $IMAGE 2>/dev/null || true"
run "az vm create --resource-group $RG --name $BUILDER --location $LOCATION --image $BASE_IMAGE --size $VM_SIZE --admin-username azureuser --ssh-key-values ~/.ssh/id_rsa.pub --os-disk-size-gb 64 --storage-sku StandardSSD_LRS --os-disk-delete-option Delete --zone 2 --security-type Standard --priority Spot --eviction-policy Deallocate --max-price -1"

if $DRY_RUN; then
  echo "[dry-run] skip provisioning/capture/register"
  exit 0
fi

IP=$(az vm show -d -g "$RG" -n "$BUILDER" --query publicIps -o tsv)
echo "Builder IP: $IP"

echo "[2/7] Provision (background on VM, poll log)"
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 azureuser@"$IP" "cat > /tmp/provision.sh" <<PROVISION
#!/bin/bash
set -euo pipefail
echo "[provision] start \$(date -u +%FT%TZ)"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -y
sudo apt-get upgrade -y
sudo apt-get install -y wget curl git unattended-upgrades ca-certificates libgomp1
sudo dpkg-reconfigure -f noninteractive unattended-upgrades
sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
sudo systemctl enable --now unattended-upgrades

echo "[provision] llama.cpp ${LLAMA_VER}"
sudo mkdir -p /opt/llama
if [ ! -x /opt/llama/llama-server ]; then
  curl -fL --retry 5 --retry-all-errors --retry-delay 5 -C - \\
    -o /tmp/llama.tar.gz \\
    "https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_VER}/llama-${LLAMA_VER}-bin-ubuntu-x64.tar.gz"
  sudo tar -xzf /tmp/llama.tar.gz -C /opt/llama --strip-components=1
  sudo chmod +x /opt/llama/llama-server /opt/llama/llama-cli
  rm -f /tmp/llama.tar.gz
else
  echo "[provision] llama-server present, skip download"
fi
sudo ln -sf /opt/llama/llama-server /usr/local/bin/llama-server
sudo ln -sf /opt/llama/llama-cli /usr/local/bin/llama-cli
/usr/local/bin/llama-server --version 2>&1 | head -1 || true

echo "[provision] model ${MODEL_FILE} (18.56GB, resumable)"
sudo mkdir -p /opt/models
if [ -f ${MODEL_PATH} ] && [ "\$(stat -c%s ${MODEL_PATH} 2>/dev/null || echo 0)" -ge 19000000000 ]; then
  echo "[provision] model present (complete), skip download"
else
  curl -fL --retry 20 --retry-all-errors --retry-delay 10 -C - \\
    -o /tmp/qwen3-30b-a3b-q4_k_m.gguf \\
    "https://huggingface.co/${MODEL_REPO}/resolve/main/${MODEL_FILE}?download=true"
  sudo mv /tmp/qwen3-30b-a3b-q4_k_m.gguf ${MODEL_PATH}
fi
sudo chown root:root ${MODEL_PATH}
sudo chmod 644 ${MODEL_PATH}

echo "[provision] llm.service (llm.service + --jinja + enable_thinking=false)"
sudo tee /etc/systemd/system/llm.service >/dev/null <<'EOF'
[Unit]
Description=llama.cpp LLM Server
After=network.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/llama-server \
  -m /opt/models/qwen3-30b-a3b-q4_k_m.gguf \
  -c 8192 \
  --port 8080 \
  --host 127.0.0.1 \
  --n-gpu-layers 0 \
  --jinja \
  --chat-template-kwargs '{"enable_thinking":false}' \
  --api-key \${LLAMA_API_KEY}
Restart=always
RestartSec=5
StartLimitBurst=3
StartLimitIntervalSec=60
DynamicUser=yes
StateDirectory=llm
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
sudo systemctl restart llm

echo "[provision] wait for llama-server /health"
ok=0
for i in \$(seq 1 90); do
  if curl -sf -m 5 http://127.0.0.1:8080/health >/dev/null 2>&1; then ok=1; echo "[provision] health OK (~\$((i*10))s)"; break; fi
  sleep 10
done
if [ "\$ok" != "1" ]; then
  echo "[provision] ERROR: llama-server health timeout"
  sudo systemctl --no-pager status llm || true
  sudo journalctl -u llm --no-pager -n 40 || true
  exit 1
fi
curl -s -m 5 http://127.0.0.1:8080/health; echo
echo "[provision] tool_calls smoke"
curl -s -m 120 http://127.0.0.1:8080/v1/chat/completions \\
  -H 'Content-Type: application/json' \\
  -d '{"messages":[{"role":"user","content":"Call the ping tool."}],"tools":[{"type":"function","function":{"name":"ping","parameters":{"type":"object","properties":{},"required":[]}}}],"tool_choice":"auto","max_tokens":64}' \\
  | head -c 500; echo

echo "[provision] caddy"
sudo apt-get install -y caddy >/dev/null 2>&1 || true
sudo mkdir -p /etc/caddy
sudo tee /etc/caddy/Caddyfile >/dev/null <<'EOF'
:443 {
    reverse_proxy 127.0.0.1:8080
}
EOF

echo "[provision] cleanup"
sudo systemctl stop llm
sudo apt-get autoremove --purge -y >/dev/null 2>&1 || true
sudo apt-get autoclean -y >/dev/null 2>&1 || true
sudo apt-get clean -y >/dev/null 2>&1 || true
sudo journalctl --vacuum-size=10M >/dev/null 2>&1 || true
sudo rm -rf /var/log/*.log /var/log/apt/*.log /home/azureuser/.cache/* /root/.cache/* 2>/dev/null || true
sudo rm -rf /var/lib/cloud/instances/* 2>/dev/null || true
echo "[provision] done \$(date -u +%FT%TZ)"
PROVISION

ssh -o StrictHostKeyChecking=no azureuser@"$IP" \
  "rm -f /tmp/provision.done /tmp/provision.exit; nohup bash -c 'bash /tmp/provision.sh; echo \$? > /tmp/provision.exit; touch /tmp/provision.done' > /tmp/provision.log 2>&1 & echo provision_started"

echo "  polling provision (timeout ${PROVISION_TIMEOUT}s, tail /tmp/provision.log)..."
deadline=$(( $(date +%s) + PROVISION_TIMEOUT ))
while :; do
  line=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 azureuser@"$IP" \
    "test -f /tmp/provision.done && echo DONE:\$(cat /tmp/provision.exit) || echo RUNNING; tail -n 1 /tmp/provision.log 2>/dev/null" 2>/dev/null || echo "SSH_ERR")
  echo "  $(date -u +%H:%M:%SZ) $line"
  case "$line" in
    DONE:0*) echo "  provision OK"; break ;;
    DONE:*) echo "  provision FAILED (exit=$(echo "$line" | sed 's/DONE://'))"; exit 1 ;;
  esac
  if (( $(date +%s) > deadline )); then echo "  provision TIMEOUT"; exit 1; fi
  sleep 30
done

echo "[3/7] Deprovision"
ssh -o StrictHostKeyChecking=no azureuser@"$IP" "sudo waagent -deprovision+user -force" || true

echo "[4/7] Deallocate + Generalize"
run "az vm deallocate --resource-group $RG --name $BUILDER"
run "az vm generalize --resource-group $RG --name $BUILDER"

echo "[5/7] Managed Image 캡처"
run "az image create --resource-group $RG --name $IMAGE --source $BUILDER --os-type Linux --hyper-v-generation V2"

echo "[6/7] Gallery 버전 등록 ($IMAGE_DEF:$VERSION)"
SUB=$(az account show --query id -o tsv)
run "az sig image-version create --resource-group $RG --gallery-name $GALLERY --gallery-image-definition $IMAGE_DEF --gallery-image-version $VERSION --managed-image /subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Compute/images/$IMAGE --target-regions $LOCATION --replica-count 1"

echo "[7/7] 임시 리소스 정리"
run "az vm delete --resource-group $RG --name $BUILDER --yes 2>/dev/null || true"
run "az image delete --resource-group $RG --name $IMAGE 2>/dev/null || true"

echo "Done. Version $VERSION — verify: az sig image-version show -g $RG --gallery-name $GALLERY --gallery-image-definition $IMAGE_DEF --gallery-image-version $VERSION"
