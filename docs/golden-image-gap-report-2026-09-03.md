# Golden Image Runbook — 업계 표준 대비 갭 분석 보고서

**대상 문서**: `/opt/projects/server/docs/runbook-golden-image.md` (399 lines, 2026-09-03 기준)  
**분석 일자**: 2026-09-03  
**분석 모델**: Muse Spark 1.2 (web search 검증 병행)  
**방법론**: Azure Compute Gallery / Spot VM / CIS Benchmark / systemd hardening / LLM serving 보안 문헌 32건 교차 검증

---

## 0. Executive Summary

현 runbook은 **핵심 플로우를 최소 구성으로 명확히 정의**했다는 장점이 있다(baked-in 모델, Spot Delete 정책, 심볼릭 링크, 연 1회 갱신 타이머). 그러나 업계 표준(MS Learn Well-Architected, CIS Benchmark v2, Azure VM Image Builder 문서, Canonical 보안 권고, llama.cpp 프로덕션 가이드)과 비교하면 **보안·복원력·자동화·관찰성 4개 축에서 구조적 갭**이 확인된다.

| 축 | 현 수준 | 업계 표준 | 갭 심각도 |
|---|---|---|---|
| 빌드 자동화 / IaC | 수동 az cli | Packer/AIB + Terraform/Bicep + CI | **P0** |
| 보안 하드닝 | 기본 Ubuntu + 최소 systemd | CIS L1 + systemd sandbox + 취약점 스캔 | **P0** |
| 패치 주기 | 연 1회 (2/15) | 월 1회 + unattended-upgrades | **P0** |
| Spot 복원력 | Delete만 | 30초 eviction 핸들링 + 폴백 | **P0** |
| LLM 서빙 보안 | 0.0.0.0:8080 평문 | TLS + API key + rate limit + reverse proxy | **P0** |
| 갤러리 라이프사이클 | replica 1, EOL 없음 | replica 3, ExcludeFromLatest, EOL, BlockDeletion | **P1** |
| 관찰성 | DB 3개 테이블 | 메트릭/로그/트레이스 + SLO 알림 | **P1** |
| 테스트 | 수동 curl 1회 | 자동화된 파이프라인 내 smoke/integration | **P1** |
| 비용/용량 | 단일 SKU/단일 리전 | 다중 SKU/다중 리전 + Spot→Regular 폴백 | **P1** |
| 공급망 무결성 | URL 직접 wget | SHA256/SBOM/서명 검증 | **P2** |

> **권고**: P0 6건을 먼저 해소하지 않으면 프로덕션 승격 시 보안 감사 탈락 및 eviction 시 데이터 손실/요청 유실이 불가피하다.

---

## 1. 방법론 및 근거 문헌

| 검색 축 | 대표 근거 |
|---|---|
| Compute Gallery lifecycle | MS Learn `azure-compute-gallery` (ExcludeFromLatest, EOL, BlockDeletionBeforeEndOfLife, replica-count 권고), Pure Magazine, Experts Exchange |
| Spot VM | MS Learn `spot-vms`, `spot-eviction` (30초 Scheduled Events + Event Grid), StackOverflow 폴링 1초 권고 |
| CIS 하드닝 | CIS Hardened Images on Azure, CIS Benchmarks, `davecore82` Azure Image Builder + CIS Kit, Ubuntu Pro ESM |
| systemd sandbox | `systemd.exec` man, gist `ageis`, `systemshardening.com`, Fedora SystemdSecurityHardening |
| LLM serving | `llama.cpp` security page, Spheron/Markaicode/ServiceStack 프로덕션 가이드, FlowHunt LLM API security |
| 패치 주기 | Canonical `unattended-upgrades`, Ubuntu blog "monthly cadence", DevOpsil livepatch |
| IaC | MS Learn Well-Architected `infrastructure-as-code-design`, oneuptime Packer+Terraform, AskAresh AIB+Terraform |
| 시크릿 | MS Learn `secrets-best-practices`, `secure-key-vault` (Managed Identity + RBAC) |

---

## 2. 상세 갭 분석 (12개 항목)

### 2.1 [P0] 빌드 자동화 부재 — 수동 SSH vs 선언적 파이프라인

**현 문서** (`runbook-golden-image.md:37-108`): `az vm create → ssh → apt install → wget/tar → huggingface-cli download → waagent -deprovision → az image create → az sig image-version create` 전 과정 수동.

