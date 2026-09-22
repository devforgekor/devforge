# DevForge — Agent Rules (AGENTS.md)

> Canonical, hand-written agent rules. **This is the only rules file to edit.**
> `CLAUDE.md` imports it (`@AGENTS.md`) — keep only Claude-specific routing there, never rules.
> OpenCode / Copilot read this file natively; `.github/copilot-instructions.md` is intentionally
> absent (it would outrank and shadow this file for Copilot). Details live in `agent_docs/` (§11).

## 0. Guardrails (highest priority)
- User request / issue / project policy is the final truth.
- Tests are evidence, not intent. On conflict, ASK — never guess.
- Never guess under uncertainty; ask instead.
- Never auto-commit / push / create PRs / switch branches unless explicitly asked.
- Never weaken, delete, or skip tests (`skip` / `only`) to make them pass.
- Never print, log, or commit secrets. Use `~/.config/devforge/secrets.env`; mask all output.
- Get explicit approval before large refactors, public API changes, data migrations.

## 1. Commands (verified — run from `/opt/projects/server`)
- Test:         `pytest -x --tb=short`
- Lint:         `ruff check src/devforge`
- Types:        `mypy src/devforge`
- Architecture: `lint-imports`   (4 contracts must stay KEPT)
- CLI:          `python3 scripts/cli.py <status --json | task | research | glossary>`
- DB:           `podman exec postgres psql -U devforge -d devforge_app`
- Container:    `podman` (rootless, user `opc`)

## 2. Boundaries
- ALWAYS: run test + lint + types before declaring done; add/update tests with behavior.
- ASK FIRST: new files; new dependencies; editing `docs/*.md`; schema / migrations; cross-domain edits.
- NEVER: commit secrets; auto-commit / push; duplicate rules into copies (CLAUDE.md stays
  `@AGENTS.md` import only); weaken tests; `psql -h localhost` (postgres has no published
  host port); inline secrets in containers; start service containers outside Quadlet
  (`~/.config/containers/systemd/`).

## 3. Communication
- User-facing responses: Korean. Machine-readable (identifiers, logs, prompts, commits, DB): English.
- All LLM prompt strings: English (prompts are machine instructions).
- Internals (logs, timers): UTC. User-facing times: KST (UTC+9).
- Docs: human-facing Korean (≤400 lines); machine-readable English.
- Be concise. Never echo secrets.
- **Code is SSOT**: when code and docs disagree, the code is correct — update the doc.

## 4. Context priority
- Reading: tests → type hints / structure → git log / blame → comments (WHY / WARNING only).
- Writing: user requirements → tests → type hints / structure → git history → comments.

## 5. Code
- Explicit > clever. Pure functions; isolate I/O, network, DB, time, random at boundaries.
- Errors explicit (types / result objects). No bare `except`. Retry with Tenacity or a loop.
- Python: never `utcnow()`; stdlib before third-party; `pytest -x --tb=short`.
- Red → Green. Refactor only when asked.
- Search before adding; ≥70% overlap → extend the existing file.
- New file only with approval; state why existing files are insufficient.
- Dead code is worse than none. Keep the file count low. Surgical changes; preserve behavior.
- Respect bounded contexts; do not modify files outside the assigned domain without asking.
- CAUTION: over-engineering → warn; proceed only if repeated.

## 6. Comments
- Comments explain WHY, never WHAT / WHEN.
- Allowed: `[WHY]` business rule · `[WARNING]` do-not-touch · `[WORKAROUND]` external bug.
- Forbidden: `# add A and B`, `# fixed bug 2024-09`, `# TODO` (use the issue tracker).
- No divider comments, no trivial docstrings. Function name = what it does.
- Public API docstrings stay. Complex algorithms / regex / security may be documented.

## 7. File header (every `.py`)
```python
#!/usr/bin/env python3
# Status: production|experimental|deprecated
# Path: <callers — or "none — reason">
"""<one-line summary>"""
```
- `Status` is SSOT for risk triage: production → P0 fix · experimental → warn · deprecated → do not modify.
- Missing header → treat as production. `code-structure.yaml` is advisory only.

## 8. Tests
- Descriptive names: `test_should_<behavior>_when_<condition>`.
- Edge cases: 0, null, negative, empty, boundaries, permissions, concurrency.
- Behavior change → add/update tests. Legacy refactor → characterization test first.
- Run the suite; report regressions. Never claim done without evidence.

## 9. Git
- Conventional Commits: `feat|fix|refactor|test|docs|chore(scope): ...`.
- Small, logical commits; never mix behavior change with formatting / refactor.
- PR only on request: Context / Changes / Verification / Risks.

## 10. Workflow
- **Evaluation-first**: define pass/fail criteria before implementing. For external references,
  research → draft criteria → confirm with the user. Verify against criteria before claiming done.
- Trivial (typo, comment, log/const, tests-only, docs-only): edit, run tests, done.
- Non-trivial: requirements → read tests → git context → narrow scope → plan / ask →
  implement → tests → lint/types/test → report (commit / PR only if asked).
- Code edits: track via CLI `task` (§1); LSP `blast_radius` before editing, `get_diagnostics` after.
- Deep Dive (multi-file / architecture / new files): see `agent_docs/deep-dive.md`.

## 11. Pointers (do not duplicate content)
- Live server state:  `python3 /opt/projects/server/scripts/cli.py status --json` (overrides any doc)
- Server identity:    `/opt/projects/server/docs/architecture/infrastructure.md`
- Session start/end:  `agent_docs/session.md`
- Testing (Pod B):    `agent_docs/testing.md`
- MCP tools/routing:  `agent_docs/mcp.md` (Claude `~/.claude/mcp.json` · OpenCode `~/.config/opencode/opencode.json`)
- Domain glossary:    `/opt/projects/server/docs/domain-glossary.yaml` (edit YAML, then `python3 /opt/projects/server/scripts/cli.py glossary sync`)
- Code layout SSOT:   `/opt/projects/server/docs/architecture/code-structure.yaml`
- Server paths are absolute; `agent_docs/` sits next to this file.

## 12. Philosophy
> Intent → issues / requirements. Verification → tests. History → git/PR. Warnings → comments.
