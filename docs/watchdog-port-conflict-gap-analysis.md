# Deep Dive — Watchdog 포트 충돌 감지·복구 로직 설계

> 산출: 2026-09-08 13:05 KST
> 방식: Deep Dive 워크플로우 (문제 구조화 → 코드 확인 → context7 검증 → 복구 로직 설계)

---

## 1. 문제 원인 (확정)

### 1.1 포착된 실시간 상태

```
day_cycle.sh ──> enrich.py ──> ensure_model("day-enricher")
  └─ kill_all() → _podman_stop_inference()
  └─ _podman_start_inference()
      └─ rootlessport: "bind: address already in use" (8080)
      └─ devforge-inference 상태 = "Created" (시작 실패)
      └─ enrich.py: ensure_model 재시도 무한 루프
```

### 1.2 포트 8080을 점유하는 3개 주체

| 주체 | 종류 | 상태 |
|------|------|------|
| `PID 3449 /app/llama-server --port 8080` | 호스트 프로세스 (reranker) | ✅ 실행 중 |
| `pod-a-infra` | Podman pod (8080 publish) | ✅ 활성 |
| `devforge-pod-a` | Podman pod (8080 publish) | ✅ 활성 |

→ inference 컨테이너가 8080에 바인딩하려다 **모두 이미 사용 중** → 실패.

### 1.3 코드 경로

- `day_cycle.sh:140 _launch_reranker()` — reranker를 `podman exec devforge-inference` 안에서 실행하는 설계
- 그러나 현재 reranker는 **호스트 PID 3449**로 떠 있어서 설계와 다른 상태
- `lib/pod_manager/container.py:37` — "Port 8080 is NOW published — Pod A is inactive, inference can serve reranker" 주석 → 이미 이전에도 포트 충돌 이력 있음

---

## 2. context7 검증 (Podman 공식 문서)

| 검증 항목 | 결과 | 출처 |
|-----------|------|------|
| rootless port publishing | rootless bridge에서 `rootlessport` 프록시가 8080을 잡음. `--publish 127.0.0.1:8080:8080` 시에도 이전에 같은 포트를 publish한 pod/컨테이너가 있으면 "bind: address already in use" | docs.podman.io |
| healthcheck 컨테이너 | 컨테이너가 unhealthy 시 kill/restart/stop 자동 트리거 가능 | podman-run(1) |
| exec inside running container | `podman exec`으로 기존 컨테이너 안에서 프로세스 킬/재시작 가능 | POST /containers/{name}/exec |
| systemd + podman | systemd service에서 `Restart=` 직접 사용 권장 (podman-restart.service 대신) | podman-systemd.unit(5) |

**결론**: "address already in use"는 컨테이너/호스트 경계의 포트 중복이 원인이며, watchdog이 감지 후 **포트를 점유한 프로세스를 킬 + inference 재시작**하는 흐름이 정석이다.

---

## 3. 복구 — 시스템 리셋 안내

지금 enrich.py가 무한 재시도 중이므로 **먼저 수동 복구**가 필요하다.

```bash
# 1. 재시도 루프 차단 (enrich/cycle 중지)
systemctl --user stop devforge-day-cycle.service

# 2. 8080 점유 프로세스 확인 후 킬
#    reranker(호스트 PID 3449) + pod-a의 8080 publish
kill 3449                                   # 호스트 reranker
podman pod stop devforge-pod-a              # Pod A 중지 (8080 해제)

# 3. inference 컨테이너 정리 및 재시작
podman rm -f devforge-inference             # Created 상태 정리
systemctl --user start devforge-day-cycle.service
```

> ⚠️ `podman pod stop devforge-pod-a`는 Pod A 안의 다른 컨테이너(있으면)까지 중지한다.
> 현재 pod-a-infra는 infra 용도이므로 확인 후 처리.

---

## 4. Watchdog 자동 복구 로직 설계

### 4.1 감지 단계 (checker.py 신규 함수)

```python
PORT_CONFLICT_PATTERNS = (
    "bind: address already in use",
    "address already in use",
    "rootlessport listen",
)

def check_port_conflict(service="devforge-day-cycle", since_min=15) -> tuple[bool, str]:
    """Recent service log에서 포트 충돌 패턴 감지."""
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", service, "--since", f"{since_min} min ago", "--no-pager"],
            capture_output=True, text=True, timeout=10,
        )
        for pat in PORT_CONFLICT_PATTERNS:
            if pat in r.stdout:
                return False, f"port conflict detected: {pat}"
        return True, "no port conflict"
    except Exception as e:
        return True, f"check failed: {e}"
```

