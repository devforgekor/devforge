# Package Modularization (10-File Split)

**Date**: 2026-06-26  
**Commit**: 08330ad  
**Impact**: -5,890 lines, 68 files changed

---

## Overview

Split 10 monolithic files into packages with ≤400 lines per module, improving maintainability while preserving backward-compatible import paths.

## Motivation

### Problems with Monolithic Files
- **Cognitive load**: 400-800+ line files difficult to navigate and review
- **Merge conflicts**: High churn areas (feedback, pipeline_common) caused frequent conflicts
- **Testing**: Hard to isolate and mock subsystems
- **Violation of SRP**: Single files handling multiple responsibilities

### Target: 400-Line Modules
- Fits on 2 screens (200 lines/screen)
- Can be reviewed in one sitting (~15 minutes)
- Clear responsibility boundaries

## Refactored Modules

| Original File | Lines | New Package Structure | Submodules |
|--------------|-------|----------------------|------------|
| `scripts/gemini_core.py` | 490 | `scripts/gemini_core/` | `api.py`, `execute.py`, `keys.py`, `tools_def.py` |
| `scripts/lib/feedback.py` | 548 | `scripts/lib/feedback/` | `messages.py`, `patterns.py`, `state.py` |
| `scripts/lib/infra/azure_spot.py` | 627 | `scripts/lib/infra/azure_spot/` | `cli.py`, `config.py`, `manager.py`, `orchestrator.py`, `tunnel.py` |
| `scripts/lib/llm_client.py` | 412 | `scripts/lib/llm_client/` | `feedback.py`, `recovery.py` |
| `scripts/lib/pipeline_common.py` | 822 | `scripts/lib/pipeline_common/` | Split into focused modules |
| `scripts/lib/pod_manager.py` | 566 | `scripts/lib/pod_manager/` | Split into focused modules |
| `scripts/lib/proxy_utils.py` | 835 | `scripts/lib/proxy_utils/` | Split into focused modules |
| `scripts/lib/slack_interactive.py` | 509 | `scripts/lib/slack_interactive/` | Split into focused modules |
| `scripts/blob_explorer.py` | — | `scripts/blob_explorer/` | `blob.py`, `handler.py` |
| `scripts/lib/watchdog/*` | — | `scripts/lib/watchdog/` | `config.py`, `state.py` (new modules) |

**Total reduction**: 5,890 lines of monolithic code → modular packages

## Package Structure Pattern

Each package follows this structure:
```
package/
├── __init__.py      # Public API exports
├── __main__.py      # CLI entry point (if applicable)
├── core.py          # Core business logic
├── config.py        # Configuration and constants
└── utils.py         # Helper functions
```

Example: `scripts/lib/feedback/`
```
feedback/
├── __init__.py      # Exports key classes (FeedbackManager, etc.)
├── messages.py      # Message formatting (69 lines)
├── patterns.py      # Pattern matching logic (184 lines)
└── state.py         # State management (200 lines)
```

## Import Compatibility

**Backward compatible**: Import paths unchanged
```python
# Before (monolithic)
from scripts.lib.feedback import FeedbackManager

# After (package) — SAME IMPORT
from scripts.lib.feedback import FeedbackManager
```

**Implementation**: `__init__.py` re-exports public API
```python
# scripts/lib/feedback/__init__.py
from .state import FeedbackManager
from .patterns import match_feedback_pattern
from .messages import format_message

__all__ = ["FeedbackManager", "match_feedback_pattern", "format_message"]
```

## Verification

### Before Split
```bash
$ wc -l scripts/lib/feedback.py scripts/lib/pipeline_common.py
  548 scripts/lib/feedback.py
  822 scripts/lib/pipeline_common.py
 1370 total
```

### After Split
```bash
$ find scripts/lib/feedback -name "*.py" | xargs wc -l
   48 __init__.py
   69 messages.py
  184 patterns.py
  200 state.py
  501 total  # (48+69+184+200)

$ find scripts/lib/pipeline_common -name "*.py" | xargs wc -l
   69 __init__.py
  [... submodules all <400 lines]
```

## Consequences

### Positive
- **Readability**: All modules ≤400 lines, reviewable in one pass
- **Maintainability**: Clear responsibility boundaries per file
- **Testing**: Easier to mock and isolate subsystems
- **Git history**: Reduced merge conflicts in high-churn areas

### Negative
- **Navigation**: More files to navigate (trade-off for clarity)
- **Import indirection**: `__init__.py` layer adds one hop (negligible)

### Neutral
- No behavior changes — pure refactoring
- Import paths preserved → no caller updates needed

## Related Changes (Same Commit)

- **Watchdog integration**: 3-tier detection (slot deadlock, token stagnation, pipeline timeout)
- **context_limit() refactoring**: See ADR-0008
- **Bug fix**: `enrich.py` model key `'day-enrich'` → `'day-enricher'`

## References

- Commit: 08330ad "watchdog 통합 + context_limit 리팩토링 + 10파일 패키지 분할"
- Code structure: `docs/architecture/code-structure.yaml` (auto-generated)
- Style guide: ≤400 lines per module (llm-common-rule.md)

---

**Maintenance**: Run `scripts/lib/gen_architecture.py` to update `code-structure.yaml` after file reorganizations.
