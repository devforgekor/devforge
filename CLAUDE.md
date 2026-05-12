# Project Rules

## Server Knowledge System
Read `/opt/projects/server/CLAUDE.yaml` as the machine entry point. All server rules, state tracking, change logging, handover protocol, and blueprint are defined there. Log into SSH to see the live MOTD dashboard.

## [MUST]
- Language: User-facing output MUST be Korean. Code, variable names, log metadata, docstring summary lines, and ALL LLM prompt text MUST be English. Prompts are machine instructions — Korean is forbidden in any prompt string.
- Answer language: Claude MUST answer in Korean. All conversational responses, explanations, and progress updates MUST be Korean. English is only allowed inside code blocks.
- Python: NEVER use utcnow(). NEVER use bare `except:`. Retry with Tenacity. Run tests with `pytest -x --tb=short`. Prefer stdlib over third-party packages.
- Emoji: NEVER add emoji to user-facing output (UI text, CLI messages, error messages, logs shown to users, API responses, documentation read by humans). Machine-to-machine documents may use status markers where mechanical recognition adds value.

## [SHOULD]
- No import-only files: If a definition is missing, provide a stub. (TYPE_CHECKING blocks are exempt.)
- Token efficiency: Use grep/glob search tools instead of reading whole files. Use Edit instead of Write for existing files.

CAUTION: Over-engineering → warn. Proceed if repeated.