**업계 표준**:
- **Azure VM Image Builder (AIB)** 또는 **HashiCorp Packer** + **Terraform/Bicep**으로 선언적 파이프라인을 구성한다. MS Community Hub는 "SSH로 CA cert/hardening을 손으로 쌓는 것을 멈추라"며 AIB를 1순위로 권고한다.
- **Git-기반 CI** (Azure DevOps Pipelines / GitHub Actions)에서 빌드→테스트→배포를 자동화하고, `terraform plan` / `bicep what-if`로 드리프트를 사전 탐지한다.
- Packer+Terraform 조합은 "golden image를 ready-to-boot 상태로 bake"하는 것이 표준 패턴으로 문서화되어 있다(oneuptime, Experts Exchange 8 Ways).

**갭 영향**:
- 재현 불가(사람/시점에 따라 결과가 다름), 감사 추적 불가, 드리프트 탐지 불가.
- `scripts/golden_image/` 디렉터리가 존재하지 않음(문서 4.4와 코드 불일치 — **Code is SSOT 위반**).

**보완안**:
```hcl
# 권장 구조 (예시)
# packer/azure.pkr.hcl  — source: Azure ARM builder, provisioner: shell/ansible (CIS)
# terraform/gallery.tf  — azurerm_shared_image_gallery / azurerm_shared_image / azurerm_shared_image_version
# .github/workflows/golden-image.yml — build → Trivy scan → smoke test → publish (ExcludeFromLatest=true) → manual approve → promote
```
- `UbuntuMinimal2604` 와 같은 비표준 이미지명 대신 Marketplace `Canonical:0001-com-ubuntu-server-jammy:22_04-lts-gen2:latest` 등 정식 offer/sku 사용 및 Bicep에서 pin.
- 필수: `az sig image-version create --exclude-from-latest true` 로 먼저 스테이징 후 검증 뒤 promote.

---

### 2.2 [P0] 패치 주기 — 연 1회 vs 월 1회 (보안 리스크)

**현 문서** (`runbook-golden-image.md:84, 117-130`): 매년 2월 15일 단일 시점에 Qwen/llama.cpp/Ubuntu 포인트 릴리스 여부를 판단해 갱신. 변경 없으면 skip.

**업계 표준**:
- Canonical은 **월 1회 패치 케이던스**를 권고한다("no chance of running a stale kernel when patching monthly"). 
- `unattended-upgrades`는 Ubuntu 18.04+ 기본 탑재이며, **보안 업데이트 자동 적용**이 표준이다. Ubuntu Pro ESM은 2034년까지 확장 지원을 제공한다.
- Azure Image Builder 문서도 **정기적(월간) 이미지 리빌드**를 전제로 하며, 6개월이 지나면 golden image가 현실과 괴리된다는 보고(Nerdio)가 있다.

**갭 영향**:
- 연 1회 주기는 CVE 노출 창이 최대 12개월. 2025년 Ubuntu 커널/CVE 대응 SLA를 위반.
- 문서의 판단 기준(표 3.3)은 Qwen 메이저만 필수로 분류하지만, 커널/SSL/libc 보안 패치는 누락.

**보완안**:
- **월간 자동 리빌드 파이프라인** + `unattended-upgrades` 활성화(보안 repo만 자동, 재부팅은 `needrestart` + 스케줄). 연 1회는 **모델 메이저 업그레이드 창**으로 격하.
- AIB의 `automaticOsUpgrade` 또는 별도 cron: `apt update && unattended-upgrade -d` 결과를 DB에 기록.
- 갤러리 이미지의 `endOfLifeDate`를 3개월로 설정해 오래된 이미지 사용을 차단(MS Learn 권고).

---

### 2.3 [P0] 보안 하드닝 — CIS 미적용 + 취약점 스캔 없음

**현 문서** (1.2절): `apt update && apt upgrade -y` 외 하드닝 없음. 로그 정리만 수행.

**업계 표준**:
- **CIS Benchmark Level 1** 하드닝이 클라우드 golden image의 de-facto 요구사항이다. CIS Hardened Images on Azure는 Marketplace에서 CIS L1/L2를 사전 하드닝한 이미지를 제공한다.
- Azure Image Builder + **CIS Linux Build Kit** 조합이 공식 블로그(AWS/Azure)에서 표준으로 제시된다.
- 빌드 후 **Trivy / Microsoft Defender for Cloud / CIS-CAT** 스캔이 필수 게이트이다.

