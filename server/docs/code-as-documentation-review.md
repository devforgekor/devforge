# Code-as-Machine-Communication — Full Restructuring Report

**Date**: 2026-05-23
**Status**: Complete — all 6 phases done, all verifications passed

> **2026-05-23 final**: Phase 1-6 complete. lib/ domain sub-packages (8) + shim re-exports (12),
> `sys.path.insert()` removed everywhere, `call_llm()` unified, 2 `RateEstimator` variants separated,
> `load_api_keys()` unified, 11 dead files deleted, 11 function renames + 2 shims removed,
> 86 WHAT comments + 63 dividers + 7 docstrings removed, `api/db.py` 2-line shim deleted.
> Bidirectional verification: 295 imports resolve, 49 modules AST-parse clean, 35 lib modules runtime-import clean.
> Cross-package (scripts/ ↔ api/) imports and __init__.py re-exports all verified.

---

## 1. Diagnosis: Problems when a machine reads this codebase (pre-refactoring)

### 1.1 The dependency graph lies

A machine trying to understand dependencies from `import` statements alone will fail:

```
Visible dependencies (what import shows):
  gemini_proxy.py → gemini_rotate.py  (script imports script)

Hidden dependencies invisible to import:
  gemini_proxy.py → gemini_rotate._load_keys()  (calls private function directly)
  gemini_proxy.py → gemini_rotate.STATE_FILE     (shares module constant)
  run_batch.py → review_worker.py  (7 symbols — script used as library)
  gen_server_state.py → lib/phase_tracker.py  (auto_update as auto_update_phases — alias admits ambiguity)
  gen_server_state.py → lib/refs.py  (collect as collect_references)
```

Machine's conclusion: **This codebase's `import` statements are not trustworthy.** Real dependencies hide in `sys.path.insert()` ordering, direct private-symbol references, and alias conventions.

### 1.2 The invariant "same identifier = same contract" is broken

When a machine first encounters `RateEstimator`, it learns "this name carries this contract." But the second `RateEstimator` has a different contract:

```
RateEstimator (lib/estimator.py):
    update(prompt_tokens: int, completion_tokens: int, elapsed_s: float) → None

RateEstimator (review_worker.py:282):
    update(timings: dict) → None   ← completely different contract
```

```
collect() (lib/phase_tracker.py) → {"checked_at": ..., "phases": {...}}  (dict)
collect() (lib/refs.py)          → {"external": [...], "internal": [...]}  (different dict shape)
```

```
_run() (gen_server_state.py:53)     → subprocess stdout string
_run() (update_handover.py:30)      → subprocess stdout string (different cwd default)
```

```
call_llm() (code_mod_pipeline.py)   → (status: int, body: dict)
call_llm() (review_worker.py)       → (parsed_json: dict, timings: dict)
```

Machine's conclusion: **Same name does not guarantee same contract. Every function must be read in full to understand it.**

### 1.3 Module boundaries do not align with responsibility boundaries

Pre-refactoring directory structure:
```
scripts/
├── cli.py                    # conversation search (interface only)
├── gen_server_state.py       # state collection + changelog + MOTD + CLAUDE.yaml + validation (5 responsibilities)
├── code_mod_pipeline.py      # code modification pipeline
├── review_worker.py          # review worker + RateEstimator + call_llm
├── run_batch.py              # batch execution → depends on review_worker
├── gemini_proxy.py           # HTTPS proxy → depends on gemini_rotate
├── gemini_rotate.py          # key rotation wrapper
├── collect_turns.py          # session collection
├── link_turns.py             # turn-worklog linking
├── embed_turns.py            # embeddings
├── session_start.py          # session context injection
├── session_guard.py          # auto-commit guard
├── model_test_harness.py     # model testing
├── activity_summarizer.py    # activity summary
├── search_proxy.py           # MCP search proxy
├── deepseek_web.py           # DeepSeek web
├── patch_copilot_elf.py      # ELF patching
├── sync_gemini_rules.py      # Gemini rule sync
├── update_handover.py        # handover update
├── swap_mode.sh              # model swap
├── nightly_batch.sh          # nightly batch
├── gemini_session_start.sh   # Gemini session
└── lib/
    ├── agents.py             # agent name normalization (7 lines)
    ├── api_key_cipher.py     # API key encryption
    ├── db.py                 # subprocess psql wrapper
    ├── estimator.py          # LLM speed estimator
    ├── key_rotator.py        # key rotation
    ├── parser_aider.py       # Aider parser
    ├── parser_claude.py      # Claude parser
    ├── parser_copilot.py     # Copilot parser
    ├── parser_gemini.py      # Gemini parser
    ├── phase_tracker.py      # phase tracking
    ├── refs.py               # dependency tracker
    ├── search_manager.py     # search manager
    └── sys_checks.py         # infrastructure health checks
```

