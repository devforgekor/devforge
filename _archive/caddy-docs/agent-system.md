# Agent Collaboration System — Multi-LLM Shared Handover Protocol

## Overview

This system enables multiple LLM-based coding agents (Claude Code, Aider, Gemini CLI, Qwen CLI, Copilot CLI) to share work context on a single Linux server. The core mechanism is a **filesystem-based handover protocol**: each agent reads the project rules and last session's handover before starting, and writes a structured patch before exiting.

All coordination happens through four files:

| File | Purpose | Authority |
|------|---------|-----------|
| `CLAUDE.md` | Project rules (universal entry point) | Human-authored, agent-read |
| `AGENTS.md` | Symlink → CLAUDE.md (Aider compatibility) | Filesystem |
| `GEMINI.md` | Symlink → CLAUDE.md (Gemini CLI compatibility) | Filesystem |
| `server/handover.yaml` | Session checkpoint + pending work | Agent-read, agent-write |

## System Environment

- **OS**: Oracle Linux Server 9.7 (aarch64)
- **User**: `opc` (single-user server)
- **Shell**: GNU Bash 5.x
- **Python**: 3.11+ (uv-managed)
- **Key tools**: `flock`, `yq` (mikefarah/go version), `git`

## Architecture

```
/opt/projects/
  agent.sh
  agent-system.md.tmpl
  agent-system.md
  scripts/
    manual_handover.sh
    pycharm_handover.sh
    regen_docs.sh
    rotate_handover.sh
    vscode_handover.sh
  server/
    handover.yaml  handover.yaml.bak  handover_recent.yaml
    CLAUDE.yaml  state.yaml  blueprint.yaml  changelog.yaml
    .handover.lock  .last-structural-hash
    archive/  logs/  scripts/
```

## File Symlinks (One-Time Setup)

```bash
cd /opt/projects
ln -sf CLAUDE.md AGENTS.md
ln -sf CLAUDE.md GEMINI.md
```

## CLAUDE.md — Universal Agent Rules

All agents MUST read this file at session start. The following section must be present and visible:

```markdown
## Agent Collaboration Protocol [MUST]

### Before Starting Work
1. Read `/opt/projects/server/handover.yaml` — check `current_task`, `pending`, `known_issues`
2. Read `/opt/projects/server/CLAUDE.yaml` for server state if touching infrastructure
3. Read `/opt/projects/server/blueprint.yaml` if making architectural changes

### During Work
- Follow all [MUST] and [SHOULD] rules in this file
- NEVER modify CLAUDE.md without explicit human request
- NEVER modify CLAUDE.yaml — it is auto-generated
- NEVER modify changelog.yaml — it is append-only and auto-generated

### Before Exiting
- Output a handover patch using the marker convention (see below)
- Update `current_task`, `decisions`, `pending`, `known_issues`

### Handover Patch Format
Output EXACTLY this at the end of your session:

__HANDOVER_PATCH__
```yaml
current_task:
  summary: "<one-line description>"
  started: "<ISO8601 datetime>"
  status: "in_progress|completed|blocked"
decisions:
  - "<decision 1>"
  - "<decision 2>"
pending:
  - "<pending item 1>"
known_issues:
  - "<issue 1>"
```
__END_HANDOVER_PATCH__

Rules:
- The markers MUST be on their own line. Leading/trailing whitespace is tolerated.
- The YAML block MUST be valid YAML (indentation, quoting, no tabs inside values).
- The YAML block MUST NOT contain the marker strings.
- Write concise, factual entries. No markdown formatting inside YAML values.
```

## server/handover.yaml — Structure

```yaml
last_checkpoint:
  time: '2026-05-13T00:17:18.307366+09:00'
  recent_files:
  - /opt/projects/server/scripts/update_handover.py
  - /opt/projects/server/CLAUDE.yaml
  - /opt/projects/server/.handover.lock
  - /opt/projects/server/logs/.raw_491481.tmp
  git:
    /opt/projects/seedling:
      branch: main
      changes: (clean)
      untracked:
      - '"# seedling \354\204\244\352\263\204 \352\263\204\355\232\215\354\204\234.md"'
      - .claude/rules/worklog.md
      - .dockerignore
      - .env.example
      - .github/workflows/deploy.yml
      - .gitignore
      - AGENTS.md
      - CLAUDE.md
      - Dockerfile.sidecar
      - _pending/budget_analyzer.py
      - _pending/gpt_fallback.py
      - _pending/hallucination_verification_v1.2.md
      - _pending/handover_20260501.md
      - _pending/metadata_architecture_v1.md
      - _pending/phase2_plan.md
      - _pending/phase3_plan.md
      - _pending/plugin_refine.py
      - _pending/review_references_separation_20260503.md
```

### Field Semantics

| Field | Writer | Purpose |
|-------|--------|---------|
| `last_checkpoint` | agent.sh (auto) / update_handover.py (auto) | Timestamp, agent name, recent files touched, git status snapshot |
| `current_task` | Agent | What is being worked on right now |
| `decisions` | Agent | Non-obvious choices made and WHY |
| `pending` | Agent | Items that need follow-up |
| `known_issues` | Agent | Bugs, constraints, or workarounds discovered |
| `completed_log` | agent.sh (auto) | Completed task summaries appended on successful patch merge |

> **Note on two recording agents**: `update_handover.py` (Claude Code SessionEnd hook / 10-min timer) writes `last_checkpoint` without the `agent` field and uses per-project nested `git` structure. `agent.sh` (non-Claude agents) adds the `agent` field, flat `git.branch`/`git.commit`, and `completed_log`. The handover file is a union of whichever agent wrote last.

