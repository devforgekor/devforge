# 핸드오버 — Azure Golden Image 재빌드 (완료)

> Status: completed · Date: 2026-09-12 (09-11본 갱신, 동일자 완료) · Owner: devforge · Related: `docs/runbooks/runbook-golden-image.md`, `docs/runbooks/azure-qwen-deepdive-endpoint.md`, `docs/reports/mcp-consolidation-applied-20260911.md`
> **완료(2026-09-12):** 골든 이미지 재빌드(Qwen3-30B-A3B-Q4_K_M MoE baked-in) → `llm-qwen-27b:2026.09.3` 등록 → 배포 툴콜 검증(`finish_reason:"tool_calls"`) → 임시 리소스 정리 CLEAN. opencode provider `azureqwen`(baseURL `127.0.0.1:18085/v1`) 등록. **남은 것**: opencode 재시작 후 provider로 Deep Dive 1회 실행 검증.

## 0-0. 완료 요약 (2026-09-12 실행분)
- **이미지**: `gallery_devforge_prod_cin/llm-qwen-27b:2026.09.3` (`Succeeded`, `DiskControllerTypes=SCSI, NVMe`).
- **baked-in**: `/opt/models/qwen3-30b-a3b-q4_k_m.gguf`(18G, MoE) + `/opt/llama/llama-server`(llama.cpp **b10919**, `libgomp1`) + `/usr/local/bin/llama-server` 심볼릭 + `llm.service`(enabled, `--jinja` + `enable_thinking=false`).
- **검증**: 모듈 `launch` 툴콜 OK → 터널 18085 경유 `/v1/chat/completions` `finish_reason:"tool_calls"` → `destroy` `CLEAN`(잔여 0).
- **DB**: `golden_image_versions` 2026.09.3 active / 2026.09.2 deprecated.
- **수정**: `azure:20137133/scripts/golden_image/yearly_refresh.sh`(runbook 정합: Spot 빌더, b10919 자산명, `libgomp1`, llm.service) — commit `44a177b`.
- **신규 사실(중요)**: 이 구독 Regular `StandardFXmsv2Family`/`StandardFXmdsv2Family` quota=0 → **빌더는 Spot 필수**(`lowPriorityCores=3`, eviction-policy Deallocate). `runbook-golden-image.md` §1.1 반영.

---