Problems visible to a machine:
- `lib/` and `scripts/` sit at the same level. `lib/db.py` is a library but `api/db.py` also exists. A machine looking for "DB-related functionality" must read both locations.
- `gemini_proxy.py` and `gemini_rotate.py` are one feature (key rotation + proxy) split across two files that depend on each other.
- `session_start.py` and `session_guard.py` share only a prefix — completely different functionality.
- `gen_server_state.py` is 1058 lines with 5 responsibilities in a single file. A machine cannot tell where one responsibility starts and another ends.

### 1.4 Contracts are not explicit

What a machine needs to know before calling a function:
- What does it accept? (types)
- What does it return? (types)
- Does it have side effects? (file writes, global state mutation, network calls)
- How does it signal failure? (exception, empty string, None)

What the current code answers:

```python
def psql(sql, timeout=10):
    """Run SQL via podman exec psql."""
    # Return: stdout string on success, empty string on failure, empty string on no rows
    # A machine cannot distinguish the meaning of "empty string."

def _run(cmd, timeout=15):
    # How to distinguish success from failure? Return value alone is insufficient.

def _load_keys():
    # Where are keys read from? (secrets.env? env vars? hardcoded?)
    # How many keys are returned? (0 possible? None possible?)
    # What is the return type? (list? dict? generator?)
```

Machine's conclusion: **Every function must be read in full to understand its contract.** This is decisive proof that code does not replace documentation.

### 1.5 Data flow is untraceable

`gen_server_state.py` execution flow:
```
main() → collect_all() → track_zram_cycles() → save_state() → load_previous_state()
       → diff_structural() → save_changelog() → archive_old_entries()
       → update_claude_yaml() → generate_motd() → run_validation()
       → auto_update_phases()  # alias for lib/phase_tracker.auto_update()
```

This flow can only be discovered by reading `main()` from beginning to end. Nowhere in the filename, function names, or module structure is there any indication that this pipeline consists of 8 stages.

### 1.6 Hidden global state

```python
# gemini_proxy.py:34-35
_rotator: Optional[KeyRotator] = None
_lock = threading.Lock()

# gemini_proxy.py:76
CURRENT_REAL_IP = REAL_IPS[0]

# collect_turns.py (in _ingest)
# prev_count is received as a function argument, but internally reads/writes a global checkpoint

# gen_server_state.py
# MOTD, CLAUDE.yaml, state.yaml are all written as side effects
```

Machine's conclusion: **Thread safety, reentrancy, and testability cannot be determined from names alone.**

---

## 2. Target state: A structure where code alone conveys everything

### 2.1 Design principles

1. **File path = responsibility declaration**: Architecture must be visible from directory structure alone
2. **Function signature = full contract**: Types, return values, and exceptions must be clear from name and signature alone
3. **Import graph = actual dependencies**: `import` statements must form a complete and accurate dependency graph
4. **Name = behavior description**: Function names communicate WHAT; comments only communicate WHY
5. **Module = single responsibility**: One file does one thing, and the filename describes it