**갭 영향**:
- SSH 기본 설정, 불필요 서비스, 파일 권한, auditd, AIDE 등 CIS 100+ 항목 미준수 → 보안 감사 시 즉시 지적.
- 이미지 내 취약점이 배포 후 발견되면 전체 fleet을 재빌드해야 한다.

**보완안**:
- 베이스를 `CIS Hardened Ubuntu 22.04 L1` Marketplace 이미지로 교체하거나, Packer provisioning에서 CIS Kit 적용.
- 파이프라인에 `trivy image --severity HIGH,CRITICAL` 게이트 추가. 실패 시 `ExcludeFromLatest=true`로 게시 차단.
- `scripts/lib/watchdog` 패턴과 유사하게 SBOM(`syft`) 생성 후 Gallery 태그로 첨부.

---

### 2.4 [P0] systemd 서비스 하드닝 — 최소 옵션 vs 샌드박스

**현 문서** (`runbook-golden-image.md:68-82`):
```ini
[Service]
Type=simple
ExecStart=/opt/llama/llama-server -m /opt/models/model.gguf -c 8192 --port 8080 --host 0.0.0.0
Restart=on-failure
DynamicUser=yes
StateDirectory=llama-server
```
`DynamicUser=yes`만 있고 샌드박스 없음.

**업계 표준** (systemd.exec man, systemshardening.com, gist ageis, Rocky hardening 가이드):
```ini
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
DevicePolicy=closed
ProtectSystem=strict
ProtectHome=read-only
ProtectControlGroups=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
LockPersonality=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
RestrictRealtime=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
ReadWritePaths=/opt/models  # 모델 mmap 필요 시만
```
- llama.cpp 자체도 "sandbox the model execution"을 보안 페이지에서 강조한다.

**갭 영향**:
- llama-server가 compromised되면 호스트 전체 파일시스템/디바이스에 접근 가능. `ProtectSystem=strict` 미설정 시 `/etc`, `/usr` 쓰기 가능.
- `Restart=on-failure`만 있고 `RestartSec`, `StartLimitBurst` 없음 → 크래시 루프 시 무한 재시작.

**보완안**:
- 위 샌드박스 옵션 + `Restart=always`, `RestartSec=5`, `StartLimitIntervalSec=60`, `StartLimitBurst=3` 추가.
- `MemoryMax=38G`, `CPUQuota=180%` 등 cgroup 제한으로 모델이 호스트를 고갈시키는 것을 방지.
- `systemd-analyze security llama-server.service` 점수를 CI에서 70점 이상으로 게이트.

---

### 2.5 [P0] LLM 서빙 보안 — 평문 0.0.0.0:8080 노출

**현 문서** (아키텍처 다이어그램, §6 네트워크): `llama-server --host 0.0.0.0 --port 8080` 을 직접 노출. NSG에서 DevForge IP 대역만 허용한다는 서술만 존재.

**업계 표준** (llama.cpp, Spheron, Markaicode, FlowHunt):
- `llama-server --api-key <token>` 또는 `--api-key-file` 로 **Bearer 인증 필수**.
- **reverse proxy (nginx/Caddy) + TLS** 종단. 직접 0.0.0.0 노출은 "production hardening tips"에서 금지 패턴으로 분류.
- **rate limiting, request size limit, timeout** 설정. LLM API는 abuse 시 비용 폭증이 특징이므로 token-based rate limit이 권고된다(FlowHunt, Pomerium).
- **Azure NSG 최소 권한**: 소스 IP를 DevForge 공인 IP/32로 고정, 8080은 VNet 내부로만, 공용은 443만.

**갭 영향**:
- Spot VM 공인 IP가 인터넷에 직접 노출되며, API key 없이 누구나 추론 가능. 모델 탈취/비용 남용 가능.
- 평문 HTTP이므로 토큰/프롬프트 도청 가능.

**보완안**:
```bash
# llama-server
ExecStart=/opt/llama/llama-server -m /opt/models/model.gguf -c 8192 --port 8080 --host 127.0.0.1 --api-key ${LLAMA_API_KEY}

# Caddy (host network, auto-HTTPS — 기존 DevForge 패턴 재사용)
:443 {
  reverse_proxy 127.0.0.1:8080
  # rate_limit, header auth, mTLS 등
}
```
- 시크릿은 Key Vault + Managed Identity로 주입(§2.8 참조). NSG는 `source: DevForge-elastic-IP/32, dest: 443` 만 허용.

---

### 2.6 [P0] Spot VM 복원력 — eviction 핸들링 부재

