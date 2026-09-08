# Watchdog 포트 충돌 감지 로직 분석 보고서

> 작성일: 2026-09-08
> 작성자: Claude Code
> 대상: `lib/watchdog/` — orchestrator.py, recovery.py, fixloop.py, state.py, checker.py, config.py

## 1. 문제 상황

### 발견된 장애

```
day_cycle.sh → enrich.py → ensure_model("day-enricher")
  → kill_all() → _podman_stop_inference()     # inference container STOP
  → _podman_start_inference()                  # 새 inference container START
  → rootlessport listen tcp 127.0.0.1:8080:    # ❌ bind: address already in use
  → podman start FAILED — :8082 will not be available
  → ensure_model(day-enricher) failed — retrying after GC + 10s
  → ... (무한 재시도 루프)
```

### 근본 원인

1. **reranker(8080)가 inference 컨테이너 밖에서 별도 프로세스로 실행 중**
2. `kill_all()` → `_podman_stop_inference()`는 inference 컨테이너만 중지하고, reranker 프로세스는 건드리지 않음
3. 새 inference 컨테이너가 8080~8084 전부를 바인딩하려다 8080이 이미 사용 중이어서 실패
4. `ensure_model()`이 10초마다 재시도하는 무한 루프에 빠짐

---

## 2. 와치독 현재 감시 체계 분석

### 2.1 체커 계층

| 계층 | 함수 | 대상 | 감지 가능? |
|------|------|------|-----------|
| T1 | `check_health(port)` | HTTP GET /health | ✅ 포트 응답 확인 |
| T2 | `check_llm_probe(port)` | POST /v1/chat max_tokens=1 | ✅ 실제 추론 확인 |
| T3 | `check_probe_latency(port)` | 5분 주기 latency 분석 | ✅ hang 감지 |
| Service | `check_service(name)` | systemctl is-active | ✅ 서비스 다운 |
| Timer | `check_timer(name)` | systemctl show LastTriggerUSec | ✅ 타이머 미발동 |
| Memory | `check_memory()` | free -m | ✅ OOM 감지 |
| Pipeline | `check_pipeline_stuck()` | DB pipeline_state 분포 | ✅ state stagnation |
| Slots | `check_llm_slots(port)` | GET /slots | ✅ slot deadlock |
| Token | `check_token_stagnation()` | /metrics aggregate | ✅ token stagnation |

### 2.2 복구 계층

| 복구 함수 | 대상 | 방식 |
|-----------|------|------|
| `recover_service(name)` | systemd 서비스 | systemctl restart |
| `recover_container(name)` | inference 컨테이너 | podman stop + start |
| `recover_oom()` | OOM 전체 | kill_all + mode restore |
| `recover_slot_deadlock(port)` | slot deadlock | podman restart |
| `graduated_recover()` | 모든 서비스 | CrashLoopBackOff + circuit breaker |
| `_fix_loop_common()` | 파이프라인 | LLM 코드 수정 + sandbox verify |
| `_recover_intermediate_states()` | pipeline state stuck | SQL UPDATE reset |

---

## 3. 포트 충돌 관련 와치독의 취약점 (Gap 분석)

### Gap #1: 포트 충돌 감지 로직이 전혀 없음

```python
# checker.py — 현재 없음
# 필요: journalctl 또는 podman 로그에서 "address already in use" 감지
```

- `check_health(port)`는 HTTP 응답을 확인하는데, 이 경우 inference 컨테이너가 아예 뜨지 않아서 **connection refused**가 발생
- "connection refused"와 "address already in use"는 **원인이 완전히 다름**
- 전자는 일시적(컨테이너 시작 중), 후자는 **포트 충돌로 복구 방식이 다름**
- 현재 checker는 둘을 구분하지 않고 모두 `str(e)`로 처리

### Gap #2: reranker 프로세스가 와치독 관리 대상이 아님

```python
# config.py — SERVICE_TARGETS
SERVICE_TARGETS = [
    "devforge-turn-watcher",
    "openrouter-rr-proxy",
]
```

- reranker는 `container-devforge-pod-a` 서비스 안에서 실행됨
- `container-devforge-pod-a`는 `ALERT_ONLY_TARGETS`에도 없음
- reranker가 죽거나 포트를 잡고 있어도 와치독이 알 수 없음

### Gap #3: `kill_all()`이 reranker를 고려하지 않음

```python
# pod_manager/__init__.py
def kill_all():
    _podman_stop_inference()  # inference 컨테이너만 중지
    _reclaim_memory()
```