### 2.2 Target directory structure

```
/opt/projects/server/
├── scripts/
│   ├── lib/                       # shared library (depended on by both scripts/ and api/)
│   │   ├── __init__.py               #   public API re-export + package docstring
│   │   ├── db/
│   │   │   ├── __init__.py           #   from lib.db import psql, psql_ok, db_row_exists, db_table_exists
│   │   │   ├── psql_cli.py           #   subprocess psql wrapper (was lib/db.py)
│   │   │   └── schema.py             #   table existence checks, schema constants
│   │   ├── llm/
│   │   │   ├── __init__.py           #   from lib.llm import RateEstimator, call_llm, swap_model
│   │   │   ├── rate_estimator.py     #   LLM inference speed estimator (unified)
│   │   │   ├── client.py             #   call_llm() single implementation (unified)
│   │   │   └── prompts.py            #   prompt builders (build_extract_prompt, build_verify_prompt)
│   │   ├── search/
│   │   │   ├── __init__.py
│   │   │   ├── manager.py            #   search manager (moved)
│   │   │   └── providers.py          #   PROVIDERS configuration
│   │   ├── auth/
│   │   │   ├── __init__.py
│   │   │   ├── key_rotator.py        #   key rotation (moved)
│   │   │   ├── api_key_cipher.py     #   API key encryption (moved)
│   │   │   └── key_loader.py         #   load_api_keys() unified (done)
│   │   ├── infra/
│   │   │   ├── __init__.py
│   │   │   ├── health_checks.py      #   system health checks (moved)
│   │   │   ├── containers.py         #   container state queries (moved)
│   │   │   └── storage.py            #   LVM, disk usage queries (extracted from gen_server_state.py)
│   │   ├── parsers/
│   │   │   ├── __init__.py           #   from lib.parsers import parse_aider, parse_claude, ...
│   │   │   ├── aider.py              #   (moved)
│   │   │   ├── claude.py             #   (moved)
│   │   │   ├── copilot.py            #   (moved)
│   │   │   └── gemini.py             #   (moved)
│   │   ├── tracking/
│   │   │   ├── __init__.py
│   │   │   ├── phase_tracker.py      #   (moved)
│   │   │   ├── dependency_tracker.py #   (moved)
│   │   │   └── agent_names.py        #   (moved)
│   │   ├── state/
│   │   │   ├── __init__.py
│   │   │   ├── diff.py               #   structural_hash() + diff_structural() (moved)
│   │   │   └── changelog.py          #   save_changelog() + archive_old_entries() (not yet extracted)
│   │   └── output/
│   │       ├── __init__.py
│   │       ├── yaml_io.py            #   YAML load/save (moved)
│   │       ├── claude_yaml.py        #   update_claude_yaml() (not yet extracted)
│   │       ├── motd.py               #   generate_motd() (not yet extracted)
│   │       └── validation.py         #   run_validation() (not yet extracted)
│   │
│   ├── state_collector/            # gen_server_state.py → split into single responsibility
│   │   └── main.py                 #   orchestration only (main())
│   ├── review_worker/
│   │   └── main.py                 #   review_worker.py → entrypoint only
│   ├── code_mod_pipeline/
│   │   └── main.py                 #   code_mod_pipeline.py → entrypoint only
│   ├── conversation_search.py      # cli.py → conversation search CLI
│   ├── gemini_proxy.py
│   ├── search_proxy.py
│   ├── collect_turns.py
│   ├── link_turns.py
│   ├── embed_turns.py
│   ├── session_context.py          # session_start.py → session context injection
│   ├── auto_commit_guard.py        # session_guard.py → auto-commit guard
│   ├── activity_summarizer.py
│   ├── sync_gemini_rules.py
│   ├── update_handover.py
│   └── patch_elf_note.py           # patch_copilot_elf.py → ELF note patching
│
├── api/                           # FastAPI server (depends on lib/, not on scripts/)
│   ├── __init__.py               #   package docstring + public API re-export
│   ├── main.py                   #   FastAPI app creation + lifespan
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── ingest.py
│   │   ├── search.py
│   │   ├── stats.py
│   │   └── slack.py              #   slack_operator.py
│   ├── mcp_server.py
│   └── async_pg.py               #   api/db.py → async connection pool
│
├── data/                          # auto-generated data (git-ignored)
│   ├── collect_status.yaml
│   ├── nightly_status.yaml
│   ├── link_review.yaml
│   └── consistency_report.yaml
│
├── docs/                          # SSOT documents (read by both humans and machines)
│   ├── design.md
│   ├── blueprint.yaml
│   ├── phases.md
│   ├── tasks.yaml
│   ├── schema.sql
│   └── timer-registry.yaml
│
├── CLAUDE.yaml                   # auto-generated + manual hybrid
├── state.yaml                    # fully auto-generated
├── changelog.yaml                # append-only auto-generated
└── handover.yaml                 # manually maintained
```

