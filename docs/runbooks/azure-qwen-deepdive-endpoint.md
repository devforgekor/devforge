# 런북 — Azure Qwen을 devforge Deep Dive 추론 엔진으로 사용

> Status: active · Date: 2026-09-12 (09-11본 갱신) · Owner: devforge · Related: `docs/reports/control-plane-roadmap.md`, `docs/reports/mcp-consolidation-applied-20260911.md`
> 원칙: **로직·툴·상태는 devforge, Azure는 추론만.** Azure에 에이전트 로직/repo/DB를 두지 않는다.

---

## 0. 목적
Azure Spot VM의 `llama-server`(Qwen)를 **OpenAI 호환 추론 엔드포인트**로 노출하고, devforge의 Deep Dive(7단계·툴·상태)가 그 모델을 **추론 엔진으로만** 사용.

## 1. 전제 (필수)
1. **Tool-calling**: `llama-server`가 **`--jinja`** 로 기동돼야 OpenAI식 function calling 지원(검증: llama.cpp `docs/function-calling.md`). Deep Dive는 툴 사용이 필수 → **미지원이면 불가**.
2. 모델: Qwen3 계열. tool-calling 안정성 이슈 보고 있음 → **1회 스모크 필수**.
3. SSH 키(`~/.ssh/vm-azure-*-key.pem` 등)와 구독/리소스그룹(`lib/infra/azure_spot/config.py`).
4. devforge 로컬 서비스와 **포트 비충돌**(터널 로컬 포트는 `TUNNEL_PORT_BASE=18085`부터; `8085`는 podman rootlessport 점유).

## 2. 절차
1. **VM 생성 + 터널**:
   ```bash
   cd /opt/projects/server/scripts
   python3.11 -m lib.infra.azure_spot.cli launch --label qwen3-30b
   # → VM 생성 → SSH/헬스 대기 → 터널 localhost:18085 → VM:8080
   ```
2. **모델 서버가 `--jinja`인지 확인**(골든 이미지 `llm.service`에 baked-in `--jinja` 반영됨):
   ```bash
   ssh azureqwen 'systemctl cat llm | grep -i jinja'
   ```
3. **엔드포인트 확인**:
   ```bash
   curl -s http://127.0.0.1:18085/v1/models
   ```
4. **opencode provider 등록**(devforge 설정) — OpenAI 호환:
   ```jsonc
   // ~/.config/opencode/opencode.json (providers 예시)
   { "provider": { "azureqwen": { "npm": "@ai-sdk/openai-compatible",
       "options": { "baseURL": "http://127.0.0.1:18085/v1" },
       "models": { "qwen3-30b": {} } } } }
   ```
5. **Deep Dive 실행**(로직/툴은 devforge, 모델만 Azure):
   ```bash
   opencode run -m azureqwen/qwen3-30b "<딥다이브 대상>"
   ```
6. **작업 완료 시 즉시 삭제 + 검증**(비용 0, 다음 생성 가능):
   ```bash
   python3.11 -m lib.infra.azure_spot.cli destroy    # 삭제 + 삭제확인(remaining=0)
   python3.11 -m lib.infra.azure_spot.cli verify     # CLEAN 확인
   ```
   - 스크립트형(생성→실행→**항상** 삭제): `run` 래퍼 사용(예외가 나도 finally에서 teardown).
     ```bash
     python3.11 -m lib.infra.azure_spot.cli run --label qwen3-30b -- <실행할 명령>
     ```
   - **삭제 확인 로직**: `destroy`/`run`은 `wait_until_deleted`로 **실제 제거를 확인**(remaining=0) → 그래야 Spot 쿼터가 풀려 다음 생성이 즉시 됨.
   - **사전 정리**: `launch`는 **preflight_clean**으로 잔여 VM을 먼저 삭제.
   - **백스톱**: 프로세스가 비정상 종료되면 `sweep --ttl`를 타이머로 돌려 orphan 정리.

## 3. 검증
- `/v1/models` 응답.
- **tool-calling 1회**(모델이 툴 호출 포맷을 내는지).
- Deep Dive 1단계 실제 실행 → 상태가 devforge(`deepdive_steps`·`tasks`)에 기록되는지.

## 4. 롤백 / 폴백
- **Spot eviction** 시 추론 중단 → 상태는 devforge에 보존 → **로컬 모델로 폴백** 후 재개.
- 터널/VM 정리 실패 시 `status` → orphan 식별 → `delete`.

## 5. 주의
- Azure Spot은 **회수 위험** + **비용 누수**. **TTL/자동정리 구현됨**: `cli sweep --ttl <sec>` (VM명의 epoch로 age 산출, 초과분 삭제). 운영은 타이머(cron/systemd)로 `sweep` 주기 실행 권장.
- 포트: `TUNNEL_PORT_BASE=18085`부터(8085는 podman rootlessport 점유). 로컬 8081(embedder) 등과 충돌 금지.
- bespoke 원격 클라이언트를 만들지 말 것(표류) → **동일 하네스 + provider만 교체**.

