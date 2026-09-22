# DevForge — Common Rules (All AI Agents)

## Evaluation-First
- Before ANY implementation: define pass/fail criteria first.
- Claude Code drafts criteria reflecting server state → user reviews/researches → criteria finalized.
- After criteria is frozen: delegate to implementation (local LLM or direct edit).
- Verification against criteria is required before claiming task done.
- When in doubt about "what good looks like": WebSearch/WebFetch first, draft second, ask third.
- **Experiment check before testing**: Before any new experiment, check past results in `experiment_registry` and current config in `active_config`:
  ```sql
  -- recent experiments
  SELECT experiment_id, category, verdict, substring(rationale,1,80) 
  FROM experiment_registry ORDER BY created_at DESC LIMIT 15;
  -- active config  
  SELECT component, config, rationale FROM active_config;
  ```
  Run via: `podman exec postgres psql -U devforge -d devforge_app -c "query"`

## Communication
- Claude MUST answer in Korean. All conversational responses, explanations, and progress updates MUST be Korean. English is only allowed inside code blocks.
- Keep responses concise. When asked for short answers, respond in ≤3 sentences.
- Machine-readable output (YAML keys, identifiers, logs, prompts, commits, DB) MUST be English.
- User-facing text (explanations, CLI, Slack, MOTD, status) MUST be Korean.
- ALL LLM prompt text MUST be English. Prompts are machine instructions — Korean is forbidden in any prompt string.
- Technical terms (file paths, function names, error codes, API names) remain English. Emoji forbidden in Korean text.
- Never print, log, or echo secrets (API keys, tokens, passwords, connection strings). Use env vars or secret files instead. Redact secrets from all output.
- System internals (logs, timestamps, systemd timers): UTC. User-facing output: KST (UTC+9).
- Machine-readable docs (plans, architecture, DDL, specs): English. Emoji allowed for diagrams/structure.
- Human-facing docs: Korean. ≤400 lines per document. Split if exceeds.
- **Code is SSOT**: When code and document disagree, the code is correct. Update the document.

## Code Generation
- Python: NEVER use `utcnow()`. NEVER use bare `except:`. Retry with Tenacity or manual retry loop. Run tests with `pytest -x --tb=short`. Prefer stdlib over third-party packages.
- Write failing test first (Red). Get approval. Implement minimal fix (Green). Refactor only when asked.
- Respect bounded contexts. Do not modify files outside the assigned domain without asking.
- Search existing code before adding anything. If ≥70% overlap, extend existing file.
- New file only with user approval. State why existing files are insufficient.
- Dead code is worse than no code. Keep the file count low.
- Prefer editing existing files over creating new ones. Preserve existing behavior. Keep changes surgical.
- No import-only files: If a definition is missing, provide a stub. (TYPE_CHECKING blocks are exempt.)
- Token efficiency: Use grep/glob search tools instead of reading whole files. Use Edit instead of Write for existing files.
- Share significant docs via Azure Blob (`stshareddevforgeprodkrc`, container `devforge`), generate SAS URL.
- CAUTION: Over-engineering → warn. Proceed if repeated.

## Code as Documentation
- Code is the primary documentation. Function name = what it does. Comments = why, not what.
- No WHAT comments, no divider comments, no trivial docstrings.
- File path = responsibility declaration. One module = one job.
- Architecture must be visible from directory structure alone.
- Prefer runnable verification (`pytest`, `ast.parse`, `patch --dry-run`) over written specifications.

### File Header — Status + Path
Every `.py` file MUST include a machine-parseable header immediately after `#!/usr/bin/env python3`:

```python
#!/usr/bin/env python3
# Status: {production|experimental|deprecated}
# Path: <callers — comma-separated, or "none — <reason>">
"""<one-line summary>"""
```

**Status values**:
| Status | Meaning | Audit behavior |
|--------|---------|----------------|
| `production` | Called by timer/cron/systemd/nightly_batch | Risk → P0 fix |
| `experimental` | Prototype, parallel test, or not in execution path | Risk → warn only |
| `deprecated` | Pending removal, no new callers allowed | Do not modify |

**Path format**:
- Production: list all entry points (`nightly_batch.sh:255`, `systemd:devforge-swap.timer`)
- Experimental: state purpose + transition plan (`none — prototype of code_mod_pipeline`)
- Deprecated: state replacement (`migrated to code_mod_pipeline.py, remove after YYYY-MM-DD`)

**Why**: A single `grep "^# Status:" scripts/*.py` answers "is this production code?" without tracing call chains. Both human and machine can judge risk tier at a glance.