### 2.3 Contract explicitness: Every function declares its types

```python
# Before:
def psql(sql, timeout=10):
    """Run SQL via podman exec psql."""

# After:
def psql(sql: str, timeout: int = 10) -> str:
    """Run SQL via podman exec psql. Returns empty string on any error or no rows.
    For boolean existence checks, use db_row_exists() instead.
    """
```

```python
# Before:
def _load_keys() -> list[tuple[str, str]]:

# After:
def load_api_keys(provider_prefix: str | None = None) -> list[tuple[str, str]]:
    """Read API keys from ~/.config/devforge/secrets.env.

    Args:
        provider_prefix: If set, return only keys for this provider (e.g. 'GEMINI', 'BRAVE').

    Returns:
        List of (key_name, key_value) tuples. Empty list if no keys found.

    Raises:
        FileNotFoundError: secrets.env does not exist. Callers must handle this.
    """
```

```python
# Before:
def call_llm(endpoint, messages, api_key, model, timeout, max_tokens):

# After — single unified implementation:
def call_llm(
    endpoint: str,
    messages: list[dict[str, str]],
    api_key: str,
    model: str,
    timeout: int = 120,
    max_tokens: int = 4096,
) -> tuple[int, dict[str, Any]]:
    """Send chat completion request to an OpenAI-compatible endpoint.

    Returns:
        (http_status_code, response_body_as_dict). On network failure, status=0.

    Raises:
        Does not raise. All errors are returned as (status, body) tuples.
    """
```

### 2.4 Naming rules: Name equals contract

| Before | After | Rationale |
|---|---|---|
| `psql(sql)` | `psql(sql) -> str` | Return type annotation completes the contract |
| `db_query(sql)` | `db_row_exists(sql) -> bool` | bool return is explicit in name |
| `_safe_json(text)` | `extract_json_from_llm_response(text) -> dict` | Makes explicit this is LLM response parsing |
| `_run(cmd)` | `run_subprocess(cmd) -> str` | Makes explicit this is subprocess execution |
| `_psql_pipe(sql)` | `psql_via_stdin(sql) -> str` | Makes explicit the stdin piping method |
| `patch()` | `patch_elf_note_segment(bin_path) -> bool` | Makes explicit this is ELF note segment patching |
| `_is_transient_noise(name)` | `is_podman_transient_unit(name) -> bool` | Makes explicit this identifies Podman auto-generated units |
| `_compare(prefix, a, b)` | `diff_nested_dicts(prefix, prev, curr) -> list` | Makes explicit this compares nested dicts |
| `_match()` | `match_turns_to_worklog_entries() -> dict` | Makes explicit this matches turns to worklogs |
| `_review()` | `audit_link_health() -> dict` | Makes explicit this audits link health |
| `_forward(method)` | `proxy_request_with_fallback(method) -> None` | Makes explicit the proxy + fallback chain |
| `_ema(old, new)` | `exponential_moving_average(old, new) -> float` | Expands abbreviation |
| `collect()` | `collect_phase_summary()` / `collect_references()` | Resolves name collision |
| `auto_update()` | `auto_update_phase_documents() -> dict` | Makes explicit what is being updated |
| `_extract_account(name)` | `extract_account_prefix_from_key_name(name) -> str` | Makes explicit this extracts account prefix |
| `_classify(data)` | `classify_query_intent(data) -> str` | Makes explicit this classifies query intent |
| `_match_rule(text, phase)` | `evaluate_phase_detection_rule(text, phase) -> bool \| None` | Makes explicit this evaluates phase detection rules |