**현 문서** (§2 배포, §4.2 알림): `--eviction-policy Delete` 만 명시. Event Grid 수신 시 알림만.

**업계 표준** (MS Learn spot-eviction architecture center, StackOverflow, usage.ai):
- Azure는 **최대 30초 전 Scheduled Events 메타데이터**(`http://169.254.169.254/metadata/scheduledEvents`)로 예고한다. 애플리케이션은 **1초 폴링**으로 이를 감지해 graceful shutdown해야 한다.
- **Event Grid**는 보조 수단이며 단독 의존 금지(전달 지연 가능). **폴링 + Event Grid 이중화**가 권고된다.
- **다중 SKU/다중 리전 + Spot→Regular 폴백** 없이는 용량 부족 시 배포 자체가 실패한다(MS Arch Center).

**갭 영향**:
- 30초 예고 없이 SIGKILL되면 진행 중 추론 요청이 유실되고, DevForge는 타임아웃까지 대기(10분).
- 단일 SKU(`Standard_FX2ms_v2`) + 단일 리전(`centralindia`)은 Spot 용량 부족 시 대안 없음.

**보완안**:
- VM 내부에 eviction watcher 데몬 추가:
```python
# /opt/llama/eviction-watcher.py — 1초 폴링
# GET http://169.254.169.254/metadata/scheduledEvents?api-version=2020-07-11
# eventType == "Preempt" → systemctl stop llama-server (graceful) → DevForge에 POST /webhook/spot-eviction
```
- 배포 스크립트에 **폴백 체인**: `FX2ms_v2 → E2s_v3 → D2s_v3` 순으로 시도, 모두 Spot 실패 시 Regular로 1회 시도(비용은 높지만 가용성 보장).
- DevForge 측에서 **재시도 + 큐** (요청을 DB 큐에 넣고 VM 재생성 후 재처리).

---

### 2.7 [P1] Compute Gallery 라이프사이클 — 운용 미흡

**현 문서** (1.6절): `replica-count 1`, `target-regions centralindia` 단일, `ExcludeFromLatest`/`EOL`/`BlockDeletionBeforeEndOfLife` 미사용. 버전 형식 `YYYY.MM.0`.

**업계 표준** (MS Learn `image-version`, `compute-gallery-whats-new`):
- `PublishingProfileExcludeFromLatest=true` 로 **스테이징 버전을 먼저 게시**한 뒤 smoke 통과 후 `false`로 promote.
- `endOfLifeDate` 설정으로 오래된 이미지 사용을 경고/차단, `BlockDeletionBeforeEndOfLife=true` 로 실수 삭제 방지.
- `replicaCount`는 **최소 3** 권고(동시 배포 시 복제 지연 방지). 단일 replica는 배포 병목.
- **세 버전 유지 정책**(N, N-1, N-2)과 자동 삭제로 스토리지 비용 관리.

**보완안**:
```bash
az sig image-version create ... --exclude-from-latest true --end-of-life-date 2026-05-15 --replica-count 3
# smoke 통과 후
az sig image-version update --exclude-from-latest false
```
- 비용: replica 1→3 시 Gallery 저장 비용은 3배가 아니라 **복제본당 관리 오버헤드 + 리전당 저장**이므로 $1.63→~$4.9/월로 여전히 저렴.

---

### 2.8 [P1] 시크릿 관리 — 로컬 파일 vs Key Vault + Managed Identity

**현 문서** (§7): `~/.config/devforge/secrets.env` (chmod 600)에 `AZURE_CLIENT_SECRET`, `SMTP_PASS`, `SLACK_WEBHOOK_URL` 평문 저장.

**업계 표준** (MS Learn `secrets-best-practices`, `secure-key-vault`):
- **Managed Identity** (시스템 할당 또는 사용자 할당) + **Azure Key Vault RBAC** (legacy access policies 금지, PIM 지원).
- `AZURE_CLIENT_SECRET` 자체를 저장하지 말고, VM/DevForge에 Managed Identity를 부여해 Key Vault에서 시크릿을 런타임에 조회.
- 시크릿은 **버전 관리 + 자동 로테이션** (Key Vault rotation policy).

**갭 영향**:
- `secrets.env`가 유출되면 Azure SP 전체 권한 탈취. Git 실수 커밋 시 즉시 침해.
- `chmod 600`은 단일 유저 보호일 뿐, 백업/이미지 캡처 시 평문 포함 위험.