## 0. 2026-09-12 세션 정비 (인프라 — 착수 전 필독)
Deep Dive 백엔드(devforge-mcp HTTP)가 죽어 있던 원인을 정비했다. **다음 세션 시작 시 devforge-mcp가 정상이어야 `deepdive_step_*` 툴이 로드된다.**
- **svc pod 포트포워딩 복구**: 호스트 `127.0.0.1:8000/8002/8085/8191`(rootlessport) 전면 다운 → `systemctl --user restart svc-pod.service`로 복구. devforge-mcp 25툴/FlareSolverr 8191 정상.
- **재발방지 구현(task#32, Deep Dive `dp-20260912-watchdog-svcpod-portforwarding`)**: watchdog에 `check_svcpod_ports`(TCP connect) + `recover_svcpod_forwarding`(svc-pod.service restart) + `_run_svcpod_forwarding` 통합(60s 주기 자동 감지·복구). 실장애 주입 검증(rootlessport kill→복구 ~26s True).
- **Quadlet generator 실패 제거**: `container-flaresolverr.container`(주석 stub) → `_disabled/*.disabled`. generator rc=0.
- **버그 수정**: `activity_summarizer.py` int.isdigit() AttributeError, `watchdog/checker.py` MODE_FILE_INFERENCE import 누락.
- DB: `handover.yaml` cp#104 · obs 4건 · task#32 completed.

> ⚠️ 세션 시작 시 devforge-mcp 로드가 실패하면 그 세션은 재연결하지 않아 `deepdive_step_*`를 못 쓴다 → **opencode 세션을 새로 시작**해야 한다. 포트포워딩은 이제 watchdog이 자동 복구한다.

---

## 1. 지금 상태 (요약)
- **MCP 통합/서버측 리서치/태스크 단일화** 완료(별도 문서).
- **azure_spot 모듈** 정합·엔드포인트화·삭제검증·TTL 완료. **라이브 전경로 검증 성공**.
- **모델 확정**: `Qwen3-30B-A3B-Q4_K_M`(MoE, 3B active, 18.56GB). 2 vCPU에서 **툴콜 정상·~6 tok/s** 검증.
- 골든 이미지(`llm-qwen-27b:2026.09.2`)는 **아직 27B dense** → 재빌드로 MoE 교체 필요.
- **(2026-09-12)** devforge-mcp/svc pod 정상화 + watchdog 포트포워딩 자동복구 가동 → Deep Dive 툴 사용 가능.

## 2. 확정된 값 (SSOT)
| 항목 | 값 |
|---|---|
| 활성 config | `qwen3-30b` (account1, sub `a942e898-…`) |
| VM 크기 | `Standard_FX2mds_v2` (2 vCPU / 42GiB) — `LowPriorityCores(3)` 안이라 quota 상향 불필요 |
| 리소스 그룹 | `rg-devforge-prod-cin` (Central India) |
| Compute Gallery | `gallery_devforge_prod_cin` |
| 이미지 정의 | **`llm-qwen-27b`** (`DiskControllerTypes=SCSI,NVMe` **필수**) |
| 모델 | **`Qwen3-30B-A3B-Q4_K_M`** (HF `Qwen/Qwen3-30B-A3B-GGUF`) → `/opt/models/qwen3-30b-a3b-q4_k_m.gguf` |
| 추론 포트 | `8080` (llama-server), `--host 127.0.0.1` |
| 터널 대역 | `TUNNEL_PORT_BASE=18085` (8085는 podman rootlessport 점유) |
| 서비스 | `llm.service`, 바이너리 `/usr/local/bin/llama-server` |
| 필수 플래그 | **`--jinja`** + **`--chat-template-kwargs '{"enable_thinking":false}'`** |

## 3. 작업 순서 (2026-09-12 실행: 1·2·4 완료, 3 provider 등록 완료·검증만 재시작 대기)
0. **사전 확인(필수)**: 새 opencode 세션에서 `deepdive_step_*` 툴이 보이는지 = devforge-mcp 정상. 안 보이면 `curl -s http://127.0.0.1:8000/health` 확인 → 죽어 있으면 watchdog 복구를 기다리거나 `systemctl --user restart svc-pod.service`. (`cli.py status --json`도 정상 확인) — **완료(이번 세션 정상).**
1. **골든 이미지 재빌드** (`docs/runbooks/runbook-golden-image.md` 최신본 그대로): — **완료 (`2026.09.3`)**
   - §1.1 빌더 VM(=`Standard_FX2ms_v2`, Ubuntu2204) → §1.2 설정(모델 Q4 MoE + `llm.service` + `--jinja` + enable_thinking=false) → 일반화 → 캡처 → **§1.6 이미지 정의에 `--features "DiskControllerTypes=SCSI,NVMe"`** → 새 버전(예: `2026.09.3`) 등록.
   - 다운로드 18.56GB는 **`curl --retry --retry-all-errors -C -`** 로(중간 stall 재현됨).
   - **주의**: Regular FX quota=0 → 빌더 `--priority Spot --eviction-policy Deallocate` 필수, `libgomp1` 필수.
2. **배포 검증**: `azure-qwen`/§2로 새 버전 배포 → `/v1/chat/completions` 툴콜 스모크(모델 `finish_reason:"tool_calls"` 확인). **완료**(모듈 launch + 터널 18085 경유, `get_weather` → `tool_calls`).
3. **opencode provider 등록**: baseURL `http://127.0.0.1:18085/v1` (모듈 `run`/터널). Deep Dive 7단계 1회 실행 검증. **provider `azureqwen` 등록 완료** — 단, opencode config는 재시작 시 반영 → **재시작 후 Deep Dive 1회 실행 검증만 남음**(VM을 `launch`로 띄운 상태에서).
4. **모듈 정합 확인**: `ensure_tool_calling()`이 `llm.service`를 찾도록(현재 `/usr/local/bin/llama-server` grep) 유지. **완료**(baked-in `--jinja` → `ALREADY`, `check_tool_calling` OK).

## 4. 함정 / 주의 (이번에 겪은 것)
- **DiskControllerTypes**: `SCSI, NVMe` 병기 아니면 FX2ms_v2에서 `cannot boot ... DiskControllerTypes supported: NVMe`로 **부팅 실패**. (E4s_v3는 현재 `NotAvailableForSubscription`.)
- **구독당 `PublicIpAddress` 쿼터=3**: `az vm delete`가 PIP를 남김 → `delete_vm_verified`가 NIC+PIP까지 삭제하도록 구현됨(삭제 검증 `verify=CLEAN`).
- **SSH 불안정(모델 로딩 부하)**: 긴 작업은 **백그라운드 스크립트 + 폴링**. `TimeoutExpired` 처리됨.
- **다운로드 stall**: `curl -C - --retry-all-errors`로 이어받기.
- **터널 정리**: tracked-pid(state file). 무관 프로세스 오살 금지(8085 rootlessport 사례).
- **재빌드는 비쌈**: 검증된 설정만 굽기(이번에 MoE+jinja+enable_thinking 검증 완료).

## 5. 변경 파일 (참고)
**(2026-09-12, 골든 이미지 재빌드 완료)**
- `azure:20137133/scripts/golden_image/yearly_refresh.sh` (Spot 빌더 + b10919 자산명 + `libgomp1` + `llm.service` 정합, commit `44a177b`)
- `docs/runbooks/runbook-golden-image.md` (§1.1 Spot 필수, §1.2 b10919/libgomp1/심볼릭, §9 개정)
- `docs/runbooks/azure-qwen-deepdive-endpoint.md` (8085→18085, v2026.09.3)
- `~/.config/opencode/opencode.json` (`azureqwen` provider 추가)
- DB: `golden_image_versions` 2026.09.3 active
**(2026-09-12, 인프라 정비)**
- `scripts/lib/watchdog/{config,checker,recovery,orchestrator,__init__}.py` (svc-pod 포트포워딩 감지·복구)
- `scripts/activity_summarizer.py` (isdigit 버그), `~/.config/containers/systemd/_disabled/container-flaresolverr.container.disabled`
**(2026-09-11, MCP/Azure)**
- `scripts/lib/infra/azure_spot/{config,manager,orchestrator,tunnel,cli,__init__}.py`
- `scripts/lib/debate/cooperative_debate.py`, `scripts/lint_rules/data.py`
- `docs/runbooks/runbook-golden-image.md`, `docs/runbooks/azure-qwen-deepdive-endpoint.md`
- (MCP 통합) `lib/research/*`, `proxies/search.py`, `exa_mcp.py`, `context7_mcp.py`, `cli.py`, `~/.config/opencode/opencode.json`, `/home/opc/llm-agent-rule.md`→`AGENTS.md`, `docs/plans/mcp-consolidation-*`, `docs/reports/*`

## 6. 검증된 실측 (Q4 vs Q6)
| quant | 크기 | 생성속도 | 툴콜 |
|---|---|---|---|
| **Q4_K_M** | 18.56GB | **~6.0 tok/s** | ✅ |
| Q6_K | 25.09GB | ~3.3 tok/s | ✅ |
| Q8_0 | ~31GB | (추정 ~2.2) | — |
