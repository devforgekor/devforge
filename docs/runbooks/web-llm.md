# Runbook — Web LLM (Qwen / DeepSeek) CLI 운영

> 목적: 로그인된 웹 LLM을 CLI에서 운영하고, 대화를 수집·모델 간 공유한다.
> 관련: `~/.local/share/chrome-web-llm/README.md` (상세) · ADR-0006(웹 수집은 Phase B/D4·D5 이후)

## 구성

```
CLI(web-llm.sh) → relay(127.0.0.1:9876) → Chromium + chrome-cli-bridge 확장 → Qwen/DeepSeek 웹 UI
```

- Chromium: Playwright 번들 full Chromium(arm64) — 컨테이너 불필요.
- 프로필(로그인 세션): `~/.cache/devforge/chrome-web-llm-profile`
- 시크릿: Azure Key Vault(`web-llm.sh`가 주입)

## 기동 / 로그인

```bash
S=~/.local/share/chrome-web-llm/scripts
$S/stack-start.sh            # relay + Chromium (멱등)
$S/login-qwen.sh            # KV QWENAI-* 로그인
$S/login-deepseek.sh        # KV DEEPSEEK-* 로그인
```

## 사용 (질의·수집·공유)

```bash
$S/web-llm.sh -m qwen "질문"
$S/web-llm.sh -m deepseek -s proj "질문"           # 세션 저장(수집)
$S/web-llm.sh -m deepseek --from proj "이 대화 검토"  # 핸드오프(공유)
$S/web-llm.sh --list-sessions

# 모드(검색/사고)
$S/web-llm.sh -m qwen --search "오늘 서울 날씨"      # Qwen "Web search" 모드
$S/web-llm.sh -m deepseek --think "단계별로 풀어줘"   # DeepSeek "DeepThink"

# 셸 함수(~/.bashrc): webq=Qwen, webd=DeepSeek(사고 기본 ON), 세션=$WL_SESSION(기본 chat)
webq "질문"; WL_SESSION=proj webd "질문"; webq --search "최신 뉴스"
```

세션 파일: `~/.local/share/chrome-web-llm/conversations/<name>.jsonl`

모드 토글: Qwen=`Select Mode`(Auto / Web search / Deep Research), DeepSeek=`div.ds-toggle-button`(DeepThink / Search). 제출 전 자동 설정.

## 핵심 규칙

1. **비-헤드리스 UA**: DeepSeek은 AWS WAF가 `HeadlessChrome` UA를 차단(로그인 API 403).
   `stack-start.sh`가 기본 적용. 다른 UA는 `UA=` 환경변수.
2. **CDP 금지(운영 시)**: `--remote-debugging-port`는 확장 `chrome.debugger`와 충돌.
   `CDP=1`은 진단용.
3. **확장 수정 반영**: 프로필의 `Default/Service Worker`·`Default/Code Cache` 삭제 후 재시작.
4. **전송 방식**: Qwen=`button[aria-label=Send]` 클릭, DeepSeek=Enter(`page.key`).

## 프록시 (선택)

Dataimpulse IP 화이트리스트 사용 시 인증 없이 가능:
```bash
PROXY_SERVER=http://gw.dataimpulse.com:823 $S/stack-start.sh
```
현재는 UA 수정으로 프록시 없이 Qwen/DeepSeek 모두 동작한다.

## 트러블슈팅

| 증상 | 조치 |
|------|------|
| 로그인 403 | UA 확인(`HeadlessChrome` 금지) 후 재기동 |
| `DEBUGGER_ATTACH_FAILED` | CDP 끄고 Chromium 재시작 |
| 응답 없음 | 로딩 대기/셀렉터 확인(`page.query`) |
| 확장 미반영 | 프로필 SW/Code Cache 삭제 후 재시작 |

## 계획과의 관계

웹 LLM 대화의 **DevForge 파이프라인 수집**(ingest/provenance)은 ADR-0006에 따라
Phase B(D4 `ingest` + D5 보안) 이후 진행한다. 본 런북은 **CLI 운영(수집은 세션 파일)** 범위다.
