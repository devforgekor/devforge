# DevForge Bash → Python Migration Plan

## Executive Summary

DevForge 서버에는 16개의 bash 스크립트(~1,890줄, lib/model_ctl.sh 포함)가 존재한다. 그러나 **2026-07-22 재평가 결과 핵심 파이프라인 로직(extract, enrich, embed)이 이미 모두 Python으로 전환**되었으며, 남은 bash는 주로 오케스트레이션(day_cycle.sh)과 라이브러리(model_ctl.sh)다.

가장 중요한 발견: **extract.py가 self-sufficient pipeline으로 진화**하여 preflight gate, reranker launch, NLI verify(v1+v2), quality check를 모두 내부 처리한다. 이로 인해 day_cycle.sh의 복잡성이 크게 감소했으며, bash→Python 마이그레이션의 긴급도와 ROI가 낮아졌다.

현재 권장 전략:
- **Phase 0 (권장)**: `model_ctl.sh` → Python (1-2h) — 유일한 bash 라이브러리
- **Phase 1 (선택)**: `day_cycle.sh` → Python (2-3h, ROI 낮음, 보류 권장)
- **그 외**: 전환 불필요 (entrypoints, system_sync, open-newhand, run_pipeline_bg)
- **위험 관리**: `subprocess timeout` 누락이 가장 큰 실질 리스크. bash `timeout` 명령어로 최소 패치 가능

**접근법**: 모듈별 1:1 Python 파일 교체 (점진적 전환, 각 파일 독립 교체 가능, systemd ExecStart만 변경)

## 1. Current State Analysis

### 1.1 Active Bash Scripts (16 files, ~1,890 lines)

| # | Script | Lines | Category | Priority | Key Complexity | Migrate? |
|---|--------|-------|----------|----------|----------------|----------|
| 1 | day_cycle.sh | 462 | Orchestrator | **P0** | 19 DB queries, 14 python3 subprocess, 3 curl, 5 funcs | **선택** (ROI 낮음) |
| 2 | night_cycle.sh | 245 | Orchestrator | **P0** | 3 py3c inline, 6 funcs, mode 전환, trap | **아니오** (night_cycle.py가 내부 처리) |
| 3 | auto_mode.sh | 271 | Batch Runner | **P0** | awk Markdown parser, Claude Code runner | **아니오** (저빈도) |
| 4 | lib/model_ctl.sh | 240 | Library | **P0** | 10 funcs, podman 생애주기, health probe | **예** (1순위) |
| 5 | claude_code_runner.sh | 126 | Utility | **P1** | A/B 테스트 루프 | **아니오** |
| 6 | run_verify_compare.sh | 100 | Test | **P1** | 40L python3 -c inline patch | **아니오** |
| 7 | gemini_session_start.sh | 99 | Utility | **P1** | Key rotation + tmux | **아니오** |
| 8 | run_baseline_monitor.sh | 82 | Test | **P1** | 4회 py3c inline, podman exec DB | **아니오** |
| 9 | claude_code_wrapper.sh | 85 | Wrapper | **P1** | JSON canonicalization + curl | **아니오** |
| 10 | weekly_enrich_rebuild.sh | 56 | Timer Batch | **P2** | 2회 py3c inline (lib.enrich_few_shot) | **아니오** |
| 11 | system_sync.sh | 41 | Timer Batch | **Unchanged** | curl duckdns, git commit | **제외** |
| 12 | open-newhand.sh | 35 | Utility | **Unchanged** | git add/commit/push | **제외** |
| 13 | run_pipeline_bg.sh | 21 | Script | **Unchanged** | 단순 순차 | **제외** |
| 14 | github_mcp_wrapper.sh | 15 | Wrapper | **Unchanged** | Secrets → exec | **제외** |
| 15 | worker-entrypoint.sh | 8 | Container | **Unchanged** | PID 1 | **제외** |
| 16 | fastapi-entrypoint.sh | 2 | Container | **Unchanged** | PID 1 | **제외** |
| 17 | mcp_entrypoint.sh | 3 | Container | **Unchanged** | PID 1 | **제외** |

### 1.2 Key Finding: Pipeline Scripts Are All Python

