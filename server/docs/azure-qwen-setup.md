# Azure Qwen3-30B-A3B MoE Spot VM Setup

## Overview
- **VM**: `vm-devforge-prod-cin` (Standard FX2mds v2 — 2 vCPU, 42 GiB RAM)
- **Region**: Central India
- **Image**: Ubuntu Minimal 22.04 LTS Gen2
- **Model**: Qwen3-30B-A3B MoE (Q4_K_M, ~18GB, official Qwen repo)
- **Server**: llama.cpp llama-server (latest master, OpenBLAS)
- **Port**: 400 (privileged, setcap)
- **SSH Key**: `~/.ssh/vm-azure-qwen-30B-A3B-key.pem`
- **SSH Alias**: `ssh azureqwen`

## Architecture
```
Azure Central India (Spot VM)
├─ llama-server :400 (systemd)
│  └─ Qwen3-30B-A3B-Q4_K_M.gguf (18GB)
├─ CPU: Intel Xeon Platinum 8573C (AVX512, AMX_INT8)
└─ API: OpenAI-compatible /v1/chat/completions
```

## Quick Connect
```bash
ssh azureqwen                                  # SSH alias
curl -s http://localhost:400/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen","messages":[{"role":"user","content":"Hello"}],"max_tokens":20}'
```

## Setup Steps (from blank Ubuntu 22.04 image)

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
mkdir -p ~/Qwen3-30B-A3B-GGUF && cd ~/Qwen3-30B-A3B-GGUF
curl -L -o Qwen3-30B-A3B-Q4_K_M.gguf \
  'https://huggingface.co/Qwen/Qwen3-30B-A3B-GGUF/resolve/main/Qwen3-30B-A3B-Q4_K_M.gguf'
```
**Note**: Use official Qwen repo (`Qwen/Qwen3-30B-A3B-GGUF`), not unsloth/bartowski.

### 4. Create systemd service
```bash
sudo tee /etc/systemd/system/llama-server.service << 'EOF'
[Unit]
Description=LLaMA.cpp Server - Qwen3-30B-A3B MoE
After=network.target

[Service]
Type=simple
User=azureuser
WorkingDirectory=/home/azureuser
ExecStart=/usr/local/bin/llama-server \
  -m /home/azureuser/Qwen3-30B-A3B-GGUF/Qwen3-30B-A3B-Q4_K_M.gguf \
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

### 5. Deprovision for image capture
```bash
sudo waagent -deprovision+user -force
```
VM auto-shuts down. Capture image in Azure Portal: VM → Capture → Image.

## Performance
| Metric | Value |
|--------|-------|
| Model size | 18 GB |
| Memory usage | ~4.5 GB (model) + KV cache |
| Prompt speed | ~14 tok/sec |
| Generation speed | ~6.5 tok/sec |
| Context | 2048 tokens |

## Known Issues
- **llama.cpp `deepseek2` architecture crash**: Latest llama.cpp has `GGML_ASSERT(*cur_backend_id != -1)` bug with deepseek2/GLM-4.7 models on CPU-only. Qwen3-30B-A3B uses `qwen3moe` architecture — not affected.
- **ollama 0.24.0**: Does NOT support glm4moe architecture. SIGSEGV on tensor load.
- **Port < 1024**: Requires `setcap cap_net_bind_service=+ep` or root.
- **Disk**: OS disk minimum 64 GB (model 18 GB + OS + build artifacts).

## Model Sources
| Source | Status | Notes |
|--------|--------|-------|
| `Qwen/Qwen3-30B-A3B-GGUF` | Official, used | Most reliable |
| `unsloth/Qwen3-30B-A3B-GGUF` | Works, not used | Different quantization |
| `bartowski/Qwen3-30B-A3B-GGUF` | Gated (401) | Requires HF auth |

## Image Usage
After capturing image to Azure Compute Gallery:
1. Create VM from gallery image `img-qwen3-30b-a3b`
2. VM boots → llama-server auto-starts on port 400
3. Ready in ~30 seconds

## Files
- Key: `~/.ssh/vm-azure-qwen-30B-A3B-key.pem`
- Config: `~/.ssh/config` (Host azureqwen)
- Service: `/etc/systemd/system/llama-server.service`
- Model: `/home/azureuser/Qwen3-30B-A3B-GGUF/Qwen3-30B-A3B-Q4_K_M.gguf`
