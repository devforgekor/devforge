# Language: English only — machine-readable per llm-common-rule.md §Communication
# Code File Refactoring Plan — 400+ Line Files & Directory Structure

Date: 2026-06-06 | Status: completed (refactoring finished by Junie on 2026-06-13)

## Rationale

- `scripts/` flat directory holds 137 .py files with no visible responsibility separation.
- `lib/` already has clean domain subdirectories (auth, code_mod, infra, llm, output, parsers, search, state, tracking) — underutilized.
- 24 .py files exceed 400 lines. Code itself is not a rule violation (400-line limit applies to human-facing docs, not code), but splitting improves readability and aligns with "architecture visible from directory structure" principle.
- **Trigger**: execute only AFTER 5-phase experiment completes, file structure stabilizes, and real deployment is verified. Current pipeline is in active testing — structure may shift.

## Related: Naming Audit

→ All violations fixed in Session 33. Lint rules in `lint_rules.py` + `cli.py lint` enforce naming conventions automatically. Original audit in `docs/_archive/plans/naming-audit.md`.

- Model names hardcoded in file/function/variable names (13 files, 8 functions, 10+ variables)
- Single-letter role abbreviations (P/R/J → Proposer/Reflector/Judge)
- Ambiguous abbreviations (ts, _est_tok, esc_sql, fb_*, mcp_pt/gt/em, etc.)
- File names that don't reveal responsibility (prj_cycle, refs, observer, shared, etc.)

**Naming fixes should be applied DURING the file-split refactoring, not separately.**
When a function moves to a new file, rename it to clear English at the same time.
When a pipeline file moves to `scripts/pipelines/`, remove model names from the filename.

## Current State (2026-06-06)

```
scripts/  (137 .py files, flat)
├── prj_cycle.py           2,142  ← biggest file
├── anthropic_proxy.py     1,381
├── code_mod_pipeline.py   1,286
├── hybrid_pipeline.py     1,014
├── cli.py                   912
├── test_27b_optimization.py 769
├── extract_pipeline.py      746
├── rubric_experiment.py     706
├── local_debate.py          699
├── experiment_runner.py     632
├── night_cycle.py        626
├── azure_spot.py            626
├── observer.py              574
├── feedback.py              554
├── orchestrator.py          484
├── review_pipeline_steps.py 467
├── classify_pipeline.py     461
├── gemini_proxy.py          459
├── search_proxy.py          451
├── test_operator.py         440
├── phase_tracker.py         439
├── verify_refactoring.py    416
├── blob_explorer.py         408
├── worklog_generator.py     404
├── ... 113 files under 400 lines
└── lib/  (8 domain subdirectories, already well-structured)
    ├── auth/          (api_key_cipher, key_loader, key_rotator)
    ├── code_mod/      (shared)
    ├── infra/         (containers, health_checks, storage, subprocess)
    ├── llm/           (client, json_parser, rate_estimator)
    ├── output/        (claude_yaml, memory_line, motd, validation, yaml_io)
    ├── parsers/       (claude, copilot, opencode — aider/gemini retired 2026-09-19)
    ├── search/        (manager)
    ├── state/         (changelog, diff, state_io)
    └── tracking/      (agent_names, dependency_tracker, phase_tracker)
```

**Note**: Some `lib/` files have duplicate shims (e.g. `lib/parser_claude.py` and `lib/parsers/claude.py`). These are intentional backward-compat shims — not addressed here.

## Root Cause Analysis

`prj_cycle.py` (2,142 lines) is the canary: it mixes three distinct concerns that should live in different modules:

1. **Infrastructure** (~300 lines): pod lifecycle (`kill_all`, `start_pod_a`, `start_pod_b`, `wait_health`, `wait_probe`, `ensure_model`, `slack_send`) — no pipeline logic, pure infrastructure.
2. **State machine** (~280 lines): `class TokenBudget`, `class PipelineState` — data structures that other pipelines could reuse.
3. **Pipeline orchestration** (~1,562 lines): P-R-J cycle, LLM calls, rubric evaluation, handoff compilation — the actual pipeline logic.

Same pattern in other large files: infrastructure code mixed with business logic, no clear domain boundary.

---

## Phase 1: High-ROI File Splits (3 files → lib/ additions)

**Principle**: split infrastructure/utility code into `lib/` subdirectories, keeping pipeline logic in-place. No logic changes — pure extraction.

### 1.1 prj_cycle.py (2,142 → ~1,242 retained)

**Infrastructure functions to extract** → `lib/infra/pod_manager.py`

