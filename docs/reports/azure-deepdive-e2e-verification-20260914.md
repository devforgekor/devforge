# Azure azureqwen Deep Dive E2E 검증 (2026-09-14)

> Status: record · Date: 2026-09-14 · Owner: devforge · Related: [`runbooks/azure-qwen-deepdive-endpoint.md`](../runbooks/azure-qwen-deepdive-endpoint.md), [`runbooks/runbook-golden-image.md`](../runbooks/runbook-golden-image.md), [`plans/azure-golden-image-rebuild-handover.md`](../plans/azure-golden-image-rebuild-handover.md)

## 1. 목적
골든 이미지 `llm-qwen-27b:2026.09.3`(Qwen3-30B-A3B MoE baked-in) Spot VM을 **opencode 추론 백엔드**로 사용할 때,
등록한 provider `azureqwen`이 **Deep Dive(devforge-mcp 툴 + DB 상태) 전경로**에서 실제로 동작하는지 E2E로 검증한다.
겸사겸사 검증 중 드러난 **ctx 부족**·**2 vCPU 성능** 이슈를 실측하고 SSOT를 정정한다.

## 2. 방법
1. `lib.infra.azure_spot.cli launch --label qwen3-30b` → VM 생성 + SSH + `llm.service` health + 터널 `localhost:18085 → VM:8080`.
2. 엔드포인트 스모크: `GET /v1/models`, `POST /v1/chat/completions`(tool 스키마) → `finish_reason` 확인.
3. **E2E**: `opencode run -m azureqwen/qwen3-30b --auto`로 모델이 `devforge-mcp`의 `deepdive_step_enter/exit`를 호출하게 하고,
   `deepdive_session_status`로 DB(`deepdive_steps`) 기록 확인.
4. **성능/스레드 실측**: 동일 2,117-token 프롬프트로 prefill tok/s 비교(`-t 1` vs `-t 2`, `-c 8192` vs `-c 32768`), `lscpu`/`nproc`·llama.cpp startup `n_threads`.
5. 정리: `cli destroy` + `cli verify`(remaining=0).

## 3. 결과 (데이터)

### 3.1 E2E 성공
| 항목 | 값 |
|---|---|
| provider/model | `azureqwen/qwen3-30b` (opencode **1.18.30**) |
| 엔드포인트 | `http://127.0.0.1:18085/v1` (터널 → VM:8080) |
| tool call | 모델이 **`deepdive_step_enter` → `deepdive_step_exit`**를 step 1·2에 대해 실제 호출 |
| DB | `deepdive_session_status(smoke-azureqwen-20260912)` → step1·2 `DONE` (elapsed 126s/128s, `has_aborted_step=false`) |
| 정리 | `destroy` → `verified=True, remaining=0`, `18085` down |

### 3.2 블로커 1 — context 부족
| 항목 | 값 |
|---|---|
| opencode baseline 요청 | **~16,699 tokens** (system prompt + MCP 툴 스키마 ~25개) |
| 골든 이미지 `2026.09.3` | `llm.service` **`-c 8192`** → `request (16699 tokens) exceeds the available context size (8192 tokens)` |
| 검증 조치 | VM에서만 **`-c 32768`로 임시 상향** 후 E2E 성공 |
| 메모리 | ctx 32768에서 `llama-server` RES **~33.5GB** < `MemoryMax=38G` (`n_slots=4`, `kv_unified=true`) |

### 3.3 블로커 2 — 2 vCPU 성능/스레드
| 조건 | prefill | 비고 |
|---|---|---|
| `-c 8192 -t 1`(기본) | **14.28 tok/s** | `n_threads=1` 자동 |
| `-c 8192 -t 2` | **14.36 tok/s** | **무효**(SMT) |
| `-c 32768 -t 2` | **14.15 tok/s** | ctx 무관 |
| 장시간/장문맥(16k) 실행 중 | 6.7 → 6.7 tok/s | 부하 지속 시 감속 |
| generation | ~2–4.5 tok/s(단문맥), ~0.5 tok/s(16k 문맥) | — |

- 토폴로지: `lscpu` = **1 physical core / 2 SMT threads**(Intel Xeon 8573C). llama.cpp는 물리 core 기준 `n_threads=1` → 2번째 logical CPU 미사용.
- `-t 2` 무효 → 병목은 **메모리 대역**(memory-bound)이며 SMT로 개선 불가.
- 결과적으로 opencode 첫 턴 prefill만 ~20–40분 → **인터랙티브 Deep Dive 백엔드로는 부적합**(단발 추론/툴콜은 유효).

## 4. 검증
- `GET /v1/models` → 모델 id `/opt/models/qwen3-30b-a3b-q4_k_m.gguf`; 요청 `model:"qwen3-30b"`도 허용(alias) 확인.
- tool-call 스모크: `finish_reason="tool_calls"`, `arguments={"city":"Seoul"}`.
- E2E: 위 3.1 (DB `deepdive_steps` `DONE`).
- SSOT 정합: `bash -n yearly_refresh.sh` OK; `handover.yaml` YAML `safe_load` OK.
- 리소스 정리: `cli destroy`/`verify` CLEAN.

## 5. 결론
- `azureqwen` provider는 **엔드포인트·툴콜·DB 연동이 전경로 정상**이다(“추론만” 원칙 유효).
- 그러나 **ctx 8192 < opencode ~16.7k**와 **2 vCPU memory-bound 성능** 두 가지가 opencode 풀 에이전트 백엔드 사용을 막는다.
- ctx는 **SSOT를 `-c 32768`로 정정**했다(차기 이미지 재빌드 시 bake). 스레드는 이 SKU에서 조정 무의미.
- 후속(별건): 성능이 필요하면 더 큰 SKU 또는 프롬프트/툴 스키마 축소, 혹은 단발 추론 용도로 한정.

## 6. 출처
- 런북: `docs/runbooks/runbook-golden-image.md`(§ 상단 ctx 주석, §1.2 `-c 32768`, §9 revision 2026-09-14), `docs/runbooks/azure-qwen-deepdive-endpoint.md`(§6).
- 코드/설정: `azure:20137133/scripts/golden_image/yearly_refresh.sh`(`-c 32768`), `scripts/lib/infra/azure_spot/{config,manager,orchestrator,tunnel,cli}.py`.
- opencode: `~/.config/opencode/opencode.json`(`azureqwen` provider).
- DB: `deepdive_steps`(session `smoke-azureqwen-20260912`), `handover.yaml` cp#106 known_issues.
- 관찰: `obs` e70e7270(ctx/E2E), e9cb6207(스레드/성능).
