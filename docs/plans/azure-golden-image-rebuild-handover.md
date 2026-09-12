# 핸드오버 — Azure Golden Image 재빌드 (다음 세션)

> Status: active · Date: 2026-09-11 · Owner: devforge · Related: `docs/runbooks/runbook-golden-image.md`, `docs/runbooks/azure-qwen-deepdive-endpoint.md`, `docs/reports/mcp-consolidation-applied-20260911.md`
> **다음 세션의 첫 작업 = 골든 이미지 재빌드(Qwen3-30B-A3B MoE baked-in).** 이 문서만 보면 이어서 진행 가능.

---

## 1. 지금 상태 (요약)
- **MCP 통합/서버측 리서치/태스크 단일화** 완료(별도 문서).
- **azure_spot 모듈** 정합·엔드포인트화·삭제검증·TTL 완료. **라이브 전경로 검증 성공**.
- **모델 확정**: `Qwen3-30B-A3B-Q4_K_M`(MoE, 3B active, 18.56GB). 2 vCPU에서 **툴콜 정상·~6 tok/s** 검증.
- 골든 이미지(`llm-qwen-27b:2026.09.2`)는 **아직 27B dense** → 재빌드로 MoE 교체 필요.

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

## 3. 다음 세션 작업 순서
1. **골든 이미지 재빌드** (`docs/runbooks/runbook-golden-image.md` 최신본 그대로):
   - §1.1 빌더 VM(=`Standard_FX2ms_v2`, Ubuntu2204) → §1.2 설정(모델 Q4 MoE + `llm.service` + `--jinja` + enable_thinking=false) → 일반화 → 캡처 → **§1.6 이미지 정의에 `--features "DiskControllerTypes=SCSI,NVMe"`** → 새 버전(예: `2026.09.3`) 등록.
   - 다운로드 18.56GB는 **`curl --retry --retry-all-errors -C -`** 로(중간 stall 재현됨).
2. **배포 검증**: `azure-qwen`/§2로 새 버전 배포 → `/v1/chat/completions` 툴콜 스모크(모델 `finish_reason:"tool_calls"` 확인).
3. **opencode provider 등록**: baseURL `http://127.0.0.1:18085/v1` (모듈 `run`/터널). Deep Dive 7단계 1회 실행 검증.
4. **모듈 정합 확인**: `ensure_tool_calling()`이 `llm.service`를 찾도록(현재 `/usr/local/bin/llama-server` grep) 유지.

## 4. 함정 / 주의 (이번에 겪은 것)
- **DiskControllerTypes**: `SCSI, NVMe` 병기 아니면 FX2ms_v2에서 `cannot boot ... DiskControllerTypes supported: NVMe`로 **부팅 실패**. (E4s_v3는 현재 `NotAvailableForSubscription`.)
- **구독당 `PublicIpAddress` 쿼터=3**: `az vm delete`가 PIP를 남김 → `delete_vm_verified`가 NIC+PIP까지 삭제하도록 구현됨(삭제 검증 `verify=CLEAN`).
- **SSH 불안정(모델 로딩 부하)**: 긴 작업은 **백그라운드 스크립트 + 폴링**. `TimeoutExpired` 처리됨.
- **다운로드 stall**: `curl -C - --retry-all-errors`로 이어받기.
- **터널 정리**: tracked-pid(state file). 무관 프로세스 오살 금지(8085 rootlessport 사례).
- **재빌드는 비쌈**: 검증된 설정만 굽기(이번에 MoE+jinja+enable_thinking 검증 완료).

## 5. 이번 세션 변경 파일 (참고)
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