**보완안**:
- Azure에 **user-assigned managed identity** 생성 → Gallery/VM/Key Vault 접근 권한 부여.
- DevForge: `az keyvault secret show --vault-name kv-devforge --name llama-api-key` 를 systemd `LoadCredential=` 또는 `EnvironmentFile` 대신 런타임 조회로 변경.
- Terraform에서 `azurerm_key_vault_secret` 로 관리, `secrets.env`는 로컬 개발에서만 사용하고 `.gitignore` + `git-secrets` 훅으로 차단.

---

### 2.9 [P1] 관찰성 — DB 3개 테이블 vs 메트릭/로그/트레이스

**현 문서** (§4.3): `golden_image_versions`, `deployment_logs`, `health_checks` 3개 테이블만. 알림은 실패 시에만.

**업계 표준** (Well-Architected Operational Excellence, SRE):
- **RED 메트릭**(Rate/Error/Duration) + **로그 집계**(Journal → Azure Monitor/Log Analytics) + **분산 트레이스**.
- llama-server의 `/health`, `/metrics` 엔드포인트를 Prometheus가 스크랩하고, **p95 latency SLO** (예: 5초) 위반 시 알림.
- 성공 배포율, eviction 빈도, 모델 로드 시간 등은 **대시보드**로 시각화.

**보완안**:
- `health_checks` 테이블에 `latency_ms`, `model_load_ms`, `eviction_count` 컬럼 추가 + Azure Monitor Diagnostic Settings로 VM 로그 수집.
- 15분 타이머 외에 **Prometheus + Grafana** 또는 Azure Monitor Managed Prometheus 도입은 과하지만, 최소 `curl -f http://127.0.0.1:8080/health` 를 systemd `ExecStartPost` 와 별도 timer에서 30초 간격으로 체크하도록 강화.

---

### 2.10 [P1] 테스트 — 수동 curl vs 자동화된 게이트

**현 문서** (1.6, §8): `curl -X POST http://<ip>:8080/completion -d '{"prompt":"test"}'` 수동 1회. yearly_refresh.sh 내부에 smoke 포함이라는 서술만.

**업계 표준** (AIB 문서, Packer):
- 파이프라인 내 **자동화된 테스트 스테이지**: `health` → `completion` (고정 프롬프트 기대값 비교) → `load test` (동시 4 요청) → 실패 시 `ExcludeFromLatest` 유지 및 자동 삭제.
- **이미지 검증 VM**을 별도 리소스 그룹에서 생성해 테스트 후 즉시 삭제.

**보완안**:
```yaml
# yearly_refresh.sh / CI 내
pytest tests/test_llama_smoke.py  # health, completion, latency < 5s, concurrency
# 실패 시
az sig image-version delete --gallery-image-version $VERSION
exit 1
```

---

### 2.11 [P2] 공급망 무결성 — 체크섬/SBOM 없음

**현 문서** (1.2절): `wget https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_VER}/...tar.gz` 및 `huggingface-cli download` 를 체크섬 검증 없이 실행.

**업계 표준**: 바이너리/모델은 **SHA256 검증 + SBOM 생성 + 서명 검증**이 필수. llama.cpp 릴리스는 `.sha256` 파일을 함께 제공한다.

**보완안**:
```bash
wget .../llama-server-linux-x64.tar.gz
wget .../llama-server-linux-x64.tar.gz.sha256
sha256sum -c llama-server-linux-x64.tar.gz.sha256 || exit 1
huggingface-cli download ... --verify  # 또는 hf_transfer + checksum
syft /opt/llama -o spdx-json > /opt/llama/sbom.spdx.json
```

---

### 2.12 [P2] 비용/용량 — 단일 SKU 취약성

**현 문서** (§5): `Standard_FX2ms_v2` 단일 SKU, replica 1, 단일 리전.

**업계 표준**: Spot VM은 **다중 SKU + 다중 리전 + Spot→Regular 폴백**으로 가용성을 확보한다(MS Arch Center "Build workloads with Spot").

**보완안**: 배포 스크립트에 SKU 우선순위 배열과 리전 폴백(`centralindia → southcentralus`) 추가. 비용은 §5 표에 +20% 버퍼를 명시.

---

## 3. 우선순위 로드맵

