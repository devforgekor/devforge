# Deep Dive — Watchdog 통합 감지·복구 설계

> 산출: 2026-09-08 14:00 KST
> 방식: Deep Dive (코드 분석 → context7 검증 → 설계)
> 상태: 설계 완료

---

## 0. 현재 시스템 구조 (완전 파악)

### 3계층 아키텍처

```
┌─────────────────────────────────────────────────────────────────────┐
│  ① 와치독 (devforge-watchdog, 60s polling)                        │
│                                                                     │
│  ├─ check_pipeline("day_cycle.sh")   ← 프로세스 생존               │
│  │   └─ 죽음 + in-flight>0 → systemctl start (재개)                │
│  │   └─ 죽음 + pending>0 → systemctl start (신규)                  │
│  ├─ check_pipeline_stuck()           ← pipeline_state 3600s 갱신 X │
│  ├─ _recover_intermediate_states()   ← extracting→scanned (30min)  │
│  │                                      enriching→verified (30min) │
│  ├─ LLM_PROBE(8082)                  ← T1 health + T2 probe       │
│  ├─ SERVICE_TARGETS                  ← turn-watcher, rr-proxy      │
│  └─ day_fix_loop()                   ← LLM 코드 수정 (인프라 X)    │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  ② day_cycle (devforge-day-cycle.service, oneshot, 6h budget)     │
│                                                                     │
│  day_cycle.sh → pipeline_state 순차 처리:                           │
│    text_clean → entity_scan → extract(:8082) → supplement          │
│    → enrich(:8082) → embed(:8081)                                  │
│                                                                     │
│  각 phase에서 ensure_inference → _ensure_model:                    │
│    kill_all() → podman stop → podman start → health check          │
│    └─ 실패 시 "continuing anyway" 경고만, 스크립트는 계속           │
│    └─ (enrich.py만 예외: ensure_model 재시도 루프)                  │
│                                                                     │
│  자체 방어: Slack alert, liveness check, budget gate, PID lock     │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  ③ lib/model_ctl.py → lib/pod_manager/                            │
│                                                                     │
│  ensure_model() → start_inference(mode, port, model_key)           │
│    ├─ kill_all() → _podman_stop_inference()                        │
│    ├─ _podman_start_inference() ← 8080-8084 publish                │
│    └─ health check (timeout=600s or 1200s)                         │
│         └─ "bind: address already in use" → report error → retry   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 1. 갭 분석 (Gap Analysis)

### 갭 #1: 포트 충돌 감지 없음

| 항목 | 내용 |
|------|------|
| **증상** | `podman run` 실패 → "bind: address already in use" |
| **현재 감지** | ❌ 없음 (ensure_model의 예외는 `log()`만, watchdog이 읽지 않음) |
| **영향** | enrich.py 무한 재시도 루프, day_cycle.sh는 살아있어서 watchdog이 "running" 판정 |
| **시정** | journalctl에서 "address already in use" 패턴 감지 필요 |

### 갭 #2: model switch 실패 감지 없음

| 항목 | 내용 |
|------|------|
| **증상** | `ensure_model()`이 실패하고 재시도 중, inference 컨테이너가 "Created" 상태 |
| **현재 감지** | ❌ 없음 (watchdog은 `check_pipeline()`만 확인) |
| **영향** | 같은 포트 충돌, day_cycle.sh는 살아있지만 실제 작업은 0 |
| **시정** | `check_all_llm()`이 8082 probe에서 실패 → 진단 필요 |

### 갭 #3: pipeline_stuck이 이벤트만 기록, 복구 안 함

| 항목 | 내용 |
|------|------|
| **증상** | `check_pipeline_stuck()`이 stuck 상태를 감지하지만 `add_event()`만 함 |
| **현재 복구** | ❌ `_recover_intermediate_states()`는 state만 리셋, 근본 원인(포트/모델)은 해결 안 함 |
| **영향** | 3600s 후 Slack에 이벤트 기록만, 자동 복구 X |
| **시정** | stuck 감지 시 → inference 재시작 → model 재시도 순서로 복구 |

### 갭 #4: day_fix_loop()가 인프라 문제를 LLM 코드 수정하려 함

| 항목 | 내용 |
|------|------|
| **증상** | `day_fix_loop()` → `_fix_loop_common()` → `run_fix_loop()` (LLM 코드 수정) |
| **문제** | 포트 충돌은 코드 문제가 아니라 인프라 문제 → LLM 코드 수정이 무의미 |
| **시정** | 인프라 문제(포트/모델/컨테이너)를 먼저 진단하고, 그 다음에만 LLM 코드 수정 |

---

## 2. 설계: Watchdog 포트 충돌 감지·복구

### 2.1 신규: 포트 충돌 감지 (checker.py)

```python
def check_port_conflict(service: str = "devforge-day-cycle", since_min: int = 15) -> tuple[bool, str]:
    """Check recent day_cycle journal for port conflict patterns.

    Returns:
        (True, "ok") or (False, "port conflict detected: <detail>")
    """
    patterns = [
        "bind: address already in use",
        "address already in use",
        "rootlessport listen",
    ]
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", service,
             "--since", f"{since_min} min ago", "--no-pager"],
            capture_output=True, text=True, timeout=10,
        )
        combined = r.stdout + r.stderr
        for pat in patterns:
            if pat in combined:
                # Extract context: which port
                for line in combined.split("\n"):
                    if pat in line:
                        return False, f"port conflict: {line.strip()[-120:]}"
        return True, "no port conflict"
    except Exception as e:
        return True, f"check failed: {e}"