| Function | Lines | Purpose |
|----------|-------|---------|
| `slack_send()` | 18 | Send Slack notification |
| `ts()` | 4 | UTC timestamp string |
| `log()` | 4 | Timestamped print |
| `load_input()` | 13 | Read input JSON from args |
| `wait_health(port, timeout)` | 14 | Wait for HTTP health endpoint |
| `wait_probe(port, model_name, timeout)` | 31 | Wait for model probe endpoint |
| `kill_all()` | 24 | Kill all llama-server processes |
| `_reclaim_memory()` | 17 | sync + drop_caches + 15s wait |
| `start_pod_b(mode, port)` | 18 | Start Pod B (7B model) |
| `start_pod_a(mode, port)` | 16 | Start Pod A (3B model) |
| `start_day_both()` | 25 | Start both pods for day mode |
| `ensure_model(physical_name)` | 20 | Ensure model file exists |
| `_est_tok(text)` | 15 | Rough token estimator |
| **Total** | **~219** | |

**State classes to extract** → `lib/llm/token_budget.py`

| Class | Lines | Purpose |
|-------|-------|---------|
| `class TokenBudget` | 64 | Phase-based token budget with priority allocation |
| `class PipelineState` | 217 | Pipeline state machine, context building |
| **Total** | **~281** | |

**prj_cycle.py retained** (1,242 lines): P-R-J cycle core only.

```python
from lib.infra.pod_manager import (
    kill_all, start_pod_a, start_pod_b, start_day_both,
    wait_health, wait_probe, ensure_model, _est_tok,
    slack_send, ts, log, load_input, _reclaim_memory,
)
from lib.llm.token_budget import TokenBudget, PipelineState
```

**Caller impact**:
- `experiment_runner.py` imports `from prj_cycle import kill_all, start_day_both, ...` → update import to `from lib.infra.pod_manager import ...`
- `night_cycle.py` imports `from prj_cycle import start_pod_b, ...` → update import

---

### 1.2 anthropic_proxy.py (1,381 → ~781 retained)

**Utility functions to extract** → `lib/proxy_utils.py`

| Function group | Approx. lines | Purpose |
|----------------|---------------|---------|
| `_flatten_text()` | 18 | Recursively flatten content to string |
| `_strip_cache_control()` | 20 | Remove cache_control from payload |
| `_strip_system_billing_header()` | 35 | Remove billing header from system messages |
| `_flatten_system_blocks()` | 16 | Flatten nested system content blocks |
| `_sanitize_messages()` | 75 | Message sanitization |
| `_fix_orphan_tool_results()` | 45 | Fix orphaned tool results |
| `_collect_tool_use_ids_present()` | 20 | Gather tool_use IDs from messages |
| `_strict_tool_adjacency_fix()` | 130 | Enforce tool_use/tool_result adjacency |
| `_apply_cache_padding()` | 15 | Append padding for DeepSeek prefix cache |
| `_extract_stream_usage()` | 35 | Extract usage from SSE stream tail |
| `_format_context_bar()` | 15 | Render 10-segment context usage bar |
| `_fetch_deepseek_balance()` | 30 | Fetch DeepSeek CNY balance |
| `log_usage()` | 85 | Pretty-print usage with cache hit rate + bar |
| `filter_response_headers()` | 20 | Filter hop-by-hop headers |
| `_json_dumps_system_first()` | 15 | Custom JSON serialization |
| **Total** | **~574** | |

**anthropic_proxy.py retained** (781 lines): `class ProxyHandler`, `_forward()`, truncation logic, insurance logic, `do_GET/POST/PUT/PATCH/DELETE`, `main()`.

```python
from lib.proxy_utils import (
    _flatten_text, _strip_cache_control, _strip_system_billing_header,
    _flatten_system_blocks, _sanitize_messages, _fix_orphan_tool_results,
    _collect_tool_use_ids_present, _strict_tool_adjacency_fix,
    _apply_cache_padding, _extract_stream_usage, _format_context_bar,
    _fetch_deepseek_balance, log_usage, filter_response_headers,
    _json_dumps_system_first,
)
```

**Caller impact**: None. `anthropic_proxy.py` is called by systemd service `ExecStart=/usr/bin/python3 /opt/projects/server/scripts/anthropic_proxy.py` — path unchanged.

---

### 1.3 cli.py (912 → ~562 retained)

**Command groups to extract**:

| New file | Functions | Lines | Purpose |
|----------|-----------|-------|---------|
| `lib/cli_worklog.py` | `cmd_worklog_add`, `cmd_worklog_recent`, `cmd_worklog_search` | ~150 | Worklog CRUD commands |
| `lib/cli_experiment.py` | `cmd_experiment_list`, `cmd_experiment_compare`, `cmd_experiment_active`, `cmd_experiment_adopt` | ~200 | Experiment registry commands |

