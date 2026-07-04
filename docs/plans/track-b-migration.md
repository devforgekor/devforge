# Track B Migration — Implementation Plan

## Reference
- **Web 검증 보고서**: `docs/reports/architecture-validation.md` (2026-06-10)
- **검증 환경**: Oracle Linux 9, ARM Neoverse-N1 4-core, rootless podman, ip_unprivileged_port_start=80

## Web 검증 핵심 발견 및 재검증 결과

| 가정 | Web 검증 결과 | 실제 구현 결과 |
|------|--------------|---------------|
| Caddy를 svc.pod에 포함 = 불가능 (pod loopback) | Caddy가 host 서비스(Natdata, 19999) 참조. pod 내부 127.0.0.1은 pod loopback. | **가능**. 모든 서비스가 pod 내부에 있으므로 127.0.0.1 동작. Netdata만 bridge gateway(10.89.0.1) 사용. |
| Caddy = 별도 Quadlet (Network=host) | Network=host Quadlet에서 PublishPort 불가 | Caddy를 svc.pod에 통합, 포트 80/443은 pod PublishPort로 해결 |

## 최종 아키텍처 (실제 배포)

```
svc.pod (devforge-net, publish 80:80, 443:443, 127.0.0.1:8000:8000)
├── caddy              (80/443 → reverse_proxy 127.0.0.1:PORT)
├── postgres           (5432 → pod localhost, DEVFORGE_DB_TCP=1)
├── devforge-mcp       (8000 → FastMCP Streamable HTTP, DEVFORGE_DB_TCP=1)
├── slack              (8084 → Slack Events API webhook)
├── slack-interactive  (8087 → NEUTRAL fact confirm/reject)
├── telegram-bot       (no port, long-polling daemon)
├── turn-watcher       (no port, DB turn watcher)
├── blob-explorer      (8085 → Azure Blob send/receive)
└── infra              (pause container)

Host-native (bridge gateway 10.89.0.1):
  └── netdata (19999)

Standalone Pod B (podman run --rm, direct container lifecycle):
  └── LLM inference (8081-8084)
  ⚠ Quadlet abandoned on OL9 — `--cgroups=split --sdnotify=conmon` fails with status=219/CGROUP
    in rootless mode. Pod B started via `podman run --rm` directly (no systemd service).
```

## 실행 완료 (2026-06-30)

### PR 1 완료: svc.pod 통합 + Caddy 전환
1. `svc.pod` 생성 — PublishPort=80, 443, 127.0.0.1:8000, ExitPolicy=continue
2. Caddy → Pod=svc.pod (Network=host → Pod), Caddyfile Netdata 주소 10.89.0.1:19999 변경
3. `Containerfile.base` 생성 — python:3.12-slim + all pip deps (468MB)
4. `devforge-base` 이미지 빌드 (4회 반복 패키지 발견)
5. `lib/db.py` DEVFORGE_DB_TCP=1 패턴 추가 (container TCP vs host podman exec)
6. `lib/slack_interactive/__main__.py` 생성 (python3 -m entry point)
7. `notice/slack.py` import 경로 수정 (git refactor 08330ad 대응)
8. postgres: pg_hba TCP trust entry 추가 (container-mode psql 대응)
9. 서비스 컨테이너 5개 생성: slack, telegram-bot, slack-interactive, blob-explorer, turn-watcher

### PR 3 완료: 정리
1. `pod-a.pod`, `pod-data-pod.pod`, `container-devforge-pod-a.container` → archive
2. `container-postgres.container` → archive (svc.pod로 대체)
3. host-mode 서비스 5개 stop + disable: slack, telegram-bot, devforge-slack-interactive, blob-explorer, devforge-turn-watcher
4. `container-postgres.service` disable

### PR 2 완료: Model Management Shell 함수
1. `lib/model_registry.py` 신규 생성 — MODEL_METADATA SSOT (14 entries)
2. `lib/pod_manager/models.py` — backward compat re-export
3. `lib/model_ctl.sh` 신규 생성 — shell 함수: `_run_model()`, `_stop_model()`, `_ensure_model()`, `_wait_health()`, `_wait_probe()`, `_check_model_id()`, `_model_port()`
4. `day_cycle.sh` — source model_ctl.sh, `ensure_pod_b()` → shell-based wrapper
5. `night_cycle.sh` — source model_ctl.sh, `switch_mode_pod_b()`, `switch_mode_both()` → `_ensure_model`
6. `cli.py status --json` — 호환성 유지 확인 완료

## 변경된 포트 매핑

| 포트 | 서비스 | Caddy 경로 | 비고 |
|------|--------|-----------|------|
| 80 | Caddy HTTP | - | pod PublishPort |
| 443 | Caddy HTTPS | - | pod PublishPort |
| 8000 | devforge-mcp | `/mcp*` | pod PublishPort, DevForge MCP |
| 8001 | tg-webhook | `/devforge/tg-webhook*` | 미구현 (Status: experimental) |
| 8084 | slack.py | `/webhooks/slack/*` | pod 내부 |
| 8085 | blob-explorer | `/send*`, `/receive*` | pod 내부 |
| 8087 | slack-interactive | `/slack/actions/*` | pod 내부 |
| 19999 | netdata (host) | `/netdata*` | bridge gateway 10.89.0.1 |

## Key Decisions
- **Sync fs creation on ARM**: Containerfile.base 468MB, 4 iteration pip install. Future: multi-stage로 slim화 가능.
- **DEVFORGE_DB_TCP=1 env var**: container에서 host postgres 접근용. 기존 host 스크립트는 podman exec 유지.
- **Caddy in pod**: pod 공유 네트워크로 127.0.0.1:PORT 동작. Netdata만 host-native라 bridge gateway 필요.
- **PublishPort multi-line**: Quadlet은 line당 하나의 PublishPort. Comma-separated 미지원.
