# Experiment Registry — Implementation Plan

## 1. Goal

Store LLM server tuning experiment results in PostgreSQL to:
- Let new session LLMs recognize previously attempted experiments
- Track config decision rationale
- Prevent experiment data from being ephemeral (volatile)

## 2. Constraints (Design Basis)

| Constraint | Reason |
|------------|--------|
| Single user, single server | No team sharing, no dashboard needed |
| ARM 4-core 22GB | No additional services (Metabase, Redash, etc.) |
| Measured metric = decode t/s | NOT NLP accuracy or prompt version management |
| Reuse existing psql() functions | No new dependencies |
| Experiment = llama-server config change | Git commits are immutable (config itself is the identifier) |

## 3. Implementation Steps

### Step 1: DB Tables — Complete

- `experiment_registry` — experiment records (config JSONB, results JSONB, verdict, rationale)
- `active_config` — currently applied settings (component PK)

### Step 2: bench_llm.py --register Flag [Implemented]

bench_llm.py accepts `--register` option for automatic DB INSERT:
```bash
python3 bench_llm.py --runs 3 --label "my-test" --register --verdict rejected --rationale "reason"
```

Behavior:
1. Run benchmark (as usual)
2. Aggregate results, INSERT into `experiment_registry`
3. If `--verdict` not specified, store as `'pending'`

Required args:
- `--experiment-id` (auto-generated from label if omitted)
- `--category` (default: 'llm-optimization')
- `--subcategory` (default: 'runtime-test')
- `--verdict` (choice: optimal/accepted/rejected/baseline/pending)
- `--rationale` (optional, defaults to '')

### Step 3: CLI Commands [Implemented]

`cli.py` experiment subcommands:

```bash
python3 scripts/cli.py experiment list          # last 20 experiments
python3 scripts/cli.py experiment list --all    # all experiments
python3 scripts/cli.py experiment list --category concurrent
python3 scripts/cli.py experiment compare id1 id2 id3  # config/results diff
python3 scripts/cli.py experiment active        # current active config
```

Output format: terminal table (tab-aligned) + YAML (detail)

### Step 4: Session Start Auto-Lookup [Implemented in CLAUDE.md]

On session start, secure context via these SQL queries:

```sql
-- (A) current active config
SELECT component, config, rationale FROM active_config;

-- (B) recent experiment history (verdict=optimal/accepted/baseline)
SELECT experiment_id, category, verdict, substring(rationale, 1, 120)
FROM experiment_registry
ORDER BY created_at DESC LIMIT 15;

-- (C) rejected experiments in same category (what failed)
SELECT experiment_id, subcategory, results->>'pod_b_decode_tps' AS tps
FROM experiment_registry
WHERE verdict = 'rejected' AND category = 'llm-optimization'
ORDER BY created_at DESC;
```

→ Documented in `CLAUDE.md` and `llm-common-rule.md`: "check experiment_registry before testing"

### Step 5: active_config Update Workflow [Operational Rule]

When changing config:
1. Edit config files (container, entrypoint)
2. Restart Pod
3. Run benchmark
4. Save result via `bench_llm.py --register`
5. Adopt via `python3 scripts/cli.py experiment adopt <experiment-id> --component pod-a-day`
   → UPDATE `active_config`

## 4. File Change List

| File | Change |
|------|--------|
| `scripts/bench_llm.py` | `--register`, `--experiment-id`, `--verdict`, `--rationale` args + DB INSERT function |
| `scripts/cli.py` | `experiment list`, `experiment compare`, `experiment active`, `experiment adopt` subcommands |
| `llm-agent-rule.md` or `CLAUDE.md` | Session start experiment_registry lookup |
| `docs/plan-experiment-registry.md` | This document (complete) |

## 5. Out of Scope

- Metabase / Redash / Grafana dashboards
- GitHub Actions CI/CD integration
- Automatic git commit recording (config itself is the identifier)
- Prompt version / NLP accuracy management
- External libraries (psycopg2, etc.) — keep using existing `podman exec postgres psql` family
- Reproduction scripts (same config rerun on same server is trivially reproducible)

## 6. Steps 2-5 Priority Order

1. **Step 2 (bench_llm.py --register)** — most urgent, used from next experiment onward
2. **Step 3 (CLI commands)** — query convenience
3. **Step 4 (session start auto-lookup)** — agent-rule registration
4. **Step 5 (adopt workflow)** — as needed

Each Step is independent; stopping mid-way preserves backward compatibility.