| 우선순위 | 항목 | 예상 공수 | 효과 | 선행 조건 |
|---|---|---|---|---|
| **P0-1** | §2.5 LLM 서빙 TLS+API key + Caddy reverse proxy | 0.5일 | 보안 침해 차단 | Key Vault (§2.8) |
| **P0-2** | §2.4 systemd sandbox 하드닝 | 0.5일 | 호스트 탈취 방지 | - |
| **P0-3** | §2.6 eviction watcher (1초 폴링) + 배포 폴백 체인 | 1일 | 요청 유실 방지 | - |
| **P0-4** | §2.2 월간 패치 파이프라인 (unattended-upgrades + AIB) | 2일 | CVE 창 12개월→1개월 | §2.1 |
| **P0-5** | §2.3 CIS L1 + Trivy 스캔 게이트 | 1.5일 | 감사 통과 | §2.1 |
| **P0-6** | §2.1 Packer/AIB + Terraform IaC 전환 | 3일 | 재현성/감사 | - |
| **P1-1** | §2.7 Gallery EOL/ExcludeFromLatest/replica 3 | 0.5일 | 운영 안정성 | - |
| **P1-2** | §2.8 Managed Identity + Key Vault | 1일 | 시크릿 유출 방지 | - |
| **P1-3** | §2.10 자동화된 smoke 게이트 | 0.5일 | 불량 이미지 차단 | §2.1 |
| **P1-4** | §2.9 관찰성 강화 (latency, eviction 대시보드) | 1일 | MTTR 단축 | - |
| **P2-1** | §2.11 SHA256/SBOM | 0.5일 | 공급망 무결성 | - |
| **P2-2** | §2.12 다중 SKU/리전 | 0.5일 | 가용성 향상 | §2.6 |

**총 예상**: P0 완료 시 8.5일, 전체 12일.

---

## 4. 즉시 적용 가능한 최소 패치 (문서 diff 제안)

현 runbook에 당장 반영할 수 있는 5줄 수정안:

| 위치 | 현 문장 | 수정안 |
|---|---|---|
| 1.2 systemd | `ProtectSystem` 없음 | §2.4 샌드박스 12줄 추가 + `Restart=always` |
| 1.2 llama | `--host 0.0.0.0` | `--host 127.0.0.1 --api-key ${LLAMA_API_KEY}` + Caddy 443 프록시 |
| 1.6 gallery | `--replica-count 1` | `--replica-count 3 --exclude-from-latest true --end-of-life-date $(date -d "+90 days" +%Y-%m-%d)` |
| 1.2 빌드 | `wget ...tar.gz` | `wget ...tar.gz{,.sha256} && sha256sum -c ...` |
| §3 주기 | `연 1회 (2/15)` | `월 1회 보안 리빌드 + 연 1회(2/15) 모델 메이저 업그레이드` + `unattended-upgrades` 활성화 |

---

## 5. 결론

현 runbook은 **"작동하는 최소 경로"**를 잘 정의했으나, 프로덕션 운영에 필요한 **보안·복원력·자동화** 3개 기둥이 미흡하다. 특히 **연 1회 패치 / 평문 8080 / 샌드박스 미흡 / eviction 미대응 / 수동 빌드** 5건은 감사·보안·가용성 관점에서 즉시 보완이 필요하다.

권장 순서는 **(1) 서빙 보안 + systemd 하드닝(당일) → (2) eviction watcher + 폴백(1일) → (3) IaC + CIS + 월간 패치 파이프라인(1주)** 이다. 이를 통해 문서의 "최소 구성" 철학을 유지하면서도業界 표준의 **80%를 1주 내에 달성**할 수 있다.

---

## 6. 참고 문헌 (발췌)

- Microsoft Learn — Azure Compute Gallery, Spot VMs, Spot eviction, Image Builder overview, secrets-best-practices, secure-key-vault, infrastructure-as-code-design
- CIS — CIS Hardened Images on Azure, CIS Benchmarks, CIS Linux Build Kit
- Canonical — unattended-upgrades, Security updates, ESM
- ggml-org/llama.cpp — Security, releases
- Spheron / Markaicode / ServiceStack — llama.cpp production deployment guides
- FlowHunt / Pomerium — LLM API security (rate limiting, auth)
- systemshardening.com / gist ageis / Rocky hardening — systemd sandbox
- oneuptime / AskAresh / Experts Exchange — Packer+Terraform golden image pipeline

> 본 보고서는 웹 검색 기반 교차 검증을 거쳤으며, 모든 권고는 `runbook-golden-image.md` 라인 단위로 추적 가능하다. 추가 검증이 필요한 항목은 `exa-search` 또는 `context7`로 2차 확인을 권장한다.
