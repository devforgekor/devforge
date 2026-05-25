# Gemini Model Routing Strategy Comparative Analysis

> Based on empirical testing 2026-05-15

## Test Environment

- **Server**: OCI ARM 4ocpu 24GB (aarch64)
- **API**: Gemini Free Tier, 6 keys (3 accounts) rotation
- **Connection**: HTTPS proxy → Google direct IP (142.250.21.95)
- **Measurement method**: Python raw socket, round-trip time measurement, chunk decoding included

## Test Models

| Model | Code Name | Role |
|------|--------|------|
| gemini-2.5-flash | flash | Classifier + simple response |
| gemma-4-26b-a4b-it | 26b | Analysis/planning |
| gemma-4-31b-it | 31b | Code modification |

## Question Types

| Type | Content |
|------|------|
| Simple search | "How to find recently modified files with Linux find command, brief" |
| Analysis/planning | "Analyze this server's container configuration and plan improvements" |
| Code modification | "Find and fix the bug in this function: def div(a,b): return a/b" |

## Response Time by Model

```
Question Type    flash      26b         31b
────────────────────────────────────────────
Simple search    3.3s (21)    23.8s (1251)  21.3s (796)
Analysis/plan    2.5s (20)    53.8s (2385)  37.3s (2102)
Code modify      2.0s (21)    41.7s (2314)  25.2s (1345)
```
*(Values in parentheses are response character counts)*

**Key findings**:
- flash: Responds quickly to all questions at 2-3s, but for complex questions recognizes its limits and responds briefly around 20 chars
- 26b: Takes 24-54s, chain-of-thought based detailed analysis (1251-2385 chars)
- 31b: Takes 21-37s, faster than 26b for code tasks (796-2102 chars)

## Approach Comparison

### Approach A: flash-first (Current strategy)
```
Simple search:   flash immediate response                   →  3.3s
Analysis/plan:   flash recognizes limit (2.5s) → 26b retry (53.8s)  → 56.3s
Code modify:     flash recognizes limit (2.0s) → 31b retry (25.2s)  → 27.2s
                                                       Total: 86.9s
```

### Approach B: Gemini CLI auto-routing (NumericalClassifierStrategy)
```
Simple search:   classifier (3.3s) → flash (3.3s)         →  6.6s
Analysis/plan:   classifier (3.3s) → 26b (53.8s)          → 57.1s
Code modify:     classifier (3.3s) → 31b (25.2s)          → 28.6s
                                                       Total: 92.3s
```

### Results

| Metric | flash-first | auto-routing | Difference |
|------|-------------|--------------|------|
| Total time | **86.9s** | 92.3s | **+5.4s (6.2%)** |
| Simple question response speed | **3.3s** | 6.6s | 2x slower |
| Complex question overhead | 2-3s (flash preceding) | 3.3s (classifier) | Similar |

## Winner: flash-first

### Reasons

1. **2x faster for simple questions** — flash answers directly, no classifier API call needed
2. **Minimal difference for complex questions** — 26b/31b response at 25-54s dominates total time, initial overhead difference (2-3s vs 3.3s) has minimal impact on overall time
3. **flash as natural classifier** — responds "cannot do this directly" in 2s to complex requests, user can decide `/model` switch
4. **API call savings** — replaces 1 classifier call with flash direct response → efficient free quota usage

### Cost-benefit analysis (assuming 50 daily uses)

| Scenario | flash-first | auto-routing |
|----------|-------------|--------------|
| Simple questions 30 | 30 API calls | 60 (classifier+routing) |
| Complex questions 20 | 40 (flash+heavy) | 40 (classifier+heavy) |
| Daily total calls | **70 calls** | 100 calls |
| Time saved | — | **+30% calls, +6% time** |

## Application Method

1. **Default**: `gemini-2.5-flash` (first entry point for all questions)
2. **When analysis/planning needed**: `/model gemma-4-26b-a4b-it`
3. **When code modification needed**: `/model gemma-4-31b-it`

Switch instantly with `/model` command during session. No restart required.

## Related Files

- `~/.gemini/header.md` — routing strategy document
- `/opt/projects/server/scripts/gemini_rotate.py` — key rotation + headless execution
- `/opt/projects/server/scripts/gemini_session_start.sh` — tmux session (flash-first)
- `/opt/projects/server/scripts/gemini_proxy.py` — HTTPS proxy (per-request key rotation)
- `/var/tmp/gemini_compare.json` — empirical data source