```

### 2.2 신규: inference 컨테이너 상태 진단 (checker.py)

```python
def check_inference_container() -> tuple[bool, str]:
    """Check if inference container is running vs stuck (Created state).

    Created state means podman couldn't start the container (e.g. port conflict).
    """
    try:
        r = subprocess.run(
            ["podman", "ps", "-a", "--format",
             "{{.Names}}|{{.Status}}", "--filter", "name=devforge-inference"],
            capture_output=True, text=True, timeout=10,
        )
        for line in r.stdout.strip().split("\n"):
            if "devforge-inference" in line and "|" in line:
                parts = line.split("|")
                name, status = parts[0], parts[1]
                if "Up" in status:
                    return True, status
                elif "Created" in status:
                    return False, f"container stuck in Created state"
                elif "Exited" in status:
                    return False, f"container exited: {status}"
        return False, "container not found"
    except Exception as e:
        return False, f"check failed: {e}"
```

### 2.3 신규: 포트 충돌 복구 (recovery.py)

```python
def recover_port_conflict() -> bool:
    """Free port 8080 and restart inference container.

    Called when port conflict is detected. Cleans up:
    1. llama-server host process on :8080
    2. stale inference container
    3. pod-a network (if 8080 published)
    4. Restarts inference container
    """
    log("  [port-conflict] recovering...")
    try:
        # 1. Kill host reranker if any
        subprocess.run(["pkill", "-f", "llama-server.*--port 8080"],
                       capture_output=True, timeout=10)
        # 2. Remove stale inference container
        subprocess.run(["podman", "rm", "-f", "devforge-inference"],
                       capture_output=True, timeout=30)
        # 3. Kill rootlessport for 8080 if leaked
        #    (rootlessport is managed by podman, rm container + wait should free it)
        time.sleep(3)
        # 4. Start fresh inference
        from lib.pod_manager.container import _podman_start_inference
        ok = _podman_start_inference()
        if ok:
            log("  [port-conflict] inference container started successfully")
            return True
        log("  [port-conflict] inference still failed after cleanup")
        return False
    except Exception as e:
        log(f"  [port-conflict] recovery error: {e}")
        return False
```

### 2.4 신규: inference health cascade (recovery.py)

```python
def recover_inference_cascade() -> bool:
    """Cascading inference recovery: kill → restart → health check.

    Graduated recovery sequence:
    Level 1: Kill inference container, restart
    Level 2: Kill llama-server host processes, rm container, restart
    Level 3: Full pod-a stop, rm container, restart
    """
    for level in range(1, 4):
        log(f"  [inference-cascade] level {level}")
        try:
            if level >= 1:
                _, _ = _podman_stop_inference(), time.sleep(2)
            if level >= 2:
                subprocess.run(["pkill", "-f", "llama-server"],
                               capture_output=True, timeout=10)
                subprocess.run(["podman", "rm", "-f", "devforge-inference"],
                               capture_output=True, timeout=30)
                time.sleep(3)
            if level >= 3:
                subprocess.run(["podman", "pod", "stop", "devforge-pod-a"],
                               capture_output=True, timeout=30)
                time.sleep(2)

            ok = _podman_start_inference()
            if ok:
                log(f"  [inference-cascade] recovered at level {level}")
                return True
        except Exception as e:
            log(f"  [inference-cascade] level {level} error: {e}")
    return False
```

---

## 3. 통합: day_fix_loop() 재설계

### 3.1 현재 (문제)

```python
def day_fix_loop():
    for pipe in ("day_cycle",):
        _fix_loop_common(pipe, llm_port=8082)  # LLM 코드 수정만