**모든 pipeline 스크립트(scripts/pipelines/*.py, 28개)는 이미 Python이다.** 남은 bash는 오케스트레이션(day_cycle.sh, night_cycle.sh)과 라이브러리(model_ctl.sh)뿐이다.

#### extract.py Self-Sufficiency (Critical)

extract.py가 K8s startupProbe 스타일의 `_preflight_gate()`를 도입하면서 day_cycle.sh의 책임이 근본적으로 축소되었다:

| 기능 | 이전 (v1.2) | 현재 (v2.0) |
|------|-----------|-----------|
| Model file 존재 확인 | day_cycle.sh 없음 | extract.py _preflight_gate() |
| Memory budget 확인 | day_cycle.sh _budget_gate() | extract.py check_memory_budget |
| Reranker launch (:8080) | day_cycle.sh _launch_reranker() | extract.py _launch_reranker() |
| Model pod start (day-extractor) | day_cycle.sh ensure_inference | extract.py _ensure_model_pod() |
| NLI verify (extracted→verified) | day_cycle.sh → day_verify.py 호출 | extract.py Phase 2-3 (_llm_nli_verify, _llm_nli_verify2) |
| Fact quality check | 없음 | extract.py _quality_check_facts |
| 8082 auto-recovery | 없음 | extract_llm.py _call_with_8082_retry |

#### 이미 Python화된 Pipeline 모듈들

| 모듈 | Status | main() 흐름 | Bash 의존성 |
|------|--------|-----------|-----------|
| extract.py | production | _preflight_gate() → preflight_checks() → _ensure_model_pod() → extract_pipeline() | 없음 |
| enrich.py | experimental | preflight_checks() → ensure_sequential_dual() → ThreadPool dual A/B | 없음 |
| embed_batch.py | production | orphan cleanup → preflight_checks() → ensure_model() → dynamic batching | 없음 |
| text_clean.py | production | language detection → cleaning → hanja → LLM verify → store | 없음 |
| fts5_refresh.py | production | stale turn query → FTS5Index.sync() | 없음 |
| entity_scan.py | experimental | pattern-based extraction (no LLM) | 없음 |
| reranker_recover.py | production | reranker health → re-score RERANKER_ERROR facts | 없음 |
| post_extract_supplement.py | experimental | offline LLM for missing facts | 없음 |
| raw_consumer.py | experimental | polls raw → clean → pending (turn_watcher chain) | 없음 |

#### Dead Code 발견

| 파일 | 상태 | 발견 내용 |
|------|------|----------|
| `day_verify.py` | experimental | **day_cycle.sh에서 더 이상 호출하지 않음**. extract.py가 verify 통합. `_launch_reranker()` 3번째 복사본 존재 |
| `polish_batch.py` | deprecated | text_clean.py에 병합 완료 |
| `_launch_reranker()` 중복 | - | day_cycle.sh(140-166), extract.py(1050-1081), day_verify.py(~551) — 3개 복사본 |

### 1.3 Pattern Analysis

**4 anti-patterns identified (all reduced vs v1.2):**

1. **python3 -c inline** (~16회): v1.2 대비 변동 없음. run_baseline_monitor.sh(4회)가 가장 밀도 높음
2. **bash FSM** (day_cycle.sh pipeline_state): 6-state → extract.py가 verify/preflight 처리로 사실상 3-state
3. **env file state sharing** (MODE=night): night_cycle.sh만 사용, 나머지는 Python dict
4. **Markdown parser in awk** (auto_mode.sh): 여전히 awk, 저빈도로 전환 불필요

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
├── cli.py                      # [EXISTING] Keep argparse as-is
├── day_cycle.py                 # [OPTIONAL] day_cycle.sh replacement (Phase 1, deferred)
├── lib/
│   ├── model_ctl.py             # [NEW] model_ctl.sh replacement (Phase 0, recommended)
│   ├── db.py                    # [EXISTING] psql wrapper
│   └── notify.py                # [EXISTING] Slack/Telegram
├── pipelines/
│   └── ...                      # [EXISTING] All Python, no migration needed
└── ... (bash scripts unchanged)
```

### 3.2 Module Dependency Graph

```
model_ctl.py (lib/)             ← day_cycle.sh (bash), night_cycle.sh (bash)
    └── lib.db
    └── lib.model_registry

day_cycle.py [OPTIONAL]         ← ExecStart from systemd (or watchdog trigger)
    └── lib.model_ctl           (ensure_inference)
    └── lib.db                  (pipeline_state queries)
    └── lib.notify              (Slack alert)
    └── pipelines/*.py          (all already Python)
```

### 3.3 Common Patterns (unchanged from v1.2)

```
lib/pattern.py  (if created)
├── run_cmd(cmd, timeout, check) -> subprocess.CompletedProcess
├── log_ts() -> str
├── db_query(sql) -> list[dict]
└── budget_check(elapsed, max, label) -> bool
```

## 4. Phase Plan (Revised)

**v2.0 변경**: v1.2의 Phase 1-3 (8개 스크립트 전환) → Phase 0 (model_ctl.sh) + Phase 1 선택 (day_cycle.sh 보류). extract.py self-sufficiency로 인해 나머지 스크립트 전환 불필요.

### Phase 0: Library Layer (model_ctl.sh → lib/model_ctl.py)

**Objective**: 유일한 bash 라이브러리를 Python으로 전환. 모든 pipeline 스크립트가 subprocess.run("podman ...") 대신 `lib.model_ctl`을 import 가능.

**Scope**:
- `lib/model_ctl.sh` (240 lines) → `scripts/lib/model_ctl.py`

**Mapping**:

| bash function | Python equivalent | Notes |
|--------------|-------------------|-------|
| `_model_port` | `model_registry.MODEL_METADATA[key].port` | Direct import |
| `_model_env_vars` | `model_registry.MODEL_METADATA[key] → env dict` | Return dict |
| `_write_mode_env` | Remove (env file deprecated) | Python in-memory |
| `_wait_health` | `urllib` → `/health` | Same logic |
| `_wait_probe` | `/v1/chat/completions` with `{max_tokens:5}` | Same |
| `_check_model_id` | `/v1/models` response comparison | Same |
| `_stop_model` | `podman rm -f -i devforge-inference` | subprocess |
| `_run_model` | `podman run -d ...` + wait + probe | subprocess |
| `_ensure_model` | Smart skip-if-healthy check | Same optimization |
| `_test_heartbeat_active` | `lib.db.psql_json` → pulse_id check | Direct import |
| `_kill_model` | `podman kill` with timeout | subprocess |

**Systemd**: No change (library, not executed directly)

**Risk**: Low. model_ctl.sh는 self-contained. 10개 함수 모두 podman wrapper. 기존 Python lib(db.py, model_registry.py) 활용 가능.

**Est. Effort**: 1-2h (python3 -c inline 0회, DB query 0회 — 가장 단순한 코드)

---

### Phase 1 (선택, 보류): day_cycle.sh (462 lines) → scripts/day_cycle.py

**권장사항: 보류.** extract.py가 verify/reranker/preflight를 모두 처리하므로 day_cycle.sh의 복잡성이 급감. 남은 bash 로직:

```
python3 pipelines/text_clean.py     # 이미 Python
python3 pipelines/fts5_refresh.py   # 이미 Python
python3 pipelines/entity_scan.py    # 이미 Python
python3 pipelines/extract.py        # 이미 Python (self-sufficient)
python3 pipelines/reranker_recover.py  # 이미 Python
python3 pipelines/post_extract_supplement.py  # 이미 Python
python3 pipelines/enrich.py         # 이미 Python
python3 pipelines/embed_batch.py    # 이미 Python
```

→ 14회 python3 subprocess 호출 + 19회 DB query. 전환 시 얻는 이점:
- DB query 통일 (psql_json으로 raw SQL 대체)
- subprocess timeout 명시 (현재 없음)
- watchdog → Python 직접 import (선택)

**Architecture** (전환 시):

```
day_cycle.py
├── class DayCycleOrchestrator
│   ├── budget_gate(state, cps, overhead) -> bool
│   ├── slack_alert(title, detail, color)
│   └── ensure_inference(model_key, ...)
├── def main():
│   ├── PID lock (flock → file lock)
│   ├── System sync (watchdog trigger, worklog)
│   ├── In-flight check
│   ├── Text preprocess → FTS5 → Entity scan
│   ├── Day extract → Noise marker → NEUTRAL gate
│   ├── Reranker recovery → Post-extract supplement
│   ├── Day enrich
│   └── Day embedding
└── if __name__ == '__main__': sys.exit(main())
```

**Systemd**: ExecStart 변경 필요.
```
Before: ExecStart=/bin/bash /opt/projects/server/scripts/day_cycle.sh
After:  ExecStart=/usr/bin/python3 /opt/projects/server/scripts/day_cycle.py
```

**Est. Effort**: 2-3h (v1.2 대비 4-6h → 2-3h, extract.py self-sufficiency로 인한 감소)

---

### Phase 2-3: 전환 불필요

나머지 12개 스크립트는 전환하지 않음:

| Script | Reason |
|--------|--------|
| `night_cycle.sh` | night_cycle.py가 P-R-J pipeline 처리. bash는 mode 전환/trap 오케스트레이션만. 전환 이점 미미 |
| `auto_mode.sh` | 저빈도 (crono 상태). awk 파서 전환 비용 대비 이익 없음 |
| `claude_code_runner.sh` | 수동 CLI, 저빈도 |
| `gemini_session_start.sh` | 수동 CLI, 저빈도 |
| `run_baseline_monitor.sh` | 테스트 도구, py3c 4회 밀도 높지만 single-use |
| `run_verify_compare.sh` | 일회성 테스트 도구 |
| `claude_code_wrapper.sh` | runner가 호출, 간접 영향 |
| `weekly_enrich_rebuild.sh` | 56L, timer 기반 간단 |
| `system_sync.sh` | 41L, 제외 대상 |
| `open-newhand.sh` | 35L, 제외 대상 |
| `run_pipeline_bg.sh` | 21L, 제외 대상 |
| entrypoints (3개) | PID 1, 변경 불가 |

## 5. Systemd Unit Changes Summary

| Timer | Service | Before ExecStart | After ExecStart |
|-------|---------|-----------------|-----------------|
| devforge-day-cycle.timer *(removed)* | devforge-day-cycle.service | `/bin/bash day_cycle.sh` | **unchanged** (Phase 1 보류, 선택적) |
| devforge-night-cycle.timer | devforge-night-cycle.service | `night_cycle.sh` | **unchanged** |
| devforge-system-sync.timer | devforge-system-sync.service | `/bin/bash system_sync.sh` | **unchanged** |
| devforge-weekly-enrich-rebuild.timer | devforge-weekly-enrich-rebuild.service | `/bin/bash weekly_enrich_rebuild.sh` | **unchanged** |
| devforge-auto.timer | devforge-auto.service | `/bin/bash auto_mode.sh` | **unchanged** |

**변경 사항**: v1.2 대비 ExecStart 변경 계획이 모두 제거됨. Phase 1(day_cycle.py) 전환 시에만 devforge-day-cycle.service의 ExecStart 변경.

> Note: devforge-day-cycle.timer는 이미 제거됨. watchdog Python이 systemctl start로 서비스를 직접 트리거.

## 6. Risk Assessment

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| day_cycle.py logic bug stops pipeline | Low (Phase 1 선택, 테스트 필요) | Medium | extract.py가 self-sufficient이므로 day_cycle 실패 시 pipeline만 중단, watchdog이 재시도 |
| model_ctl.py container management regression | Medium | High (inference down) | _ensure_model fallback to bash on failure |
| subprocess.run() vs bash behavior diff | Low | Medium | Use shell=False for all commands |
| systemd ExecStart python path wrong | Very Low | High (service fails) | daemon-reload + status check in deploy |

**v2.0 주요 리스크 감소**: v1.2 대비 전환할 스크립트가 4개에서 1-2개로 줄어, 전환 리스크가 60% 이상 감소.

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

| Phase | Scripts | Lines | Est. Effort | Dependencies | Priority |
|-------|---------|-------|-------------|--------------|----------|
| Phase 0 | `lib/model_ctl.sh` | 240 | **1-2h** | None | **권장** |
| Phase 1 (선택) | `day_cycle.sh` | 462 | **2-3h** | Phase 0 | 보류 (ROI 낮음) |
| **Total (최소)** | | **240** | **1-2h** | | **model_ctl only** |
| **Total (최대)** | | **702** | **3-5h** | | day_cycle 포함 |

**v1.2 대비 75% 감소** (20.5h → 1-5h). extract.py self-sufficiency가 전환 필요성을 근본적으로 낮춤.

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

*Document Version: 2.0*
*Author: Claude Code (Deep Dive)*
*Date: 2026-07-22*

## Document History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-07-04 | Initial design |
| 1.1 | 2026-07-05 | Web validation findings: sys.exit(main()), subprocess.run(timeout=), plumbum rationale, lib/pattern.py |
| 1.2 | 2026-07-22 | Sync with live code changes: pipeline_state 7→6, day_verify.py removed, _launch_reranker() added |
| **2.0** | **2026-07-22** | **Complete re-evaluation: extract.py self-sufficiency → scope/priority restructured. Phase 0 (model_ctl)만 권장. Phase 1 (day_cycle) 선택적. Phase 2-3 전환 불필요. 예상 공수 20.5h→1-5h.** |

### Changed Since v1.2

| Area | v1.2 | v2.0 |
|------|------|------|
| Executive Summary | 16개 스크립트 전환 | model_ctl.sh만 권장, ROI 낮음 |
| Scope | 11개 전환 대상 | Phase 1 (선택) + Phase 0 = 1-2개 |
| Pipeline 상태 | 모든 bash 스크립트 분석 | **모든 pipeline script는 이미 Python** 발견 |
| extract.py | day_cycle.sh가 호출하는 단순 pipeline | self-sufficient (preflight/reranker/verify 내재화) |
| Phase Plan | Phase 0-3 (8개 sub-phase) | Phase 0 (model_ctl) + Phase 1 선택 (day_cycle 보류) |
| 예상 공수 | ~20.5h | 1-2h (model_ctl only) ~ 3-5h (day_cycle 포함) |
| Dead code | 없음 | day_verify.py (호출 제거됨), polish_batch.py (deprecated), _launch_reranker 3중복 |
| Systemd 변경 | 4개 서비스 ExecStart 변경 | ExecStart 변경 불필요 (전환 보류) |
