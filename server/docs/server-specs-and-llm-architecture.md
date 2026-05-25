# DevForge — Server Specifications & LLM Architecture

## Hardware

| Component | Detail |
|-----------|--------|
| Platform | Oracle Cloud Infrastructure (OCI) ARM |
| CPU | Neoverse-N1 (aarch64), 4 cores, 1 thread/core, 1 socket |
| RAM | 24 GB (22 GB usable) |
| Swap | 8 GB (4 GB ZRAM + 4 GB LVM swap) |

## Storage

| Mount | Size | Used | Purpose |
|-------|------|------|---------|
| `/mnt/lv_db` | 30 GB | 365 MB | PostgreSQL data |
| `/mnt/secure_meta` | 4.5 GB | 1.3 GB | DB backups, archives |
| `/opt/projects` | 10 GB | 720 MB | Application code |
| `/opt/ai_data` | 100 GB | 65 GB | LLM models, sessions, test artifacts |

## Operating System

- **OS**: Oracle Linux 9.7 (aarch64)
- **Kernel**: UEK7 6.12.0-201.74.2.3
- **Runtime**: Podman rootless (user `opc`), Quadlet systemd
- **Database**: PostgreSQL 16 with `pgvector` 0.8.2 (`pg_trgm`, JSONB, vector(768))
- **Proxy**: Caddy (host network, auto-HTTPS, reverse proxy `/devforge/*` → `localhost:8000`)

## LLM Models

| Model | Size | Quantization | File |
|-------|------|-------------|------|
| Qwen2.5-Coder-32B-Instruct | 20 GB | IQ4_XS | `Qwen2.5-Coder-32B-Instruct-IQ4_XS.gguf` |
| Qwen2.5-Coder-14B-Instruct | 8.4 GB | Q4_K_M | `qwen2.5-coder-14b-instruct-q4_k_m.gguf` |
| Agentica DeepCoder-14B | 8.4 GB | Q4_K_M | `agentica-org_DeepCoder-14B-Preview-Q4_K_M.gguf` |
| Phi-4 (14B) | 8.3 GB | Q4_K_M | `phi-4-Q4_K_M.gguf` |
| Selene Mini (8B) | 4.6 GB | Q4_K_M | `selene-1-mini-llama-3.1-8b-q4_k_m.gguf` |
| Phi-4-mini (4B) | 3.9 GB | Q8_0 | `Phi-4-mini-instruct.Q8_0.gguf` |
| Qwen3-4B | 2.4 GB | Q4_K_M | `Qwen3-4B-Q4_K_M.gguf` |
| Qwen2.5-0.5B-Instruct | 469 MB | Q4_K_M | `qwen2.5-0.5b-instruct-q4_k_m.gguf` |

## Container Architecture

```
┌─────────────────────────────────────────────────────────┐
│ Podman B: container-devforge-swap (mode-switchable)     │
│   Ports: 8081-8082    Image: llama.cpp:server           │
│   CPU: 4 threads   Memory: varies by mode               │
│                                                         │
│   Mode: code     → :8081 = Qwen32B IQ4_XS               │
│   Mode: batch   → :8081 = Phi-4 14B Q4_K_M             │
│   Mode: normal  → :8081 = Phi-4-mini, :8082 = Selene   │
│   Mode: debate  → :8081 = supervisor, :8082 = Selene   │
│   Mode: discussion → :8081 = supervisor (dynamic)       │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ Podman A: container-devforge-qwen (auxiliary)           │
│   Port: 8080          Image: llama.cpp:server           │
│   Model: Qwen3-4B Q4_K_M                                │
│   Active only in normal/discussion mode                 │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ data-pod                                                │
│   └─ postgres :5432    Image: localhost/devforge-...    │
│      Database: devforge_app                             │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ container-devforge-api (standalone)                     │
│   Port: 127.0.0.1:8000  Image: localhost/devforge-...  │
│   Routes: POST /ingest, GET /stats, /health             │
│   MCP SSE: mem_save, mem_search                         │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ Caddy (rootful, host network)                           │
│   Ports: :443 (HTTPS)                                   │
│   /devforge/* → localhost:8000                          │
│   netdata → localhost:19999                             │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ Netdata (rootless, host PID)                            │
│   Port: 19999   System monitoring dashboard             │
└─────────────────────────────────────────────────────────┘
```

## Mode Switching

Modes are controlled via `/opt/ai_data/scripts/current-mode.env`:

| Mode | Container B (:8081) | Container B (:8082) | Container A (:8080) | Use Case |
|------|--------------------|--------------------|--------------------|----------|
| **code** | Qwen32B IQ4_XS (ctx=16384, cache-ram=6656, mlock) | — | stopped | 4-stage pipeline |
| **debate** | supervisor (Phi-4-14B + 32B) | Selene Mini 8B | stopped | 3-LLM debate |
| **batch** | Phi-4 14B Q4_K_M (ctx=8192) | — | stopped | Review worker |
| **normal** | Phi-4-mini Q8_0 | Selene Mini 8B | Qwen3-4B | Daily operations |
| **discussion** | supervisor (dynamic swap) | — | Phi-4 14B | Interactive discussion |

Mode switch procedure: write mode to env file → restart container B → wait for health check.

## 4-Stage Pipeline (code mode)

All stages run on local Qwen2.5-Coder-32B (IQ4_XS):

| Stage | Name | Description | Token Budget |
|-------|------|-------------|--------------|
| 1 | ANALYZE | Full code analysis, identify affected sections | Full file (~4K-13K tokens) |
| 2 | PLAN | Design minimal change approach | Sliced code (~300-500 tokens) |
| 3 | IMPLEMENT | Produce unified diff | Sliced code (~400-600 tokens) |
| 4 | PACKAGE | JSON package for downstream consumption | Minimal (~500 tokens) |

Code slicing: Stage 1 outputs `affected_sections` (function:line_range). Stages 2-4 receive only the affected code regions (±3 lines before, ±5 lines after context padding), achieving 89-92% token reduction.

Dynamic timeout via RateEstimator: `(prompt_tokens / measured_prompt_rate) + (max_tokens / measured_gen_rate) + buffer`.

llama.cpp flags for 32B:
```
--ctx-size 16384 --cache-ram 6656 --kv-unified --cache-idle-slots
--mlock --batch-size 1024 --threads 4 --threads-batch 4
--temp 0.1 --timeout 28800 --parallel 1 --no-warmup
```

## Debate Architecture (debate mode)

3-LLM dual-judge debate:

```
Round 1-4 (Phi-4-14B cycles):
  :8082 Selene Mini 8B → Neutral history summary (fixed)
  :8081 supervisor:
    Phi-4-14B → Proposal A
    Phi-4-14B → Proposal B (reload with different seed)

Round 5 (Synthesis):
  32B IQ4_XS → Synthesis A
  Phi-4-14B → Synthesis B
  
Evaluation:
  DeepSeek Pro (API) → Final score
```

## API Integrations

| API | Model | Purpose |
|-----|-------|---------|
| DeepSeek API (`api.deepseek.com`) | `deepseek-chat` (v4-pro) | Pipeline Stage 2 (--with-api), evaluation |
| DeepSeek API | `deepseek-chat` (v4-flash) | Aider code assistance |
| GitHub REST API | — | Reference tracking, issue/PR monitoring |

## Network

```
Bridge: devforge-net (10.89.0.0/24)
SSH: 22/tcp (key-only authentication)
Internal APIs: 127.0.0.1 only
```

## Performance Benchmarks (32B IQ4_XS on 4-core Neoverse-N1)

| Metric | Value |
|--------|-------|
| Prompt evaluation rate | ~1.9 tok/s |
| Token generation rate | ~1.4 tok/s |
| Model load time (warmup) | ~7-22 seconds |
| Stage 1 (12K tokens) | ~6,400 seconds (1h 47m) |
| Stage 2-4 (sliced) | ~1,200 seconds (20m) total |
| Full 4-stage (T11, large task) | ~8,300 seconds (2h 19m) |
| Small task (T01, 4K tokens) | ~2,200 seconds (37m) |

Memory pressure: 20 GB model + 6.5 GB KV cache > 22 GB physical RAM → ~1.3 GB swap used under load.

## Key Scripts

| Script | Purpose |
|--------|---------|
| `code_mod_pipeline.py` | 32B 4-stage code modification pipeline |
| `debate.py` | 3-LLM dual-judge debate orchestrator |
| `review_worker.py` | 2-phase LLM review (extract → verify) |
| `collect_turns.py` | Multi-source session collection (15min timer) |
| `link_turns.py` | Turn-to-worklog matching (nightly) |
| `auto_commit_guard.py` | Git auto-commit safety net |
| `gen_server_state.py` | Live state.yaml generation (15min timer) |
| `telegram_bot.py` | Telegram notification bot |
| `eval_comparison.py` | DeepSeek Pro comparison test evaluator |
| `run_comparison_tests.sh` | 6-run local comparison test runner |