```

### 3.2 변경 (watchdog orchestrator.py)

```python
def day_fix_loop():
    if _test_active:
        return

    # ── Phase 1: 인프라 진단 ──
    # 1a. 포트 충돌 감지
    port_ok, port_detail = check_port_conflict()
    if not port_ok:
        log(f"  [watchdog] {port_detail}")
        _state.add_event("infra", "port_conflict", port_detail)
        if recover_port_conflict():
            _state.add_event("infra", "port_conflict_recovered", "inference restarted")
            send_recovery("infra:port_conflict", "inference restarted after port conflict")
            return
        _state.add_event("infra", "port_conflict_failed", "all recovery levels failed")
        send_alert("infra:port_conflict", "DOWN", "cascade recovery failed")
        return

    # 1b. inference 컨테이너 상태 진단
    infer_ok, infer_detail = check_inference_container()
    if not infer_ok:
        log(f"  [watchdog] inference container issue: {infer_detail}")
        _state.add_event("infra", "inference_down", infer_detail)
        if recover_inference_cascade():
            _state.add_event("infra", "inference_recovered", "cascade OK")
            send_recovery("infra:inference", "inference container restarted")
            return
        send_alert("infra:inference", "DOWN", "cascade recovery failed")
        return

    # 1c. LLM probe (8082) — day-extractor가 정상 응답하는지
    probe_ok, probe_detail = check_llm_probe(8082, "day-extract")
    if not probe_ok:
        log(f"  [watchdog] LLM probe 8082 failed: {probe_detail}")
        _state.add_event("infra", "llm_probe_failed", probe_detail)
        recover_inference_cascade()
        return

    # ── Phase 2: pipeline state stuck 진단 ──
    stuck = _state.check_pipeline_stuck()
    for s in stuck:
        log(f"  [watchdog] pipeline stuck: {s['state']} ({s['cnt']} turns, {s['stuck_sec']}s)")
        _state.add_event("pipeline_stuck", s["state"],
                         f"{s['cnt']} turns, {s['stuck_sec']}s")
        # Stacked: model OK but pipeline not progressing → restart day_cycle
        log(f"  [watchdog] restarting day_cycle to unstick pipeline")
        subprocess.run(
            ["systemctl", "--user", "restart", "devforge-day-cycle.service"],
            capture_output=True, timeout=30,
        )

    # ── Phase 3: 기존 LLM 코드 fix loop (인프라 문제가 없을 때만) ──
    if not stuck:
        for pipe in ("day_cycle",):
            _fix_loop_common(pipe, llm_port=8082)
```

---

## 4. 체크 순서도

```
day_fix_loop() 진입
│
├─ Phase 1: 인프라 진단
│  ├─ 1a. 포트 충돌? → recover_port_conflict() → return
│  ├─ 1b. inference Created/Exited? → recover_inference_cascade() → return
│  └─ 1c. LLM probe 8082 실패? → recover_inference_cascade() → return
│
├─ Phase 2: pipeline stuck 진단
│  └─ pipeline_state 3600s 갱신 X → restart day_cycle → return
│
└─ Phase 3: LLM 코드 fix (기존)
   └─ _fix_loop_common("day_cycle", llm_port=8082)
```

---

## 5. 필요한 파일 변경

| 파일 | 변경 | 유형 |
|------|------|------|
| `lib/watchdog/checker.py` | `check_port_conflict()` + `check_inference_container()` 추가 | 신규 함수 |
| `lib/watchdog/recovery.py` | `recover_port_conflict()` + `recover_inference_cascade()` 추가 | 신규 함수 |
| `lib/watchdog/orchestrator.py` | `day_fix_loop()` 재설계 (Phase 1→2→3) | 수정 |
| `lib/watchdog/__init__.py` | 신규 함수 export | 수정 |
| `lib/watchdog/config.py` | 변경 없음 (기존 LLM_TARGETS, DAY_PORTS 활용) | — |

---

## 6. context7 검증 요약

| 검증 항목 | 출처 | 결론 |
|-----------|------|-------|
| rootless port publish conflict | docs.podman.io | 기존 publish와 충돌 → "bind: address already in use" |
| 컨테이너 exec 내 프로세스 관리 | podman REST API | `podman exec`으로 기존 컨테이너 안에서 프로세스 제어 가능 |
| systemd Restart 정책 | systemd docs | `Restart=on-failure` + `RestartSec=` 표준 패턴 |
| inference 컨테이너 포트 범위 | day_cycle.sh | 8080-8084 전부 publish → 8080 충돌 시 전부 불가 |

---

## 7. 적용 후 기대 효과

| 시나리오 | 현재 | 변경 후 |
|----------|------|---------|
| reranker 호스트 프로세스가 8080 점유 | enrich.py 무한 재시도 루프, 사람이 직접 kill | watchdog이 60s 내 감지 → `recover_port_conflict()`로 자동 해결 |
| inference 컨테이너 "Created" 스턱 | day_cycle.sh는 살아있지만 작업 0, 알림 없음 | watchdog이 `check_inference_container()`로 Created 감지 → cascade 복구 |
| pipeline_state 3600s 갱신 없음 | Slack 이벤트만 기록, 복구 없음 | day_cycle 재시작으로 복구 시도 |
| 8082 probe 실패 | T1/T2만 기록, 조치 없음 | inference cascade 복구 트리거 |