- reranker(8080)는 inference 컨테이너 밖(Pod A)에서 실행 중
- `kill_all()`이 inference 컨테이너만 중지하고 reranker는 그대로 둠
- 새 inference 컨테이너가 8080 바인딩 시도 → 실패

### Gap #4: `day_fix_loop()`가 LLM 코드 수정에만 집중

```python
def day_fix_loop():
    for pipe in ("day_cycle",):
        _fix_loop_common(pipe, llm_port=8082)  # LLM 코드 수정만
```

- `day_fix_loop()`는 `_fix_loop_common()`을 호출하는데, 이는 **LLM 코드 수정**만 수행
- "day_cycle이 멈췄다"는 사실을 감지해도, LLM이 코드를 수정하게 하는 게 아니라 **인프라 복구**를 먼저 해야 함
- `day_fix_loop()`에는 인프라 수준 복구 로직이 전혀 없음

### Gap #5: `dead man's switch`가 `day_cycle` 진행을 감시하지 않음

```python
# config.py — HEARTBEAT_WORKERS
HEARTBEAT_WORKERS = {
    "embed_batch": 1800,
    "day_extract": 1800,
    "day_enrich": 1800,
    # etc.
}
```

- `enrich.py`가 `heartbeat()`를 호출하면 `HEARTBEAT_WORKERS`에 등록된 `day_enrich`의 staleness를 와치독이 감시
- **그런데 enrich.py가 heartbeat를 보내지 않으면** (시작조차 못 한 상태) → 등록 자체가 안 됨
- 즉, enrich.py가 포트 충돌로 시작조차 못 하면 와치독이 감지할 방법이 없음

---

## 4. 권장 개선 사항

### P0: 포트 충돌 감지 + 복구

**checker.py** — 포트 충돌 감지 함수 추가:

```python
def check_port_conflict(service_name: str = "devforge-day-cycle") -> tuple[bool, str]:
    """Check for "address already in use" errors in recent service logs."""
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", service_name, "--since", "10 min ago", "--no-pager"],
            capture_output=True, text=True, timeout=10,
        )
        if "address already in use" in r.stdout or "bind: address already in use" in r.stdout:
            return False, "port conflict detected (8080 in use)"
        return True, "no port conflict"
    except Exception as e:
        return True, f"check failed: {e}"
```

**orchestrator.py** — day_fix_loop에 포트 충돌 체크 + 복구 추가:

```python
def day_fix_loop():
    if _test_active:
        return
    # 1. 포트 충돌 체크
    ok, detail = check_port_conflict()
    if not ok:
        log(f"  [port-conflict] {detail}")
        # reranker(pod-a) stop → inference restart → reranker restart
        _kill_reranker_process()
        recover_container("devforge-inference")
        return
    # 2. 기존 fix loop
    for pipe in ("day_cycle",):
        _fix_loop_common(pipe, llm_port=8082)
```

### P1: reranker를 와치독 관리 대상에 추가

**config.py**:
```python
SERVICE_TARGETS = [
    "devforge-turn-watcher",
    "openrouter-rr-proxy",
    "container-devforge-pod-a",  # reranker 포함
]
```

### P2: `check_all_llm()` → reranker(8080) 별도 상태 관리

현재 `LLM_TARGETS`에는 reranker가 없음(야간 모델만 있음). reranker용 T1 체크를 추가:

```python
# orchestrator.py → _run_common_checks()
def _check_reranker(results):
    ok, detail = check_health(8080, "reranker")
    tracker = _state.get("llm:reranker")
    ...
```

---

## 5. 결론

와치독은 현재 **LLM 추론 슬롯, 파이프라인 state, 메모리, 서비스** 등 다양한 계층을 감시하지만, **포트 충돌(`address already in use`)**이라는 특정 장애 패턴을 감지/복구할 수 있는 로직이 전혀 없다.

이 문제는 `reranker가 inference 컨테이너 밖에서 실행 중`이라는 구조적 특성에서 발생하며, `day_cycle.sh` → `enrich.py` → `ensure_model()` 시퀀스에서 `kill_all()`이 reranker를 고려하지 않기 때문에 발생한다.

### 필요 로직 요약

| 단계 | 함수 | 위치 |
|------|------|------|
| 감지 | `check_port_conflict()` | `checker.py` (신규) |
| 복구 (reranker 정지) | `_kill_reranker_process()` | `recovery.py` (신규) |
| 복구 (inference 재시작) | `recover_container("devforge-inference")` | `recovery.py` (기존) |
| 통합 | `day_fix_loop()` → 포트 충돌 체크 추가 | `orchestrator.py` (수정) |
| 등록 | `CONTAINER_TARGETS`에 pod-a 추가 | `config.py` (수정) |