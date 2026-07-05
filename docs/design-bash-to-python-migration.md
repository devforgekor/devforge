# DevForge Bash → Python Migration Plan

## Executive Summary

DevForge 서버에는 18개의 active bash 스크립트(~2,500줄)가 운영 중이다. 핵심 오케스트레이터(day_cycle.sh 429줄, night_cycle.sh 245줄, auto_mode.sh 271줄, model_ctl.sh 240줄)가 bash로 작성되어 있으며, python3 -c 인라인 코드 15회 이상 포함되어 유지보수와 디버깅이 어렵다. 본 문서는 이들 bash 스크립트를 단계적으로 Python으로 전환하는 설계를 제시한다.

**접근법**: 모듈별 1:1 Python 파일 교체 (점진적 전환, 각 파일 독립 교체 가능, systemd ExecStart만 변경)

## 1. Current State Analysis

### 1.1 Active Bash Scripts (18 files, ~2,500 lines)

| # | Script | Lines | Category | Priority | Key Complexity |
|---|--------|-------|----------|----------|----------------|
| 1 | day_cycle.sh | 429 | Orchestrator | **P0** | pipeline_state 7단계 FSM, python3 -c inline 4회, 예산 계산, Slack alert |
| 2 | night_cycle.sh | 245 | Orchestrator | **P0** | mode 전환, debate/verify 오케스트레이션, retry 로직 |
| 3 | auto_mode.sh | 271 | Batch Runner | **P0** | Markdown 파서(awk), Claude Code 실행기, 메모리 체크 |
| 4 | lib/model_ctl.sh | 240 | Library | **P0** | inference 생애주기, python3 -c inline 3회, health/probe wait |
| 5 | claude_code_runner.sh | 126 | Utility | **P1** | A/B 테스트 루프, proxy env 관리 |
| 6 | run_verify_compare.sh | 100 | Test | **P1** | Python inline 40줄 config 패치 |
| 7 | gemini_session_start.sh | 99 | Utility | **P1** | Key rotation + tmux session |
| 8 | run_baseline_monitor.sh | 82 | Test | **P1** | JSON parsing, YAML 생성, DB query |
| 9 | claude_code_wrapper.sh | 85 | Wrapper | **P1** | Proxy canonicalization + curl |
| 10 | weekly_enrich_rebuild.sh | 56 | Timer Batch | **P2** | Python import 2회 호출 |
| 11 | system_sync.sh | 41 | Timer Batch | **Unchanged** | 간단 순차 |
| 12 | open-newhand.sh | 35 | Utility | **Unchanged** | git add/commit/push |
| 13 | run_pipeline_bg.sh | 21 | Script | **Unchanged** | 단순 순차 실행 |
| 14 | github_mcp_wrapper.sh | 15 | Wrapper | **Unchanged** | Secrets → exec |
| 15 | worker-entrypoint.sh | 8 | Container | **Unchanged** | PID 1 |
| 16 | fastapi-entrypoint.sh | 2 | Container | **Unchanged** | PID 1 |
| 17 | mcp_entrypoint.sh | 3 | Container | **Unchanged** | PID 1 |
| 18 | embed_runner.py | N/A | (기존 Python) | — | 이미 Python |

### 1.2 Pattern Analysis

**4 anti-patterns identified:**

1. **python3 -c inline** (15+ occurrences): Debugging impossible, syntax errors invisible until runtime, no import caching
2. **bash FSM** (day_cycle.sh `pipeline_state`): 7-state transitions managed with if/elif chains and DB queries
3. **env file state sharing** (MODE=night, MODEL_NAME=...): Race conditions, no atomic writes, grep/cut parsing
4. **Markdown parser in awk** (auto_mode.sh): HTML comment skip + heading extraction + multiline body — 20 lines of awk

## 2. Design Principles