**Retained in cli.py**: `cmd_activity_*`, `cmd_auto_*`, `cmd_discussion`, `cmd_upload`, `cmd_extract`, `cmd_mcp_consume`, `cmd_dashboard`, `_format_results`, `_switch_mode`, `_container_in_review_mode`, `_find_pipeline_output`, `_read_auto_tasks`, `_write_auto_tasks`, `main()` dispatch.

```python
from lib.cli_worklog import cmd_worklog_add, cmd_worklog_recent, cmd_worklog_search
from lib.cli_experiment import (
    cmd_experiment_list, cmd_experiment_compare,
    cmd_experiment_active, cmd_experiment_adopt,
)
```

**Caller impact**: None. `cli.py` called by `night_cycle.sh`, `.bashrc` aliases, systemd timer scripts via `python3 scripts/cli.py <subcommand>`. CLI interface unchanged.

---

## Phase 2: Pipeline Domain Directory (`scripts/pipelines/`)

Goal: "these are pipelines" becomes visible from directory structure. Current flat layout forces developers to read file contents to understand and categorize each file.

### Files to move (no code changes, import path updates only)

| Current (flat) | Target (pipelines/) | Lines | Pipeline role |
|----------------|---------------------|-------|---------------|
| `scripts/extract_pipeline.py` | `scripts/pipelines/extract.py` | 746 | Extract findings from raw turns |
| `scripts/classify_pipeline.py` | `scripts/pipelines/classify.py` | 461 | Classify findings by severity/type |
| `scripts/review_pipeline_steps.py` | `scripts/pipelines/review.py` | 467 | Review pipeline step execution |
| `scripts/hybrid_pipeline.py` | `scripts/pipelines/hybrid.py` | 1,014 | Hybrid local+web pipeline (experimental) |
| `scripts/code_mod_pipeline.py` | `scripts/pipelines/code_mod.py` | 1,286 | Code modification pipeline |
| `scripts/rubric_experiment.py` | `scripts/pipelines/rubric.py` | 706 | Rubric-based experiment |
| `scripts/experiment_runner.py` | `scripts/pipelines/runner.py` | 632 | 5-phase experiment runner |
| `scripts/night_cycle.py` | `scripts/pipelines/night.py` | 626 | Nightly batch pipeline |

### Import path update registry

Callers that import from these pipelines:

| Caller | Imports | New import |
|--------|---------|------------|
| `prj_cycle.py` | `import extract_pipeline` | `from scripts.pipelines import extract` |
| `prj_cycle.py` | `import classify_pipeline` | `from scripts.pipelines import classify` |
| `orchestrator.py` | `from review_pipeline_steps import ...` | `from scripts.pipelines.review import ...` |
| `night_cycle.sh` | `python3 scripts/experiment_runner.py` | `python3 scripts/pipelines/runner.py` |
| `night_cycle.sh` | `python3 scripts/night_cycle.py` | `python3 scripts/pipelines/night.py` |
| systemd timers | `ExecStart=.../night_cycle.py` | Update timer unit files |

### Backward-compat shim

Create `scripts/pipelines/__init__.py` with re-exports so `from scripts.pipelines import extract` still works:

```python
# scripts/pipelines/__init__.py
# Re-export for backward compat — allows:
#   from scripts.pipelines import extract
#   from scripts.pipelines.extract import run_extract
```

---

## Phase 3: Selective Cleanup (lower priority)

### 3.1 Domain-fit moves to `lib/`

| Current | Target | Lines | Reason |
|---------|--------|-------|--------|
| `scripts/observer.py` | `lib/observer.py` | 574 | Observer pattern — generic infrastructure, not a script |
| `scripts/worklog_generator.py` | Merge into `lib/worklog.py` | 404 | Duplicate concern with existing `lib/worklog.py` |
| `scripts/azure_spot.py` | `lib/infra/azure_spot.py` | 626 | Azure infrastructure — belongs in `lib/infra/` |

### 3.2 Proxy grouping → `scripts/proxies/`

| Current | Target | Lines |
|---------|--------|-------|
| `scripts/anthropic_proxy.py` | `scripts/proxies/anthropic.py` | 781 (after Phase 1) |
| `scripts/gemini_proxy.py` | `scripts/proxies/gemini.py` | 459 |
| `scripts/search_proxy.py` | `scripts/proxies/search.py` | 451 |

Systemd service path updates:
```
anthropic-proxy.service:  ExecStart=.../scripts/proxies/anthropic.py
gemini-proxy.service:     ExecStart=.../scripts/proxies/gemini.py
```

### 3.3 Test files → `tests/`

