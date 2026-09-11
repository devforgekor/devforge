# 런북 — Azure Qwen을 devforge Deep Dive 추론 엔진으로 사용

> Status: proposed · Date: 2026-09-11 · Owner: devforge · Related: `docs/reports/control-plane-roadmap.md`, `docs/reports/mcp-consolidation-applied-20260911.md`
> 원칙: **로직·툴·상태는 devforge, Azure는 추론만.** Azure에 에이전트 로직/repo/DB를 두지 않는다.

---

## 0. 목적
Azure Spot VM의 `llama-server`(Qwen)를 **OpenAI 호환 추론 엔드포인트**로 노출하고, devforge의 Deep Dive(7단계·툴·상태)가 그 모델을 **추론 엔진으로만** 사용.

## 1. 전제 (필수)
1. **Tool-calling**: `llama-server`가 **`--jinja`** 로 기동돼야 OpenAI식 function calling 지원(검증: llama.cpp `docs/function-calling.md`). Deep Dive는 툴 사용이 필수 → **미지원이면 불가**.
2. 모델: Qwen3 계열. tool-calling 안정성 이슈 보고 있음 → **1회 스모크 필수**.
3. SSH 키(`~/.ssh/vm-azure-*-key.pem` 등)와 구독/리소스그룹(`lib/infra/azure_spot/config.py`).
4. devforge 로컬 서비스와 **포트 비충돌**(터널 로컬 포트는 `TUNNEL_PORT_BASE=8085`부터).

## 2. 절차
1. **VM 생성 + 터널**:
   ```bash
   cd /opt/projects/server/scripts
   python3.11 -m lib.infra.azure_spot.cli launch --label qwen3-30b
   # → VM 생성 → SSH/헬스 대기 → 터널 localhost:8085 → VM:8081
   ```
2. **모델 서버가 `--jinja`인지 확인**(골든 이미지/스타트업에 반영돼 있어야 함):
   ```bash
   ssh azureqwen 'systemctl cat llama-server | grep -i jinja'
   ```
3. **엔드포인트 확인**:
   ```bash
   curl -s http://127.0.0.1:8085/v1/models
   ```
4. **opencode provider 등록**(devforge 설정) — OpenAI 호환:
   ```jsonc
   // ~/.config/opencode/opencode.json (providers 예시)
   { "provider": { "azureqwen": { "npm": "@ai-sdk/openai-compatible",
       "options": { "baseURL": "http://127.0.0.1:8085/v1" },
       "models": { "qwen3-30b": {} } } } }
   ```
5. **Deep Dive 실행**(로직/툴은 devforge, 모델만 Azure):
   ```bash
   opencode run -m azureqwen/qwen3-30b "<딥다이브 대상>"
   ```
6. **정리**(비용/누수 방지):
   ```bash
   python3.11 -m lib.infra.azure_spot.cli status
   python3.11 -m lib.infra.azure_spot.cli delete <label> <vm-name>
   # 또는 orchestrator.cleanup_all()
   ```

## 3. 검증
- `/v1/models` 응답.
- **tool-calling 1회**(모델이 툴 호출 포맷을 내는지).
- Deep Dive 1단계 실제 실행 → 상태가 devforge(`deepdive_steps`·`tasks`)에 기록되는지.

## 4. 롤백 / 폴백
- **Spot eviction** 시 추론 중단 → 상태는 devforge에 보존 → **로컬 모델로 폴백** 후 재개.
- 터널/VM 정리 실패 시 `status` → orphan 식별 → `delete`.

## 5. 주의
- Azure Spot은 **회수 위험** + **비용 누수**(TTL 없음). **미구현(후속)**: VM **TTL/자동 정리(태그+정리 잡)** — 필요.
- 포트: `TUNNEL_PORT_BASE=8085`부터. 로컬 8081(embedder) 등과 충돌 금지.
- bespoke 원격 클라이언트를 만들지 말 것(표류) → **동일 하네스 + provider만 교체**.