| Principle | Description |
|-----------|-------------|
| **1:1 Replacement** | Each bash script → one Python module. No restructuring during migration. |
| **Incremental** | One script at a time. Systemd ExecStart updated per conversion. |
| **Zero Behavioral Change** | Output, exit codes, error handling match original. Verified via parallel run. |
| **Library First** | Shared logic (model_ctl) converted first. Consumers (day_cycle, night_cycle) follow. |
| **No New Dependencies** | stdlib only: `subprocess`, `argparse`, `pathlib`, `logging`, `json`, `yaml` (stdlib). |
| **Explicit Exit Code** | Every script MUST `sys.exit(main())`. Python does NOT auto-propagate last command's exit code like bash's implicit `exit $?`. |
| **Explicit subprocess Timeout** | Every `subprocess.run()` in systemd-executed scripts MUST specify `timeout=`. Without it, a hung subprocess blocks indefinitely — bash relies on systemd's TimeoutSec, but Python's subprocess.run() default is infinite wait. |
| **Keep Entrypoints** | Container entrypoints (<10 lines, PID 1) stay in bash. |

## 3. Module Architecture

### 3.1 Target Directory Layout

```
scripts/
├── cli.py                      # [EXISTING] Keep argparse as-is (search/save CLI)
├── day_cycle.py                 # [NEW] day_cycle.sh replacement
├── night_cycle.py               # [NEW] night_cycle.sh replacement
├── auto_mode.py                 # [NEW] auto_mode.sh replacement
├── claude_code_runner.py        # [NEW] claude_code_runner.sh replacement
├── claude_code_wrapper.py       # [NEW] claude_code_wrapper.sh replacement
├── gemini_session.py            # [NEW] gemini_session_start.sh replacement
├── system_sync.py               # [NEW] system_sync.sh replacement
├── weekly_enrich_rebuild.py     # [NEW] weekly_enrich_rebuild.sh replacement
├── run_baseline_monitor.py      # [NEW] run_baseline_monitor.sh replacement
├── run_verify_compare.py        # [NEW] run_verify_compare.sh replacement
├── lib/
│   ├── model_ctl.py             # [NEW] model_ctl.sh replacement
│   ├── db.py                    # [EXISTING] psql wrapper
│   └── notify.py                # [EXISTING] Slack/Telegram
├── tests/
│   └── ...                      # [EXISTING] test scripts
├── pipelines/
│   └── ...                      # [EXISTING] Python pipelines
└── ... (entrypoints unchanged)
```

### 3.2 Module Dependency Graph

```
model_ctl.py (lib/)         ← day_cycle.py, night_cycle.py, run_verify_compare.py
    └── lib.db
    └── lib.model_registry

day_cycle.py                ← ExecStart from systemd
    └── lib.model_ctl        (ensure_inference → _ensure_model → _run_model)
    └── lib.db               (pipeline_state queries)
    └── lib.notify           (Slack alert)
    └── pipelines/text_clean.py, entity_scan.py, extract.py, ...

night_cycle.py              ← ExecStart from systemd
    └── lib.model_ctl        (switch_inference → _ensure_model)
    └── lib.db
    └── pipelines/night_cycle.py

auto_mode.py                ← ExecStart from systemd
    └── lib.db
    └── subprocess (Claude Code CLI)

gemini_session.py           ← Manual CLI
    └── lib.auth.key_rotator
    └── subprocess (tmux)

claude_code_runner.py       ← Manual CLI
    └── claude_code_wrapper.py
    └── subprocess (systemctl, curl)
```

### 3.3 Common Patterns (Shared via lib/)

```
lib/pattern.py  (if created, else inline each module)
├── run_cmd(cmd, timeout, check) -> subprocess.CompletedProcess
│   # Wrapper for subprocess.run with logging + retry
├── log_ts() -> str
│   # [2026-07-04T12:00:00Z] format
├── db_query(sql) -> list[dict]
│   # podman exec postgres psql wrapper
└── budget_check(elapsed: int, max: int, label: str) -> bool
    # day_cycle.sh _budget_gate equivalent
```

## 4. Phase Plan

### Phase 0: Library Layer (model_ctl.sh → lib/model_ctl.py)

**Objective**: Replace the shared inference management library first, so all consumers benefit immediately.

**Scope**:
- `lib/model_ctl.sh` (240 lines) → `scripts/lib/model_ctl.py`

**Mapping**:

| bash function | Python equivalent | Notes |
|--------------|-------------------|-------|
| `_model_port` | `model_registry.MODEL_METADATA[key].port` | Direct import |
| `_model_env_vars` | `model_registry.MODEL_METADATA[key] → env dict` | Return dict |
| `_write_mode_env` | Remove (env file deprecated) | Use Python dict in memory |
| `_wait_health` | `requests_available()` or `urllib` → `/health` | Same logic |
| `_wait_probe` | `/v1/chat/completions` with `{max_tokens:5}` | Same body |
| `_check_model_id` | `/v1/models` response comparison | Same |
| `_stop_model` | `podman rm -f -i devforge-inference` | subprocess |
| `_run_model` | `podman run -d ...` + wait + probe | subprocess |
| `_ensure_model` | Smart check → skip if healthy | Same optimization |
| `_test_heartbeat_active` | `lib.db.psql_json` → pulse_id check | Direct import |

**Systemd**: No change (library is imported, not executed directly)

**Risk**: Low — library is self-contained, no ExecStart change required.

---

### Phase 1: Core Orchestrators (day_cycle.sh, night_cycle.sh)

**Objective**: Replace the two main pipeline orchestrators that manage pipeline_state FSM.

#### Phase 1a: day_cycle.sh (429 lines) → scripts/day_cycle.py

**Architecture**:

```
day_cycle.py
├── class DayCycleOrchestrator
│   ├── __init__(self, max_cycle_sec=21600)
│   ├── budget_gate(state, cps, overhead) -> bool
│   ├── slack_alert(title, detail, color)
│   └── ensure_inference(model_key, ...)
├── def main():
│   ├── PID lock (flock → file lock)
│   ├── System sync (gen_architecture, duckdns, watchdog, worklog)
│   ├── In-flight check (pipeline_state != pending, embedded)
│   ├── Text preprocess (text_clean.py)
│   ├── FTS5 refresh
│   ├── Entity scan
│   ├── Day extract
│   ├── Noise marker handling
│   ├── Reranker recovery
│   ├── Day verify
│   ├── Day enrich
│   └── Day embedding
└── if __name__ == '__main__': main()
```

**Systemd change**:
```
# Before
ExecStart=/bin/bash /opt/projects/server/scripts/day_cycle.sh
# After
ExecStart=/usr/bin/python3 /opt/projects/server/scripts/day_cycle.py
```

#### Phase 1b: night_cycle.sh (245 lines) → scripts/night_cycle.py

**Architecture**:

```
night_cycle.py
├── class NightCycleOrchestrator
│   ├── set_mode(mode: str)         # _set_mode
│   ├── wait_for_model(port, label, max_wait)
│   ├── switch_inference(model_key, port)
│   ├── stop_llm_services()
│   └── start_llm_services()
├── def main():
│   ├── PID lock
│   ├── Mode: night
│   ├── Server validation
│   ├── Test heartbeat check
│   ├── Night debate (night_cycle.py --queue)
│   ├── Night verify (review_consumer.py)
│   ├── Day mode restore
│   ├── Daily structure sync
│   ├── Proxy audit (proxy_reviewer.py)
│   ├── FTS5 rebuild (1st only)
│   └── Status YAML → nightly_status.yaml
└── if __name__ == '__main__': main()
```

**Systemd change**:
```
# Before
ExecStart=/opt/projects/server/scripts/night_cycle.sh
# After
ExecStart=/usr/bin/python3 /opt/projects/server/scripts/night_cycle.py
```

---

### Phase 2: Batch Runners & Utilities

#### Phase 2a: auto_mode.sh (271 lines) → scripts/auto_mode.py

**Key challenge**: Markdown parser in awk. Replace with Python `re` + state machine.

```
auto_mode.py
├── class AutoTaskParser:
│   ├── parse(filepath) -> list[AutoTask]
│   └── AutoTask(title, body)
├── class AutoModeRunner:
│   ├── ensure_memory(min_mb=4096)
│   ├── run_task(task) -> (exit_code, output_lines)
│   └── main()
│       ├── Parse auto_tasks.md
│       ├── For each task: ensure_memory → run Claude Code
│       └── Archive + reset
```

#### Phase 2b: run_baseline_monitor.sh (82 lines) → tests/baseline_monitor.py

**Change**: JSON/YAML parsing in bash → native Python dict.