## 6. 현황 / 블로커 (2026-09-11, 실측)
**config 정합 완료**(`lib/infra/azure_spot/config.py`):
| label | SP | RG | 갤러리 / 이미지 | VNet / subnet |
|---|---|---|---|---|
| qwen3-30b | account1 | `rg-devforge-prod-cin` | `gallery_devforge_prod_cin` / **`llm-qwen-27b`** (v2026.09.3, MoE baked-in) | `vm-devforge-prod-cin-vnet` / `default` |
| nemotron3-nano | account2 | `rg-devforge-llm-prod-cin` | `gallery_devforge_llm_prod_cin` / (이미지 없음) | `vm-devforge-llm-prod-cin-vnet` |
| gemma-4-26b | account3 | `rg-devforge-llm-judge-cin` | `gallery_devforge_llm_judge_cin` / (이미지 없음) | `vm-gemma-4-26b-spotVNET` |

> **활성 = `qwen3-30b` 단일 계정(account1).** nemotron/gemma은 **폐기**(계정/SP 정보는 유지). VM 크기 = **`Standard_FX2mds_v2`**(모듈 배포, 2 vCPU/42GiB; 2026.09.3 빌더는 `Standard_FX2ms_v2` — 동일 2 vCPU/42GiB·Regular FX quota=0이라 빌더는 Spot 필수). 추론 포트 = **8080**, 터널 대역 = **18085**. `--max-price`는 config `max_price`(기본 `-1`).

**검증(라이브, 최종)**:
- **읽기**: 3계정 `list_spot_vms`/`sweep --dry-run` 정상.
- **전체 라이프사이클 성공**: `launch → destroy → verify`
  - 생성(`Standard_FX2mds_v2` spot) → SSH → **llama-server ready(:8080)** → **터널 `localhost:18085 → VM:8080`** → `destroy`(VM+NIC+PublicIP+터널 삭제, `verified=True remaining=0`) → `verify=CLEAN`, 터널 잔여 0.
- **쿼터**: `Standard_FX2mds_v2`(2 vCPU)는 **`LowPriorityCores`(3) 안 → 상향 불필요**.

**교정 사항(이번 Deep Dive)**: VM 크기 A100→`Standard_FX2mds_v2`, 추론 포트 8081→**8080**, 터널 대역 8085→**18085**(8085에 podman rootlessport), `_az`의 `--subscription`을 인자 **끝**으로(앞에 두면 `vm create` 거부), SSH `TimeoutExpired` 처리, 터널 정리를 **tracked-pid** 기반(무관 프로세스 오살 방지), **PublicIpAddress 쿼터(구독당 3)** 대응해 destroy가 **PIP까지 삭제**.

**남은 블로커**: 없음(에이전트가 이 엔드포인트로 Deep Dive 구동 시 opencode provider 등록 + 서버 `--jinja` 필요 — §1·§2).

**Qwen Deep Dive 준비 상태 (2026-09-11, 실측·확정)**:
- 골든 이미지 유닛 = **`/etc/systemd/system/llm.service`**(name `llm`). 모듈 **`ensure_tool_calling()`** 이 SSH 후 `--jinja` 자동 적용(stop→sed→reload→start).
- **모델 확정 = `Qwen3-30B-A3B-Q4_K_M`**(MoE·3B active, 18.56GB). FX2ms_v2에서 **툴콜 정상**(`finish_reason:"tool_calls"`), 생성 **~6 tok/s**. (Q6_K 25GB·~3.3 tok/s, Q8 ~2.2 tok/s → 비권장)
- **필수 플래그**: `--jinja` + `--chat-template-kwargs '{"enable_thinking":false}'`.
- **모듈 코드 기준**(`lib/infra/azure_spot/`):
  - `config.py`: VM_SIZE `Standard_FX2mds_v2`, `LLAMA_SERVER_PORT=8080`, `TUNNEL_PORT_BASE=18085`, `max_price=-1.0`, 활성 config 1개(qwen).
  - `manager.py`: `create_vm` / `poll_until_ready` / `ensure_tool_calling` / `check_tool_calling` / `wait_until_deleted` / `delete_vm_verified`(NIC+PIP 포함) / `sweep_orphans`.
  - `orchestrator.py`: `launch_all` / `teardown` / `verify_clean` / `preflight_clean` + 소비자 호환 `add`·`provision_all`·`managers`·`terminate_all`.
  - `tunnel.py`: **tracked-pid**(state file, ssh 검증) 기반 close.
  - `cli.py`: `launch` / `status` / `delete` / `sweep` / `destroy` / `verify` / `run`.
- **완료(2026-09-12)**: 골든 이미지 재빌드에 MoE baked-in(`runbook-golden-image.md`) → 갤러리 **`llm-qwen-27b:2026.09.3`** 등록 → 배포 검증(`launch`, `check_tool_calling` OK) → 터널 `18085` 경유 `/v1/chat/completions` **`finish_reason:"tool_calls"`** → `destroy` CLEAN. opencode provider **`azureqwen`**(baseURL `http://127.0.0.1:18085/v1`) 등록. **남은 것**: opencode 재시작 후 provider로 Deep Dive 1회 실행 검증(런타임 config는 재시작 시 반영).