## agent.sh — The Wrapper Script

This is the runtime that wraps non-Claude agents. Claude Code uses its own SessionEnd hook instead.

### Requirements

```bash
# One-time dependency install
dnf install -y util-linux-core   # provides flock
go install github.com/mikefarah/yq/v4@latest   # NOT kislyuk/yq
```

### Full Script

```bash
#!/bin/bash
set -eo pipefail
umask 077
export TZ=UTC

# === agent.sh — Multi-Agent Handover Wrapper ===
# Wraps any LLM CLI agent, captures output, extracts handover patch,
# validates YAML syntax, and merges into server/handover.yaml under flock.
#
# Usage:
#   ./agent.sh <agent_name> -- <command> [args...]
#
# Examples:
#   ./agent.sh aider -- aider --model gemini/gemini-2.5-flash
#   ./agent.sh gemini -- gemini -p "Fix the bug in db.py"
#   ./agent.sh qwen -- qwen -p "Review app/config.py"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HANDOVER_FILE="${SCRIPT_DIR}/server/handover.yaml"
LOCK_FILE="${SCRIPT_DIR}/server/.handover.lock"
LOG_DIR="${SCRIPT_DIR}/server/logs"
CURRENT_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

mkdir -p "$LOG_DIR"

# === Cleanup trap (runs on EXIT, ensures lock fd is released) ===
cleanup() {
    exec 200>&- 2>/dev/null || true
}
trap cleanup EXIT

# === 1. Argument Parsing ===
if [ "$#" -lt 3 ] || [ "$2" != "--" ]; then
    echo "Usage: $0 <agent_name> -- <command> [args...]"
    echo "Example: $0 aider -- aider --model gemini/gemini-2.5-flash"
    exit 1
fi

AGENT_NAME="$1"
shift 2
AGENT_ARGS=("$@")   # Array preserves arguments with spaces

# LOG_FILE uses agent name + UTC timestamp (sortable, identifiable)
LOG_FILE="${LOG_DIR}/agent_handover_${AGENT_NAME}_$(date -u +%Y-%m-%dT%H-%M-%SZ).jsonl"
RAW_LOG="${LOG_DIR}/.raw_$$.tmp"

# JSONL line wrapper (one JSON record per output line)
to_jsonl() {
    while IFS= read -r line; do
        printf '{"ts":"%s","agent":"%s","msg":"%s"}\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            "$1" \
            "$(printf '%s' "$line" | sed 's/\\/\\\\/g; s/"/\\"/g; s/'$'\t''/\\t/g')"
    done
}

# === 2. Pre-flight Checks ===
if [ ! -f "$HANDOVER_FILE" ]; then
    echo "[FATAL] Handover file not found: $HANDOVER_FILE"
    exit 1
fi

if ! command -v yq &>/dev/null; then
    echo "[FATAL] yq (mikefarah/go) is required. Install: go install github.com/mikefarah/yq/v4@latest"
    exit 1
fi

if ! yq --version 2>&1 | grep -q "mikefarah"; then
    echo "[FATAL] Wrong yq installed. Need mikefarah/yq (Go), not kislyuk/yq (Python)."
    echo "Fix: go install github.com/mikefarah/yq/v4@latest"
    exit 1
fi

echo "=== Agent Handover System ==="
echo "Agent : $AGENT_NAME"
echo "Time  : $CURRENT_TIME"
echo "Lock  : $LOCK_FILE"
echo "Log   : $LOG_FILE"
echo ""

# === 3. Execute Agent (capture all output) ===
echo "[SYSTEM] Starting agent: ${AGENT_ARGS[*]}"
echo ""

set +e
# Raw log for patch extraction (temp), JSONL for archival (persisted)
if command -v stdbuf &>/dev/null; then
    stdbuf -oL -eL "${AGENT_ARGS[@]}" 2>&1 | tee "$RAW_LOG" | to_jsonl "$AGENT_NAME" > "$LOG_FILE"
else
    "${AGENT_ARGS[@]}" 2>&1 | tee "$RAW_LOG" | to_jsonl "$AGENT_NAME" > "$LOG_FILE"
fi
AGENT_EXIT=$?
set -e

echo ""
echo "[SYSTEM] Agent exited with code: $AGENT_EXIT"

# === 4. Extract Handover Patch (single awk pass) ===
PATCH_CONTENT=$(awk '
    /^[[:blank:]]*__HANDOVER_PATCH__[[:blank:]]*$/  { flag=1; next }
    /^[[:blank:]]*__END_HANDOVER_PATCH__[[:blank:]]*$/ { exit }
    flag
' "$RAW_LOG" | grep -v '```')

if [ -z "$PATCH_CONTENT" ]; then
    echo "[SYSTEM] No handover patch found. Handover file unchanged."
    echo "[SYSTEM] JSONL log preserved at: $LOG_FILE"
    exit $AGENT_EXIT
fi

echo "$PATCH_CONTENT" > "${RAW_LOG}.patch"
echo "[SYSTEM] Patch extracted: $(wc -l < "${RAW_LOG}.patch") lines"

# === 5. YAML Syntax Validation (before acquiring lock) ===
if ! yq eval '.' "${RAW_LOG}.patch" > /dev/null 2>&1; then
    echo "[FATAL] Patch YAML syntax is INVALID. Handover file NOT modified."
    echo "[FATAL] yq error:"
    yq eval '.' "${RAW_LOG}.patch" 2>&1 || true
    echo ""
    echo "[SYSTEM] Patch content preserved at: ${RAW_LOG}.patch"
    echo "[SYSTEM] Full log preserved at: $LOG_FILE"
    echo "[SYSTEM] Fix the YAML syntax and re-run, or manually edit handover.yaml"
    exit 1