```
baseline_monitor.py
├── def main():
│   ├── Run extract.py --limit 50 --json
│   ├── Parse JSON result (already json, no tail -1 needed)
│   ├── DB query for per-turn stats (psycopg2)
│   └── Write YAML summary (yaml.dump)
```

#### Phase 2c: run_verify_compare.sh (100 lines) → tests/verify_compare.py

**Change**: 40-line inline Python config patch → direct module mutation.

```
verify_compare.py
├── def main():
│   ├── Step 1: Run Qwen Q8 verify (listener loop → async wait)
│   ├── Step 2: Record Qwen results
│   ├── Step 3: Update model_registry for NextCoder Q8
│   ├── Step 4: Restart inference with NextCoder Q8
│   └── Step 5: Run NextCoder verify → comparison report
```

---

### Phase 3: Utility Scripts

#### claude_code_runner.sh (126 lines) → scripts/claude_code_runner.py

**Change**: seq loop + case switching → Python `for` + `argparse`.

```
claude_code_runner.py
├── def run_one(variant, i, outfn, source_secrets, wrapper)
├── def set_proxy_env(variant, proxy_service)
├── def restore_proxy_env(proxy_service)
├── def sleep_between(jitter)
└── def main():
    ├── argparse (runs, output, jitter, source-secrets, ab)
    ├── CSV summary
    ├── --ab: 4 variants
    └── default: single variant
```

**Systemd**: No change (manual CLI)

#### gemini_session_start.sh (99 lines) → scripts/gemini_session.py

**Change**: bash variable export → Python function.

```
gemini_session.py
├── def fetch_key() -> tuple[str, str]  # (key, key_name)
├── def start_session(prompt=None, model="gemini-2.5-flash")
└── if __name__ == '__main__': fire CLI
```

**Systemd**: No change (manual CLI)

#### weekly_enrich_rebuild.sh (56 lines) → scripts/weekly_enrich_rebuild.py

```
weekly_enrich_rebuild.py
├── def quality_check() -> str  # "pass" | "fail" | "skip"
├── def rebuild() -> dict       # {action, slot, total}
└── if __name__ == '__main__': main()
```

**Systemd change**:
```
# Before
ExecStart=/bin/bash /opt/projects/server/scripts/weekly_enrich_rebuild.sh
# After
ExecStart=/usr/bin/python3 /opt/projects/server/scripts/weekly_enrich_rebuild.py
```

#### claude_code_wrapper.sh (85 lines) → scripts/claude_code_wrapper.py

**Key**: Model name remapping + JSON canonicalization + curl.

```
claude_code_wrapper.py
├── MODEL_ALIASES = {"qwen3-30b-a3b-local": "deepseek-v4-flash", ...}
├── def canonicalize(prompt: str) -> str  # strip timestamps/UUIDs
├── def build_payload(model, system, user) -> dict
├── def send(payload, proxy_url, auth_token) -> (int, str)  # HTTP code + body
└── if __name__ == '__main__': argparse CLI
```

**Systemd**: No change (called by claude_code_runner.py)

---

### 3.4 Scripts Remaining in Bash (Unchanged)

| Script | Lines | Reason |
|--------|-------|--------|
| `system_sync.sh` | 41 | Simple sequential: gen_architecture → duckdns → git commit. No Python gain. |
| `open-newhand.sh` | 35 | git commit/push only. One-time operation. |
| `run_pipeline_bg.sh` | 21 | Trivial sequential python calls. |
| `github_mcp_wrapper.sh` | 15 | Secrets sourcing → `exec node`. Requires shell for `grep` on secrets. |
| `fastapi-entrypoint.sh` | 2 | `exec python3 -m uvicorn ...` — PID 1 requirement |
| `worker-entrypoint.sh` | 8 | `exec python3 /scripts/worker_supervisor.py` — PID 1 |
| `mcp_entrypoint.sh` | 3 | `exec python3 /scripts/mcp_server.py` — PID 1 |

## 5. Systemd Unit Changes Summary