### 4.2 복구 단계 (recovery.py 신규 함수)

```python
def recover_port_conflict() -> bool:
    """8080 점유 프로세스를 정리하고 inference를 재시작."""
    log("  [port-conflict] freeing port 8080 and restarting inference...")
    try:
        # 1. 호스트 reranker 프로세스 정리
        subprocess.run(["pkill", "-f", "llama-server.*--port 8080"], capture_output=True)
        # 2. Pod A의 8080 publish 해제
        subprocess.run(["podman", "pod", "stop", "devforge-pod-a"], capture_output=True, timeout=30)
        # 3. Created/stale inference 제거
        subprocess.run(["podman", "rm", "-f", "devforge-inference"], capture_output=True, timeout=30)
        # 4. inference 재시작
        from lib.pod_manager.container import _podman_start_inference
        return _podman_start_inference()
    except Exception as e:
        log(f"  port conflict recovery failed: {e}")
        return False
```

### 4.3 통합 (orchestrator.py day_fix_loop)

```python
def day_fix_loop():
    if _test_active:
        return
    # P0: 포트 충돌 먼저 진단
    ok, detail = check_port_conflict()
    if not ok:
        log(f"  [watchdog] {detail} → running port conflict recovery")
        _state.add_event("port_conflict", "detected", detail)
        if recover_port_conflict():
            _state.add_event("port_conflict", "recovered", "inference restarted")
            send_recovery("port_conflict", "inference restarted after port conflict")
            return
        _state.add_event("port_conflict", "recovery_failed", detail)
        send_alert("port_conflict", "DOWN", detail)
        return
    # 기존 day fix loop
    for pipe in ("day_cycle",):
        _fix_loop_common(pipe, llm_port=8082)
```

### 4.4 등록 (config.py)

```python
# 8080이 inference와 겹치는지 계속 감시하기 위해
SERVICE_TARGETS = [
    "devforge-turn-watcher",
    "openrouter-rr-proxy",
    # reranker(호스트)는 systemd 서비스가 아니므로 별도 처리,
    # 포트 충돌 로직이 이를 대체
]
```

---

## 5. 이후 재발 방지 (구조적)

### 문제의 근본: reranker가 호스트 프로세스로 떠 있음

설계(`day_cycle.sh:140`)는 **reranker를 inference 컨테이너 안에서** 실행하도록 되어 있는데, 현재는 호스트 PID 3449로 떠 있어서 컨테이너 시작 시마다 충돌한다.

**해결**: `day_cycle.sh` 맨 앞에 reranker 호스트 프로세스 정리 + inference 시작 전 8080 해제 보장을 추가:

```bash
# day_cycle.sh 상단, System Sync 전
_ensure_8080_free() {
    # 호스트 reranker 프로세스가 있으면 정리
    if pgrep -f "llama-server.*--port 8080" >/dev/null; then
        LOG "  Freeing host reranker on :8080"
        pkill -f "llama-server.*--port 8080"
        sleep 2
    fi
    # Pod A가 8080을 publish 중이면 중지
    if podman pod ps --format '{{.Name}}' | grep -q '^devforge-pod-a$'; then
        LOG "  Stopping pod-a to release :8080"
        podman pod stop devforge-pod-a
    fi
}
```

### 와치독 polling 주기

| 수준 | 주기 | 역할 |
|------|------|------|
| `check_day_cycle_health()` | 60s | day_cycle.service active 여부 |
| `check_port_conflict()` | 60s (+15min 창) | "address already in use" 로그 탐지 |
| `check_pipeline_stuck()` | 60s | enrich 시작 못 했다면 state가 accumulating 안 되는지 |

---

## 6. 결론

1. **원인 확정**: reranker(호스트 PID 3449) + pod-a(8080 publish)가 8080을 점유, inference 컨테이너가 바인딩 실패 → "Created"에서 시작 못 함.
2. **즉시 조치**: 8080 점유자 제거 후 day_cycle 재시작.
3. **자동화**: watchdog에 `check_port_conflict()` + `recover_port_conflict()` 추가.
4. **재발 방지**: `day_cycle.sh`에 `_ensure_8080_free()` 사전 정리 + Pod A 정리 루틴.
5. **context7 검증**: podman 공식 문서로 "rootlessport bind conflict = 기존 publish와의 충돌", "exec으로 프로세스 관리" 확인.