fi

echo "[SYSTEM] YAML syntax validation: PASSED"

# === 6. Acquire Lock and Merge ===
exec 200>"$LOCK_FILE"

echo "[SYSTEM] Acquiring lock..."
flock -w 300 200 || { echo "[FATAL] Lock timeout after 5min."; exit 1; }

echo "[SYSTEM] Lock acquired."

cp "$HANDOVER_FILE" "${HANDOVER_FILE}.bak"

yq eval -i ".last_checkpoint.time = \"$CURRENT_TIME\"" "${RAW_LOG}.patch"
yq eval -i ".last_checkpoint.agent = \"$AGENT_NAME\"" "${RAW_LOG}.patch"

# Explicit merge: arrays = full replace, objects = deep merge.
# Only keys listed here are merged (safe allowlist).
# Agents MUST output complete arrays, not partial additions.
# Arrays are replaced, not appended — this is intentional (declarative state).
yq eval-all '
  select(fileIndex == 0) as $base |
  select(fileIndex == 1) as $patch |
  $base |
  .pending = ($patch.pending // $base.pending) |
  .decisions = ($patch.decisions // $base.decisions) |
  .known_issues = ($patch.known_issues // $base.known_issues) |
  .completed_log = ($patch.completed_log // $base.completed_log) |
  .current_task = ($base.current_task * ($patch.current_task // {})) |
  .last_checkpoint = ($base.last_checkpoint * ($patch.last_checkpoint // {}))
' "$HANDOVER_FILE" "${RAW_LOG}.patch" > "${HANDOVER_FILE}.tmp"

# Post-merge validation
if ! yq eval '.' "${HANDOVER_FILE}.tmp" > /dev/null 2>&1; then
    echo "[FATAL] Merged YAML is INVALID. Rolling back."
    echo "[FATAL] Backup preserved at: ${HANDOVER_FILE}.bak"
    echo "[FATAL] Debug files: ${RAW_LOG}.patch, ${HANDOVER_FILE}.tmp"
    rm -f "${HANDOVER_FILE}.tmp"
    exit 1
fi

mv "${HANDOVER_FILE}.tmp" "$HANDOVER_FILE"

# === 7. Cap completed_log to prevent unbounded growth ===
MAX_COMPLETED=50
COMPLETED_COUNT=$(yq eval '.completed_log | length' "$HANDOVER_FILE" 2>/dev/null || echo 0)
if [ "$COMPLETED_COUNT" -gt "$MAX_COMPLETED" ]; then
    START_INDEX=$((COMPLETED_COUNT - MAX_COMPLETED))
    yq eval -i ".completed_log |= .[$START_INDEX:]" "$HANDOVER_FILE"
    echo "[SYSTEM] Trimmed $((COMPLETED_COUNT - MAX_COMPLETED)) oldest completed_log entries"
fi

# Release lock (trap also handles this on abnormal exit)
exec 200>&-

echo "[SYSTEM] Handover merged successfully."

# === 8. Cleanup on Success ===
rm -f "${LOG_FILE}" "${RAW_LOG}" "${RAW_LOG}.patch"
echo "[SYSTEM] Temporary files cleaned up."
echo "[SYSTEM] Backup of previous handover: ${HANDOVER_FILE}.bak"

exit $AGENT_EXIT
```

### Script Defense Layers

| Layer | Mechanism | Failure Behavior |
|-------|-----------|------------------|
| **Marker detection** | `grep -E` with `[[:blank:]]*` (POSIX) | No marker → skip, log preserved |
| **Patch extraction** | `awk` with flag toggle, same POSIX regex | Empty patch → exit 1, log preserved |
| **YAML pre-validation** | `yq eval '.'` on patch before touching handover | Invalid → exit 1, patch + log preserved, handover untouched |
| **Flock mutual exclusion** | `flock 200` on `.handover.lock` | Concurrent agents serialize cleanly |
| **Pre-merge backup** | `cp handover.yaml handover.yaml.bak` | Recovery: `mv handover.yaml.bak handover.yaml` |
| **Post-merge validation** | `yq eval '.'` on merged result | Invalid → rollback, .bak preserved |
| **Cleanup gating** | `rm` only on success path | Any failure keeps all debug artifacts |
| **Pipeline failure detection** | `set -eo pipefail` | Agent crash in pipeline detected, not silently swallowed |
| **Systemd output buffering** | `stdbuf -oL -eL` | Line-buffered output ensures real-time log capture under journald |
| **Log directory isolation** | `server/logs/` (not `/tmp`), `umask 077` | Prevents sensitive log leakage after reboot |
| **Word splitting prevention** | `"${AGENT_ARGS[@]}"` array | Arguments with spaces preserved correctly |
| **Abnormal exit safety** | `trap cleanup EXIT` | Lock fd released even on SIGINT/flock failure |
| **yq binary verification** | Version string check for "mikefarah" | Prevents silent failure with wrong yq implementation |
| **Array replacement merge** | Explicit key-by-key merge, arrays fully replaced | Prevents zombie entries from yq index-based array merge |
| **Flcck timeout** | `flock -w 300` (5 minutes) | Prevents infinite wait on stale lock |
| **Git audit trail** | `git add` + `git commit` on successful merge | Long-term history beyond single `.bak` file |
| **Claude Code flock** | `update_handover.py` uses same `.handover.lock` | Prevents Claude vs agent.sh concurrent writes |
| **completed_log capping** | `MAX_COMPLETED=50` sliding window | Prevents unbounded handover file growth |
| **Git context injection** | branch, commit, recent_files stamped into patch | Full audit trail per agent run |

### Operational Tools

**status.sh** — Quick handover state viewer:
```bash
./status.sh
```
Reads `server/handover.yaml` and displays: last update time + agent, current task with status, pending list, known issues, completed log. Requires `yq`.

**server/scripts/update_handover.py** — Claude Code SessionEnd hook target:

```python
#!/usr/bin/env python3
"""update_handover.py — Mechanical session context capture.

Triggered by SessionEnd hook AND 10-min checkpoint timer.
Captures file changes and system fingerprint. AI writes decisions/tasks inline.
"""

import fcntl
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

SERVER_DIR = Path("/opt/projects/server")
HANDOVER_FILE = SERVER_DIR / "handover.yaml"
LOCK_FILE = SERVER_DIR / ".handover.lock"
PROJECT_DIRS = [
    Path("/opt/projects/seedling"),
    Path("/opt/projects/common-lib"),
    Path("/opt/projects/server"),
]
TZ = timezone(timedelta(hours=9))
CHECKPOINT_WINDOW_HOURS = 1  # scan files modified within this window


def _run(cmd, timeout=15, cwd="/opt/projects/seedling"):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.stdout.strip()
    except Exception:
        return ""


def load_handover():
    if HANDOVER_FILE.exists():
        with open(HANDOVER_FILE) as f:
            return yaml.safe_load(f) or {}
    return {}


def save_handover(data):
    HANDOVER_FILE.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120))


def scan_recent_files():
    """Find files modified in the last CHECKPOINT_WINDOW_HOURS."""
    recent = []
    for proj in PROJECT_DIRS:
        if not proj.exists():
            continue
        out = _run(["find", str(proj), "-type", "f",
                     "-mmin", f"-{CHECKPOINT_WINDOW_HOURS * 60}",
                     "-not", "-path", "*/.git/*",
                     "-not", "-path", "*/__pycache__/*",
                     "-not", "-path", "*/.venv/*",
                     "-not", "-path", "*/node_modules/*",
                     "-not", "-path", "*/.pytest_cache/*",
                     "-not", "-name", "*.pyc",
                     "-not", "-name", "uv.lock",
                     "-not", "-path", "*/state.yaml",
                     "-not", "-path", "*/changelog*.yaml",
                     "-not", "-path", "*/handover.yaml",
                     "-not", "-path", "*/blueprint.yaml",
                     "-not", "-path", "*/.last-*",
                    ])
        for line in out.split("\n"):
            line = line.strip()
            if line:
                recent.append(line)
    return recent


def git_status():
    """Get git status summaries for project dirs."""
    result = {}
    for proj in PROJECT_DIRS:
        git_dir = proj / ".git"
        if not git_dir.exists():
            continue
        branch = _run(["git", "branch", "--show-current"], cwd=str(proj))
        stat = _run(["git", "diff", "--stat"], cwd=str(proj))
        untracked = _run(["git", "ls-files", "--others", "--exclude-standard"], cwd=str(proj))
        result[str(proj)] = {
            "branch": branch or "unknown",
            "changes": stat if stat else "(clean)",
            "untracked": untracked.split("\n") if untracked else [],
        }
    return result


def checkpoint_hash(checkpoint):
    """Hash the mechanical data (excluding time) to detect changes."""
    if not checkpoint:
        return ""
    payload = {
        "recent_files": sorted(checkpoint.get("recent_files", [])),
        "git": checkpoint.get("git", {}),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def main():
    now = datetime.now(TZ)
    data = load_handover()

    # Preserve AI-written sections
    ai_sections = {}
    for key in ("current_task", "decisions", "pending", "known_issues"):
        if key in data:
            ai_sections[key] = data[key]

    # Build mechanical checkpoint
    recent_files = scan_recent_files()
    git_state = git_status()

    checkpoint = {
        "time": now.isoformat(),
        "recent_files": recent_files[:30],  # cap at 30
        "git": git_state,
    }

    # Skip write if mechanical data unchanged
    old_hash = checkpoint_hash(data.get("last_checkpoint"))
    new_hash = checkpoint_hash(checkpoint)
    if old_hash and old_hash == new_hash:
        return 0

    # Merge: AI content preserved, mechanical updated
    data = {
        "last_checkpoint": checkpoint,
        "current_task": ai_sections.get("current_task", {"summary": "", "started": "", "branch": ""}),
        "decisions": ai_sections.get("decisions", []),
        "pending": ai_sections.get("pending", []),
        "known_issues": ai_sections.get("known_issues", []),
        "completed_log": ai_sections.get("completed_log", data.get("completed_log", [])),
    }

    # Use the same lock as agent.sh to prevent concurrent writes
    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        save_handover(data)

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### Failure Recovery Quick Reference

| Symptom | Recovery Action |
|---------|----------------|
| Agent crash, no patch | Read `$LOG_FILE` for error context. Handover unchanged. |
| Invalid YAML patch | Read `${LOG_FILE}.patch`, fix YAML, run `yq eval '.'` to verify, then manually merge |
| Merged YAML corrupt | `mv handover.yaml.bak handover.yaml` restores previous state |
| Two agents collided | The second one waits on `flock`. Order determined by kernel scheduler. |
| Lock file stuck (rare) | `rm server/.handover.lock` and re-run. Lock is released on script exit. |

## Handover Archiving — 3-Tier Rotation

Prevents unbounded growth of `handover.yaml`. Runs daily via cron.

**Flow**: Hot (handover.yaml, ≤30d) → Recent (handover_recent.yaml, ≤90d) → Archive (quarterly files, ≤3yr) → Delete

```bash
#!/bin/bash
# scripts/rotate_handover.sh — 3-Tier handover archival
# Hot (handover.yaml) → recent (30d) → archive (90d quarterly) → delete (3yr)
# Run via cron: 0 3 * * * /opt/projects/scripts/rotate_handover.sh

set -euo pipefail
export TZ=UTC

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HANDOVER="${SCRIPT_DIR}/server/handover.yaml"
RECENT="${SCRIPT_DIR}/server/handover_recent.yaml"
ARCHIVE_DIR="${SCRIPT_DIR}/server/archive"
LOCK_FILE="${SCRIPT_DIR}/server/.handover.lock"

CUTOFF_30=$(date -d '30 days ago' +%Y-%m-%dT%H:%M:%SZ)
CUTOFF_90=$(date -d '90 days ago' +%Y-%m-%dT%H:%M:%SZ)

MONTH=$(date +%m)
MONTH=$((10#$MONTH))
QUARTER=$(( (MONTH - 1) / 3 + 1 ))
CURRENT_QUARTER="$(date +%Y)-Q${QUARTER}"

exec 200>"$LOCK_FILE"
flock -w 300 200 || { echo "[FATAL] Lock timeout after 5min."; exit 1; }

[ ! -f "$RECENT" ] && echo 'recent_tasks: []' > "$RECENT"
mkdir -p "$ARCHIVE_DIR"

# Tier 1: handover.yaml → handover_recent.yaml (tasks older than 30 days)
OLD_COUNT=$(yq eval "
  [.completed_log[] | select((.completed // .started // \"9999-12-31T23:59:59Z\") < \"$CUTOFF_30\")] | length
" "$HANDOVER" 2>/dev/null || echo 0)

if [ "$OLD_COUNT" -gt 0 ]; then
    TMP_OLD=$(mktemp)
    yq eval "
      [.completed_log[] | select((.completed // .started // \"9999-12-31T23:59:59Z\") < \"$CUTOFF_30\")]
    " "$HANDOVER" > "$TMP_OLD"

    yq eval-all 'select(fileIndex == 0).recent_tasks += (select(fileIndex == 1) | .[])' \
        "$RECENT" "$TMP_OLD" > "${RECENT}.tmp"
    mv "${RECENT}.tmp" "$RECENT"

    yq eval -i ".completed_log |= map(select((.completed // .started // \"9999-12-31T23:59:59Z\") >= \"$CUTOFF_30\"))" "$HANDOVER"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Moved $OLD_COUNT entries to handover_recent.yaml"
    rm -f "$TMP_OLD"
fi

# Tier 2: handover_recent.yaml → archive (tasks older than 90 days)
ARCHIVE_COUNT=$(yq eval "
  [.recent_tasks[] | select((.completed // .started // \"9999-12-31T23:59:59Z\") < \"$CUTOFF_90\")] | length
" "$RECENT" 2>/dev/null || echo 0)

if [ "$ARCHIVE_COUNT" -gt 0 ]; then
    ARCHIVE_FILE="${ARCHIVE_DIR}/handover_${CURRENT_QUARTER}.yaml"
    [ ! -f "$ARCHIVE_FILE" ] && echo 'archived_tasks: []' > "$ARCHIVE_FILE"

    TMP_ARCHIVE=$(mktemp)
    yq eval "
      [.recent_tasks[] | select((.completed // .started // \"9999-12-31T23:59:59Z\") < \"$CUTOFF_90\")]
    " "$RECENT" > "$TMP_ARCHIVE"

    yq eval-all 'select(fileIndex == 0).archived_tasks += (select(fileIndex == 1) | .[])' \
        "$ARCHIVE_FILE" "$TMP_ARCHIVE" > "${ARCHIVE_FILE}.tmp"
    mv "${ARCHIVE_FILE}.tmp" "$ARCHIVE_FILE"

    yq eval -i ".recent_tasks |= map(select((.completed // .started // \"9999-12-31T23:59:59Z\") >= \"$CUTOFF_90\"))" "$RECENT"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Archived $ARCHIVE_COUNT entries to $CURRENT_QUARTER"
    rm -f "$TMP_ARCHIVE"
fi

# Tier 3: Delete archives older than 3 years
find "$ARCHIVE_DIR" -name "*.yaml" -mtime +1095 -delete 2>/dev/null || true

exec 200>&-
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Rotation complete"
```

**Cron configuration** (`crontab -e`):
```cron
0 3 * * * /opt/projects/scripts/rotate_handover.sh >> /var/log/handover_rotation.log 2>&1
```

## Agent-Specific Integration

### Claude Code

Claude reads `CLAUDE.md` and `.claude/rules/` automatically on every session. The SessionEnd hook in `~/.claude/settings.json` triggers `update_handover.py`.

**Configuration** (`~/.claude/settings.json`):
```json
{
  "hooks": {
    "SessionEnd": [
      {
        "command": "cd /opt/projects && python server/scripts/update_handover.py"
      }
    ]
  }
}
```

No wrapper script needed. Claude's native hook system handles handover updates.

### Aider

**Setup**:
```bash
# Aider reads AGENTS.md (= symlink to CLAUDE.md) automatically
# Create post-command hook
mkdir -p ~/.aider
cat > ~/.aider.hooks.post << 'HOOK'
#!/bin/bash
# After each aider session, wrap handover update
/opt/projects/agent.sh aider -- echo "Session complete" 2>/dev/null || true
HOOK
```

**Alternative**: Use the wrapper directly:
```bash
/opt/projects/agent.sh aider -- aider --model gemini/gemini-2.5-flash
```

### Gemini CLI

**Setup**:
```bash
# Gemini CLI reads GEMINI.md (= symlink to CLAUDE.md) automatically
# Create policy file for handover protocol
cat > ~/.gemini/policy.md << 'POLICY'
# Handover Protocol
At the end of EVERY session, output a handover patch:

__HANDOVER_PATCH__
```yaml
current_task:
  summary: "<description>"
  status: "in_progress|completed|blocked"
decisions: []
pending: []
known_issues: []
```
__END_HANDOVER_PATCH__

Read /opt/projects/server/handover.yaml before starting.
POLICY
```

**Usage**:
```bash
/opt/projects/agent.sh gemini -- gemini --policy ~/.gemini/policy.md -p "Fix the bug"
```

### Qwen CLI (qwen)

Qwen CLI has no native hooks or policy files. The agent wrapper script is the only integration point.

**Usage**:
```bash
# Inject only the handover protocol section (not full CLAUDE.md) to save tokens
PROTOCOL=$(awk '/^## Agent Collaboration Protocol/{f=1;next} /^## /{f=0} f' /opt/projects/CLAUDE.md)
/opt/projects/agent.sh qwen -- qwen -p "$PROTOCOL

Current handover: $(cat /opt/projects/server/handover.yaml)

Task: Fix the bug in db.py"
```

**Critical limitation**: Qwen CLI does not automatically read CLAUDE.md. Injecting the full file wastes context window. Extract only the "Agent Collaboration Protocol" section plus the current handover state.

### GitHub Copilot Family

GitHub Copilot has multiple interfaces with different integration requirements:

#### Copilot CLI (No Integration)

**Products**: `gh copilot suggest`, `gh copilot explain`

Single-shot command suggestions. Does not modify files or maintain session state. No integration needed — these are lookup tools, not autonomous agents.

#### Copilot Chat (Manual Handover Recommended)

**Products**: VSCode Copilot Chat, IDE chat panels

Multi-turn conversations with code suggestions. Can explain and propose changes, but you manually apply them. Since Copilot Chat runs inside the IDE without CLI hooks, handover updates must be manual.

**Workflow**:

1. Before starting: paste handover context into the chat
2. After completing: run `./scripts/manual_handover.sh copilot-chat`

**VSCode Task** (optional, for auto-tracking):
```json
// .vscode/tasks.json
{
  "version": "2.0.0",
  "tasks": [{
    "label": "Update Handover",
    "type": "shell",
    "command": "${workspaceFolder}/scripts/vscode_handover.sh",
    "presentation": { "reveal": "silent" }
  }]
}
```

#### Copilot Edits (Semi-Automated Integration)

**Products**: VSCode Copilot Edits (file modification agent)

Directly edits multiple files. This IS an autonomous agent and should update the handover.

**Option 1**: Run `./scripts/manual_handover.sh copilot-edits` after edits complete.

**Option 2**: Git-based auto-detection via `scripts/vscode_handover.sh`.

**VSCode Keybinding**:
```json
// .vscode/keybindings.json
[{ "key": "ctrl+shift+h", "command": "workbench.action.tasks.runTask", "args": "Update Handover" }]
```

#### Copilot Workspace (Future: API Required)

**Products**: GitHub Copilot Workspace (web-based project editor)

Full project-level autonomous coding. Plans, implements across files, commits to branches.

**Status**: Not yet supported. Requires GitHub API access or webhook integration when available.

### PyCharm Junie (JetBrains AI Assistant)

PyCharm's Junie runs inside the IDE like Copilot Chat/Edits. Integration uses JetBrains **External Tools** with a keyboard shortcut.

**Approach**: External Tool mapped to `Ctrl+Shift+H` triggers `scripts/pycharm_handover.sh`, which detects uncommitted changes via `git diff` and updates handover under flock.

**Setup**:

1. Create the script:
```bash
#!/bin/bash
# scripts/pycharm_handover.sh — PyCharm Junie External Tool target
# Usage: Called from PyCharm External Tools or terminal after Junie session.
# Detects uncommitted changes via git diff and updates handover under flock.

set -e

HANDOVER="/opt/projects/server/handover.yaml"
LOCK_FILE="/opt/projects/server/.handover.lock"
PROJECT_ROOT="/opt/projects"

cd "$PROJECT_ROOT"

CHANGED_FILES=$(git diff --name-only HEAD 2>/dev/null | head -10 | yq -n '[inputs]' 2>/dev/null || echo '[]')

if [ "$CHANGED_FILES" = "[]" ]; then
    echo "[INFO] No uncommitted changes detected."
    exit 0
fi

GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
GIT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")

(
    flock -x 200

    cp "$HANDOVER" "${HANDOVER}.bak"

    yq eval -i "
      .last_checkpoint.time = \"$(date -Iseconds)\" |
      .last_checkpoint.agent = \"pycharm-junie\" |
      .last_checkpoint.recent_files = $CHANGED_FILES |
      .last_checkpoint.git.branch = \"$GIT_BRANCH\" |
      .last_checkpoint.git.commit = \"$GIT_COMMIT\"
    " "$HANDOVER"

    echo "[SYSTEM] Handover updated."
    echo "[SYSTEM] Branch: $GIT_BRANCH ($GIT_COMMIT)"

) 200>"$LOCK_FILE"
```

2. PyCharm: Settings → Tools → External Tools → +
   - Name: `Update Handover`
   - Program: `/opt/projects/scripts/pycharm_handover.sh`
   - Working directory: `/opt/projects`
   - Synchronize files after execution: checked
3. Keymap: search "Update Handover" → assign `Ctrl+Shift+H`

**Alternative: Git Pre-Commit Hook** (fully automatic):

```bash
#!/bin/bash
# .git/hooks/pre-commit
STAGED=$(git diff --cached --name-only | yq -n '[inputs]' 2>/dev/null)
[ "$STAGED" = "[]" ] && exit 0
(
    flock -n 200 || exit 0
    yq eval -i "
      .last_checkpoint.time = \"$(date -Iseconds)\" |
      .last_checkpoint.agent = \"pycharm-junie\" |
      .last_checkpoint.recent_files = $STAGED
    " /opt/projects/server/handover.yaml
) 200>/opt/projects/server/.handover.lock
exit 0
```

### Manual Agent Scripts

**scripts/manual_handover.sh** — Interactive handover updater for agents without CLI hooks:

```bash
#!/bin/bash
# scripts/manual_handover.sh — Interactive handover updater for manual agents
# Usage: ./scripts/manual_handover.sh <agent_name>
#   ./scripts/manual_handover.sh copilot-chat
#   ./scripts/manual_handover.sh copilot-edits

set -e

HANDOVER="/opt/projects/server/handover.yaml"
LOCK_FILE="/opt/projects/server/.handover.lock"
AGENT_NAME="${1:-manual}"

echo "=== Manual Handover Update ==="
echo "Agent: $AGENT_NAME"
echo ""

echo "Current task: $(yq '.current_task.summary' "$HANDOVER")"
echo ""

read -p "Task completed? (y/n): " COMPLETED
if [ "$COMPLETED" = "y" ]; then
    STATUS="completed"
else
    STATUS="in_progress"
fi

read -p "New pending items (comma-separated, or empty): " PENDING_INPUT
read -p "New known issues (comma-separated, or empty): " ISSUES_INPUT

# Get changed files from git
CHANGED_FILES=$(cd /opt/projects && git diff --name-only HEAD 2>/dev/null | head -10 | yq -n '[inputs]' 2>/dev/null || echo '[]')

# Update handover under lock
(
    flock -x 200

    cp "$HANDOVER" "${HANDOVER}.bak"

    yq eval -i "
      .last_checkpoint.time = \"$(date -Iseconds)\" |
      .last_checkpoint.agent = \"$AGENT_NAME\" |
      .last_checkpoint.recent_files = $CHANGED_FILES |
      .current_task.status = \"$STATUS\"
    " "$HANDOVER"

    # Handle pending items
    if [ -n "$PENDING_INPUT" ]; then
        echo "$PENDING_INPUT" | tr ',' '\n' | sed 's/^ *//;s/ *$//' | grep -v '^$' | \
            yq eval '.pending = load("/dev/stdin")' - | \
            yq eval-all 'select(fileIndex == 0) as $base | select(fileIndex == 1) as $patch | $base | .pending = $patch.pending' "$HANDOVER" - > "${HANDOVER}.tmp"
        mv "${HANDOVER}.tmp" "$HANDOVER"
    fi

    # Handle known issues
    if [ -n "$ISSUES_INPUT" ]; then
        echo "$ISSUES_INPUT" | tr ',' '\n' | sed 's/^ *//;s/ *$//' | grep -v '^$' | \
            yq eval '.known_issues = load("/dev/stdin")' - | \
            yq eval-all 'select(fileIndex == 0) as $base | select(fileIndex == 1) as $patch | $base | .known_issues = $patch.known_issues' "$HANDOVER" - > "${HANDOVER}.tmp"
        mv "${HANDOVER}.tmp" "$HANDOVER"
    fi

    echo "[SYSTEM] Handover updated successfully."

) 200>"$LOCK_FILE"

echo ""
echo "Updated state saved. View with: ./status.sh"
```

**scripts/vscode_handover.sh** — Non-blocking auto-update on file save (VSCode task target). Uses `flock -n` to silently skip if another agent holds the lock.

```bash
#!/bin/bash
# scripts/vscode_handover.sh — Auto-update handover on file changes (VSCode task)
# Called by VSCode tasks.json or keybinding.
# Uses non-blocking flock: if another agent is active, silently skip.

HANDOVER="/opt/projects/server/handover.yaml"
LOCK_FILE="/opt/projects/server/.handover.lock"
PROJECT_ROOT="/opt/projects"

cd "$PROJECT_ROOT"

# Only update if there are uncommitted changes
if ! git diff --quiet HEAD 2>/dev/null; then
    (
        flock -n 200 || exit 0  # Non-blocking: skip if another agent is active

        yq eval -i "
          .last_checkpoint.time = \"$(date -Iseconds)\" |
          .last_checkpoint.agent = \"vscode-autosave\" |
          .last_checkpoint.recent_files = $(git diff --name-only HEAD | head -5 | yq -n '[inputs]' 2>/dev/null || echo '[]')
        " "$HANDOVER"

    ) 200>"$LOCK_FILE"
fi
```

### Integration Matrix

| Agent | Integration | Automation | Setup |
|-------|------------|------------|-------|
| Claude Code | SessionEnd hook | Full auto | Low |
| Aider | agent.sh wrapper | Full auto | Low |
| Gemini CLI | agent.sh wrapper | Full auto | Low |
| Qwen CLI | agent.sh + prompt injection | Semi-auto | Medium |
| Copilot CLI | None needed | N/A | N/A |
| Copilot Chat | manual_handover.sh | Manual | Low |
| Copilot Edits | manual_handover.sh / vscode_handover.sh | Semi-auto | Medium |
| Copilot Workspace | Webhook (future) | N/A | High |
| PyCharm Junie | External Tool / Git Hook | Semi-auto | Low |

## .gitignore

```gitignore
# Agent handover artifacts
server/handover.yaml.bak
server/.handover.lock
server/*.patch.tmp

# Agent logs (moved from /tmp for security)
server/logs/

# Handover archival
server/handover_recent.yaml
server/archive/
```

## Systemd Timer Integration

For autonomous agent runs (e.g., overnight batch tasks), create a systemd user timer:

```ini
# ~/.config/systemd/user/agent-nightly.service
[Unit]
Description=Nightly agent run

[Service]
Type=oneshot
Environment="PATH=/usr/local/bin:/usr/bin:/bin:/home/opc/.local/bin"
StandardOutput=journal+console
StandardError=journal+console
# stdbuf -oL in agent.sh ensures line-buffered output for journald
ExecStart=/opt/projects/agent.sh aider -- aider --message "Run daily code review on app/"
```

```ini
# ~/.config/systemd/user/agent-nightly.timer
[Unit]
Description=Nightly agent timer

[Timer]
OnCalendar=*-*-* 03:00:00
RandomizedDelaySec=600
Persistent=true

[Install]
WantedBy=timers.target
```

## Design Decisions

### Why `flock` instead of a queue file
A queue (FIFO file) adds complexity without benefit for a single-user server. `flock` provides mutual exclusion with zero overhead: the second agent blocks until the first finishes, then proceeds. No lost work, no queue management.

### Why `yq` deep-merge instead of append-only
Append-only would create duplicate `current_task` entries. Deep-merge (`*`) overwrites matching top-level keys while preserving array items. This keeps handover.yaml clean without manual deduplication.

### Why validate YAML BEFORE acquiring the lock
If the patch is invalid, there is no work to do. Acquiring the lock before validation would hold other agents for no reason. Validate first, lock only when ready to write.

### Why a backup instead of a git commit
Git commits require a clean working tree and generate permanent history. The `.bak` file is a one-step undo that avoids polluting the git log with mechanical handover snapshots. The `completed_log` array in handover.yaml serves as the permanent record of completed work.

### Why `umask 077` instead of explicit `chmod`
`umask 077` applies to ALL temp files created by the script (log, patch, tmp merge). Individual `chmod` calls can miss files and create race windows. Set once at the top, everything is protected.

### Why `[[:blank:]]` instead of `\t`
POSIX character class `[[:blank:]]` matches both space and tab on all Unix-like systems. `\t` in ERE is a GNU extension; BSD grep/awk interpret it as literal `t`. Since handover patches may originate from macOS development machines, POSIX compliance prevents silent marker misses.

## Troubleshooting

| Problem | Likely Cause | Fix |
|---------|-------------|-----|
| "No handover patch marker found" | Agent didn't output markers | Check agent's rules/policy file; manually add patch |
| "Patch YAML syntax is INVALID" | Agent malformed the YAML block | Read `${LOG_FILE}.patch`, fix indentation, re-apply |
| "Merged YAML is INVALID" | Patch YAML conflicts with handover structure | Diff `.bak` vs `.patch`, resolve manually |
| "Wrong yq installed" | kislyuk/yq (Python) is in PATH | `go install github.com/mikefarah/yq/v4@latest` |
| Agent hangs after starting | Another agent holds the lock | Wait, or `rm server/.handover.lock` if stale |
| Gemini ignores GEMINI.md | Gemini CLI may not auto-read it | Use `--policy` flag explicitly |
| Qwen ignores all rules | Qwen CLI has no rules file support | Inject rules into the prompt string |

## Migration Checklist (Server Move)

When moving this system to a new server:

- [ ] Rsync `/opt/projects/` (all files including `.git/`)
- [ ] Recreate symlinks: `AGENTS.md` → `CLAUDE.md`, `GEMINI.md` → `CLAUDE.md`
- [ ] Install dependencies: `flock` (util-linux-core), `yq` (mikefarah/go)
- [ ] Verify `umask 077` in agent.sh
- [ ] Restore `.gitignore` with handover artifact exclusions
- [ ] Reconfigure Claude Code SessionEnd hook in `~/.claude/settings.json`
- [ ] Set up Aider/Gemini hooks or policy files
- [ ] Test: run each agent with agent.sh, verify handover.yaml is updated
- [ ] Test: simulate concurrent agents to verify flock serialization
- [ ] Test: inject invalid YAML patch, verify rejection and log preservation
- [ ] Run `./scripts/regen_docs.sh` to regenerate agent-system.md from template

## References

- `server/CLAUDE.yaml` — Server infrastructure state (auto-generated every 15min)
- `server/blueprint.yaml` — Target architecture and backlog
- `server/changelog.yaml` — Append-only change log (entries < 3 months)
- `server/handover.yaml` — Session checkpoint (read before work, write after)
- `docs/spec-doc-sync.md` — Design spec for this document's auto-generation system