| Timer | Service | Before ExecStart | After ExecStart |
|-------|---------|-----------------|-----------------|
| devforge-day-cycle.timer | devforge-day-cycle.service | `/bin/bash day_cycle.sh` | `/usr/bin/python3 day_cycle.py` |
| devforge-night-cycle.timer | devforge-night-cycle.service | `night_cycle.sh` | `/usr/bin/python3 night_cycle.py` |
| devforge-system-sync.timer | devforge-system-sync.service | `/bin/bash system_sync.sh` | **unchanged** |
| devforge-weekly-enrich-rebuild.timer | devforge-weekly-enrich-rebuild.service | `/bin/bash weekly_enrich_rebuild.sh` | `/usr/bin/python3 weekly_enrich_rebuild.py` |
| devforge-auto.timer | devforge-auto.service | `/bin/bash auto_mode.sh` | `/usr/bin/python3 auto_mode.py` |

**All timer definitions remain unchanged.** Only the `.service` file `ExecStart` lines change.

## 6. Risk Assessment

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| day_cycle.py logic bug stops pipeline | Low (Phase 1, tested) | High (no data processing) | Parallel run with old bash for 1 cycle |
| model_ctl.py container management regression | Medium | High (inference down) | `_ensure_model` fallback to bash on failure |
| auto_mode.py Markdown parser diff | Low | Medium | Compare output with awk version on test file |
| subprocess.run() vs `bash -c` behavior diff | Low | Medium | Use `shell=True` for piped commands only |
| systemd ExecStart python path wrong | Very Low | High (service fails) | `daemon-reload` + `status` check in deployment script |

## 7. Testing Strategy

### Per-Script Verification

Every converted script follows this gate:

```bash
# 1. Run both versions in parallel on same input
bash original.sh --dry-run > /tmp/orig.out 2>&1
python3 new_script.py --dry-run > /tmp/new.out 2>&1

# 2. Compare exit codes and output
diff <(tail -5 /tmp/orig.out) <(tail -5 /tmp/new.out)

# 3. Deploy only if match
```

### Integration Test

```bash
# Replace systemd ExecStart → Python version
systemctl --user daemon-reload
systemctl --user start devforge-day-cycle.service
systemctl --user status devforge-day-cycle.service  # verify exit=0
journalctl --user -u devforge-day-cycle.service --since "5 min ago" --no-pager
```

### Rollback Plan

If Python version fails:
```bash
systemctl --user stop devforge-day-cycle.service
# Restore original ExecStart in .service file
systemctl --user daemon-reload
systemctl --user start devforge-day-cycle.service
```

## 8. Migration Timeline (Estimated)

| Phase | Scripts | Lines | Est. Effort | Dependencies |
|-------|---------|-------|-------------|--------------|
| Phase 0 | `lib/model_ctl.sh` | 240 | 2-3h | None |
| Phase 1a | `day_cycle.sh` | 429 | 4-6h | Phase 0 |
| Phase 1b | `night_cycle.sh` | 245 | 3-4h | Phase 0 |
| Phase 2a | `auto_mode.sh` | 271 | 3-4h | None |
| Phase 2b | `run_baseline_monitor.sh` | 82 | 1h | None |
| Phase 2c | `run_verify_compare.sh` | 100 | 1-2h | Phase 0 |
| Phase 3 | `claude_code_runner.sh` | 126 | 2h | None |
| Phase 3 | `claude_code_wrapper.sh` | 85 | 1h | None |
| Phase 3 | `gemini_session_start.sh` | 99 | 1-2h | None |
| Phase 3 | `weekly_enrich_rebuild.sh` | 56 | 0.5h | None |
| **Total** | | **~1,700** | **~20h** | |

## 9. Key Design Decisions

### 9.1 Why Not Typer?
기존 `cli.py`가 argparse 기반이고, systemd ExecStart는 단일 Python 함수 호출이므로 Typer 오버헤드가 불필요. 각 스크립트는 `if __name__ == '__main__'` + `argparse`로 충분. 향후 CLI 통합이 필요하면 그 때 Typer 도입 검토.

### 9.2 Why Not Single CLI.py?
모든 기능을 cli.py에 통합하면 단일 실패점(SPOF)이 되고, systemd ExecStart가 `python3 -m cli day-cycle`처럼 길어짐. 독립 파일 유지가 각 Script의 책임을 명확히 하고 실패 영향 범위를 최소화.