| Current | Target | Lines |
|---------|--------|-------|
| `scripts/test_27b_optimization.py` | `tests/test_27b_optimization.py` | 769 |
| `scripts/test_operator.py` | `tests/test_operator.py` | 440 |
| `scripts/verify_refactoring.py` | `tests/verify_refactoring.py` | 416 |

Update: `pytest` config (`pyproject.toml` or `pytest.ini`) `testpaths = ["tests"]`.

### 3.4 blob_explorer.py split (408 lines)

Mixed CLI + Azure API. Split:
- CLI → stays in `scripts/blob_explorer.py`
- API calls → `lib/infra/blob_api.py`
- `blob_uploader.py` already in `lib/` — these two can share `lib/infra/blob_api.py`.

### 3.5 local_debate.py (699 lines) — No split

Only 2 functions (`run_debate_plan` at 286 lines, `run_local_multi` at 83 lines). These are genuinely large functions, not import-organization problems. Splitting would fragment related logic. Leave as-is.

### 3.6 No action needed

| File | Lines | Func/class | Why skip |
|------|-------|------------|----------|
| `lib/feedback.py` | 554 | 11 | Single responsibility (feedback processing), cleanly bounded |
| `lib/infra/subprocess.py` | ~200 | 5 | Already clean, under 400 |
| `lib/llm/client.py` | ~200 | 4 | Already clean, under 400 |

---

## Execution Summary

| Phase | Edit | Create | Move | Delete | Risk |
|-------|------|--------|------|--------|------|
| 1 (split) | 3 + ~3 callers | 5 (`pod_manager.py`, `token_budget.py`, `proxy_utils.py`, `cli_worklog.py`, `cli_experiment.py`) | 0 | 0 | Low |
| 2 (pipelines/) | ~5 imports + 1 shell + 2 systemd | 1 (`__init__.py`) | 8 | 0 | Low |
| 3 (misc) | ~5 imports + 3 systemd paths | 2 (`blob_api.py`, merged `worklog.py`) | 11 | 4 (merged/duplicates) | Medium |
| **Total** | **~22** | **8** | **19** | **4** | |

## File Count Impact

| State | File count |
|-------|-----------|
| Current (flat scripts/) | 137 scripts/*.py + 58 lib/**/*.py = 195 total |
| After Phase 1 | 134 scripts/*.py + 63 lib/**/*.py = 197 total (+2) |
| After Phase 2 | 126 scripts/*.py + 63 lib/**/*.py + 9 pipelines/__init__.py = 198 total (+1) |
| After Phase 3 | 111 scripts/*.py + 67 lib/**/*.py + 9 pipelines/*.py + 3 tests/*.py = 190 total (-8 net) |

Directory structure after Phase 3:
```
scripts/
├── cli.py
├── blob_explorer.py
├── local_debate.py
├── prj_cycle.py
├── orchestrator.py
├── feedback.py
├── ... remaining scripts ...
├── pipelines/                   ← NEW
│   ├── __init__.py
│   ├── extract.py
│   ├── classify.py
│   ├── review.py
│   ├── hybrid.py
│   ├── code_mod.py
│   ├── rubric.py
│   ├── runner.py
│   └── night.py
├── proxies/                     ← NEW
│   ├── anthropic.py
│   ├── gemini.py
│   └── search.py
└── lib/
    ├── cli_worklog.py           ← NEW
    ├── cli_experiment.py        ← NEW
    ├── observer.py              ← moved from scripts/
    ├── proxy_utils.py           ← NEW
    ├── worklog.py               ← merged with worklog_generator.py
    ├── infra/
    │   ├── pod_manager.py       ← NEW
    │   ├── azure_spot.py        ← moved from scripts/
    │   ├── blob_api.py          ← NEW
    │   ├── containers.py
    │   ├── health_checks.py
    │   ├── storage.py
    │   └── subprocess.py
    ├── llm/
    │   ├── token_budget.py      ← NEW
    │   ├── client.py
    │   ├── json_parser.py
    │   └── rate_estimator.py
    ├── ... (existing domains unchanged)
    └── tracking/
tests/                           ← NEW
    ├── test_27b_optimization.py
    ├── test_operator.py
    └── verify_refactoring.py
```

## Prerequisites

- [ ] 5-phase experiment completed, results stable
- [ ] Current file structure frozen (no active pipeline changes)
- [ ] All existing tests passing before any move
- [ ] `git status` clean before starting each phase
- [ ] Phase 1 first → smoke test → Phase 2 → smoke test → Phase 3

## Rollback

Each phase independently reversible via `git checkout` of moved files. No DB migrations needed — pure file reorganization. Shell scripts and systemd units tracked in same repository.

---

*Generated: 2026-06-06 | Language: English (machine-readable) | Status: refactoring completed by Junie on 2026-06-13*