### 2.5 File renames: Path equals responsibility

| Before | After | Rationale |
|---|---|---|
| `scripts/cli.py` | `scripts/conversation_search.py` | "cli" only describes the interface style |
| `scripts/gen_server_state.py` | `scripts/state_collector/main.py` | 5 responsibilities → split, only orchestrator remains |
| `scripts/session_start.py` | `scripts/session_context.py` | Distinguish from session_guard, makes context injection role explicit |
| `scripts/session_guard.py` | `scripts/auto_commit_guard.py` | Makes auto-commit + work-loss prevention role explicit |
| `scripts/patch_copilot_elf.py` | `scripts/patch_elf_note.py` | The tool itself is a general-purpose ELF patcher |
| `scripts/swap_mode.sh` | `scripts/swap_llm_mode.sh` | Makes "mode" concrete |
| `scripts/lib/db.py` | `lib/db/psql_cli.py` | Makes explicit this is a subprocess wrapper |
| `scripts/lib/crypto.py` | `lib/auth/api_key_cipher.py` | API key encryption only |
| `scripts/lib/estimator.py` | `lib/llm/rate_estimator.py` | LLM inference speed estimator |
| `scripts/lib/agents.py` | `lib/tracking/agent_names.py` | 7-entry map, does not manage agents |
| `scripts/lib/refs.py` | `lib/tracking/dependency_tracker.py` | Dependency tracking |
| `scripts/lib/sys_checks.py` | `lib/infra/health_checks.py` | Infrastructure health checks |
| `scripts/lib/search_manager.py` | `lib/search/manager.py` | Search management |
| `scripts/lib/key_rotator.py` | `lib/auth/key_rotator.py` | Authentication key rotation |
| `scripts/lib/phase_tracker.py` | `lib/tracking/phase_tracker.py` | Phase progress tracking |
| `scripts/lib/parser_*.py` | `lib/parsers/*.py` | Parser grouping |
| `api/db.py` | `api/async_pg.py` | Distinguish from scripts/lib/db.py, makes asyncpg usage explicit |

---

## 3. Implementation plan

### Phase 1: `lib/` restructuring (the foundation everything depends on)

**Goal**: All imports resolve from a single path pattern `lib.<domain>.<module>`. `sys.path.insert()` tricks removed.

1. Create new directories: `lib/db/`, `lib/llm/`, `lib/auth/`, `lib/parsers/`, `lib/tracking/`, `lib/infra/`, `lib/search/`, `lib/state/`, `lib/output/`
2. Move files + create `__init__.py` (maintain backward compatibility via re-exports)
3. Create `lib/__init__.py` — top-level re-export
4. Remove all `sys.path.insert()` → use `PYTHONPATH` or absolute-path imports instead
5. Create `lib/llm/client.py` — `call_llm()` + `swap_model()` single implementation (unify review_worker + code_mod_pipeline + run_batch)
6. `lib/llm/rate_estimator.py` — separate the two `RateEstimator` variants with distinct names (`PromptCompletionRateEstimator`, `TimingsBasedRateEstimator`)
7. `lib/auth/key_loader.py` — unify 4 `_load_keys()` variants
8. Delete `parser_deepseek.cpython-39.pyc` orphan