### 9.3 Why stdlib Only?
서버에 추가 패키지 설치 없이 `subprocess`, `argparse`, `pathlib`, `logging`, `json`, `yaml`(lib/yaml.py via pip)로 충분. [plumbum](https://plumbum.readthedocs.io/)은 shell-like Python DSL을 제공하지만 의존성 추가 부담 대비 이점이 미미함. Typer/Click 도입은 Phase 3 이후 검토. **원칙: zero dependency. 모든 시스템 스크립트는 Python 3.11 stdlib만으로 동작해야 함.**

### 9.4 env file → Python dict
MODE 파일(`current-system-mode.env`)은 Python dict로 대체. 단, external system(container entrypoint)이 읽을 수 있어야 하므로:
- Python: `from lib.mode import get_mode, set_mode` (sqlite or in-memory)
- Bash fallback: `cat /opt/ai_data/...mode.env` (hybrid period only)

### 9.5 `sys.exit(main())` 패턴 필수

Bash는 마지막 명령어의 exit code를 자동 반환하지만, Python은 명시하지 않으면 항상 exit 0이다. systemd는 `ExecStart=` 프로세스의 exit code로 성공/실패를 판단하므로 모든 Python 스크립트는 반드시 `sys.exit(main())` 패턴을 따라야 한다.

```python
def main() -> int:
    ...

if __name__ == '__main__':
    sys.exit(main())
```

예외: `Type=oneshot` 서비스에서 `RemainAfterExit=yes`가 설정된 경우. 이 경우에도 exit code 명시를 습관화.

### 9.6 `subprocess.run(timeout=)` 명시

Bash는 systemd `TimeoutSec=`(기본 90s, day_cycle 21600s)에 의해 타임아웃되지만, Python `subprocess.run()`의 기본 `timeout=None`은 **무한 대기**다. systemd가 프로세스를 kill해도 subprocess가 fork한 자식 프로세스는 고아로 남을 수 있다.

**규칙**: 모든 `subprocess.run()` 호출에 `timeout=`을 명시한다.

```python
# Good — timeout 명시
subprocess.run([...], check=True, timeout=30)

# Bad — systemd가 kill해도 subprocess는 무한 대기
subprocess.run([...], check=True)
```

타임아웃 값 기준:
- **일반 명령어**: 30-60s (DB 조회, 파일 처리)
- **LLM 추론**: 300-600s (모델 응답 대기)
- **전체 파이프라인**: systemd `TimeoutSec=` 값 (21600s)

주의: `shell=True`는 사용하지 않는다. 인젝션 위험 + 시그널 전파 문제. 모든 외부 명령어는 `subprocess.run(list_args, ...)` 형태로 호출.

### 9.7 공통 유틸리티 — `lib/pattern.py` (선택)

모든 Python 스크립트가 반복해서 작성하게 될 패턴(서브프로세스 실행, 재시도, 로깅)을 공통 모듈로 분리:

```python
# lib/pattern.py (Phase 0, 선택)
# Status: production
# Path: all system scripts
"""Common patterns for system Python scripts (replaces bash boilerplate)."""

import subprocess
import logging
from collections.abc import Sequence

logger = logging.getLogger(__name__)


def run_cmd(
    args: Sequence[str],
    timeout: float = 30,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    """Run external command with timeout.

    Args:
        args: Command as sequence (NO shell=True).
        timeout: Seconds before subprocess.TimeoutExpired.
        check: Raise CalledProcessError on non-zero exit.
        capture: Capture stdout/stderr (text mode).

    Returns: subprocess.CompletedProcess.
    """
    kwargs = {"check": check, "timeout": timeout}
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    return subprocess.run(args, **kwargs)
```

`run_cmd` 도입 시점: Phase 1a(day_cycle.py)에서 첫 사용. Phase 0에서 미리 작성, 이후 각 Phase에서 점진적으로 도입. CLI 통합(Phase 3 이후) 전까지는 필요에 따라 각 스크립트가 독립 `main()` 함수를 유지.

---

*Document Version: 1.1*
*Author: Claude Code (Deep Dive)*
*Date: 2026-07-04 (v1.1: 2026-07-05)*
