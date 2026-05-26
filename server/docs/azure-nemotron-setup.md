# Azure Nemotron-3-Nano-30B-A3B MoE Spot VM Setup

## Overview
- **VM**: `vm-devforge-llm-prod-cin` (Standard FX2mds v2 — 2 vCPU, 42 GiB RAM)
- **Region**: Central India
- **Image**: Ubuntu Minimal 22.04 LTS Gen2
- **Model**: Nemotron-3-Nano-30B-A3B MoE (IQ4_XS, ~19GB, Unsloth GGUF)
- **Server**: llama.cpp llama-server (latest master, OpenBLAS)
- **Port**: 400 (privileged, setcap)
- **SSH Key**: `~/.ssh/vm-azure-nvidia-nemotron3-nano-30B-key.pem`
- **SSH Alias**: `ssh azurenemo`

## Architecture
```
Azure Central India (Spot VM)
├─ llama-server :400 (systemd)
│  └─ Nemotron-3-Nano-30B-A3B-IQ4_XS.gguf (19GB)
├─ CPU: Intel Xeon Platinum 8573C (AVX512, AMX_INT8)
└─ API: OpenAI-compatible /v1/chat/completions
```

## Quick Connect
```bash
ssh azurenemo                                  # SSH alias
curl -s http://localhost:400/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"nemotron","messages":[{"role":"user","content":"Hello"}],"max_tokens":50,"chat_template_kwargs":{"enable_thinking":false}}'
```

**Important**: Always pass `chat_template_kwargs: {enable_thinking: false}` to disable reasoning mode. Without this, max_tokens < 50 produces empty `content` (all tokens consumed by reasoning trace).

## Setup Steps (from blank Ubuntu 24.04 image)

### 1. Install dependencies
```bash
sudo apt update && sudo apt install -y build-essential cmake git libopenblas-dev
```

### 2. Build llama.cpp
```bash
git clone --depth 1 https://github.com/ggerganov/llama.cpp
cd llama.cpp && mkdir build && cd build
cmake .. -DLLAMA_CUDA=OFF -DLLAMA_BLAS=ON -DLLAMA_BLAS_VENDOR=OpenBLAS
make -j$(nproc) && sudo make install && sudo ldconfig
```

### 3. Download model
```bash
mkdir -p ~/Nemotron-3-Nano-30B-A3B-GGUF && cd ~/Nemotron-3-Nano-30B-A3B-GGUF
curl -L -o Nemotron-3-Nano-30B-A3B-IQ4_XS.gguf \
  'https://huggingface.co/unsloth/NVIDIA-Nemotron-3-Nano-Omni-30B-A3B-Reasoning-GGUF/resolve/main/NVIDIA-Nemotron-3-Nano-Omni-30B-A3B-Reasoning-UD-IQ4_XS.gguf'
```
**Note**: IQ4_XS (19GB) chosen over Q4_K_M (24GB) to fit 29GB OS disk with comfortable margin.

### 4. Create systemd service
```bash
sudo tee /etc/systemd/system/llama-server.service << 'EOF'
[Unit]
Description=LLaMA.cpp Server - Nemotron-3-Nano-30B-A3B MoE
After=network.target

[Service]
Type=simple
User=azureuser
WorkingDirectory=/home/azureuser
ExecStart=/usr/local/bin/llama-server \
  -m /home/azureuser/Nemotron-3-Nano-30B-A3B-GGUF/Nemotron-3-Nano-30B-A3B-IQ4_XS.gguf \
  -c 2048 --port 400 --host 0.0.0.0 -t 2
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo setcap 'cap_net_bind_service=+ep' /usr/local/bin/llama-server
sudo systemctl daemon-reload
sudo systemctl enable --now llama-server
```

### 5. Cleanup (after verifying service works)
```bash
rm -rf ~/llama.cpp
sudo apt clean
sudo apt remove -y build-essential cmake git gcc g++ make
# CRITICAL: Keep runtime libraries! Do NOT autoremove blindly.
sudo apt-mark manual libgomp1 libopenblas0 libopenblas0-pthread libstdc++6
sudo apt autoremove -y
# Verify no missing libs: ldd /usr/local/bin/llama-server | grep "not found"
```

### 6. Deprovision for image capture
```bash
sudo waagent -deprovision+user -force
```

## Performance
| Metric | Value |
|--------|-------|
| Model size | 19 GB |
| Memory usage | ~17 GB |
| Prompt speed | ~13 tok/sec |
| Generation speed | ~5.5 tok/sec |
| Context | 2048 tokens |

## Known Issues
- **Reasoning mode (default ON)**: Model generates `reasoning_content` by default. `content` is empty when max_tokens < 50 (all tokens consumed by reasoning trace). **Fix**: Pass `chat_template_kwargs: {enable_thinking: false}` in API request to disable reasoning mode entirely.
- **apt autoremove will break the server**: `autoremove` removes `libgomp1` (OpenMP runtime) which llama-server depends on. After cleanup, MUST `apt-mark manual libgomp1 libopenblas0 libopenblas0-pthread` to protect these. Symptom: service exit code 127.
- **Disk**: IQ4_XS chosen over Q4_K_M to leave 7.8GB free on 29GB OS disk.
- **Architecture**: `nemotron_h_moe` — requires latest llama.cpp (master branch).

## Model Sources
| Source | Status | Notes |
|--------|--------|-------|
| `unsloth/NVIDIA-Nemotron-3-Nano-Omni-30B-A3B-Reasoning-GGUF` | Used | IQ4_XS quant (19GB) |
| `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16` | Reference | Full BF16 (61.5GB) |

## Files
- Key: `~/.ssh/vm-azure-nvidia-nemotron3-nano-30B-key.pem`
- Config: `~/.ssh/config` (Host azurenemo)
- Service: `/etc/systemd/system/llama-server.service`
- Model: `/home/azureuser/Nemotron-3-Nano-30B-A3B-GGUF/Nemotron-3-Nano-30B-A3B-IQ4_XS.gguf`