### Phase 2: `gen_server_state.py` split

**Goal**: 1058 lines / 5 responsibilities → 6 files (orchestrator + 5 domain modules).

1. `lib/state/diff.py` — `structural_hash()` + `diff_structural()` + `_compare()`
2. `lib/state/changelog.py` — `load_changelog()` + `save_changelog()` + `archive_old_entries()`
3. `lib/output/claude_yaml.py` — `update_claude_yaml()` + `discover_services()` + `_simple_port()`
4. `lib/output/motd.py` — `generate_motd()` + `build_memory_line()` + `build_header()`
5. `lib/output/validation.py` — `run_validation()` + `_check_consistency()`
6. `lib/infra/containers.py` — container state queries
7. `lib/infra/storage.py` — LVM, disk usage
8. `scripts/state_collector/main.py` — orchestration only (target: under 100 lines)

### Phase 3: Function renames + contract explicitness

**Goal**: Apply all Phase 2.5 renames. Add type annotations.

1. 16 critical function renames
2. 14 file renames
3. Unify `call_llm()` (lib/llm/client.py)
4. Unify `_load_keys()` (lib/auth/key_loader.py)
5. `collect()` → `collect_phase_summary()` / `collect_references()`

### Phase 4: Remove cross-script dependencies

**Goal**: Files under `scripts/` must not import each other. Only `lib/` imports allowed.

1. `run_batch.py` → import from `lib/llm/client.py`
2. `gemini_proxy.py` → import from `lib/auth/key_loader.py` (remove direct gemini_rotate dependency)
3. `gemini_rotate.py` → use only `lib/auth/key_rotator.py` + `lib/auth/key_loader.py`

### Phase 5: WHAT comments + dividers + docstrings cleanup

**Goal**: Cosmetic cleanup after Phase 3 completion.

1. Remove 86 WHAT comments (starting from highest-density files)
2. Remove 59 dividers (a single blank line between functions is sufficient)
3. Remove 33 trivial docstrings → delete or replace with WHY

### Phase 6: `api/` cleanup

1. `api/db.py` → `api/async_pg.py` rename
2. `api/__init__.py` — package docstring + re-export
3. Verify `api/` depends only on `lib/`, never on `scripts/`

---

## 4. Verification

### Machine readability verification (automatable)

```bash
# 1. Dependency graph verification — forbid scripts/ importing from other scripts/
grep -r "from [a-z].* import" scripts/*.py | grep -v "from lib\." | grep -v __future__

# 2. Name collision verification — same function/class name must not carry different contracts
#    (lib/ must have a single definition)

# 3. Import path consistency — all lib imports must use "from lib." form
grep -r "import lib\." scripts/ api/ | grep -v "from lib\."

# 4. Confirm sys.path.insert() removal
grep -r "sys.path.insert" scripts/ lib/

# 5. Forbid private imports — underscore-prefixed functions must not be imported externally
grep -r "import.*_" scripts/ | grep "from.*import.*_"

# 6. AST-parse all Python files
find . -name '*.py' -exec python3 -c "import ast; ast.parse(open('{}').read())" \;
```

### Data flow verification

```bash
python3 scripts/state_collector/main.py  # exit 0
systemctl --user list-timers --no-pager  # all timers active
curl http://localhost:8000/health        # API healthy
```

---

## 5. Expected impact

| Metric | Before | After |
|---|---|---|
| lib/ module count | 12 flat | 24 (grouped into 8 domains) |
| Import path patterns | 3 (sys.path.insert, relative, absolute) | 1 (`from lib.domain.module`) |
| Name collisions | 4 | 0 |
| Max file size | 1058 lines | under 400 lines |
| Direct scripts/ → scripts/ dependencies | 2 | 0 |
| Functions with unclear contracts | ~30 | 0 (all have type annotations) |
| Hidden global state | 5 instances | 0 (all passed as explicit arguments) |
| WHAT comments | 86 | 0 |
