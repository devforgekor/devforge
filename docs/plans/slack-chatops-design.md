# DevForge Slack ChatOps — 도입 상세 설계서

> **작성일**: 2026-06-09  
> **상태**: 기획/설계 (미구현)  
> **대상 파일**: `scripts/slack/` (신규 모듈)

---

## 개요

Slack을 단순 알림 채널이 아닌 **서버 운영을 위한 대화형 인터페이스**로 전환.  
3가지 기능을 단계적으로 도입:

| # | 기능 | 난이도 | 설명 |
|---|------|--------|------|
| 1 | `/status` | 하 | 실시간 서버 상태 조회 (Block Kit 포맷, watchdog heartbeat 재사용) |
| 2 | `/investigate` | 중 | Qwen AI 에이전트에 질의/지시 (bot_processor.py 재사용) |
| 3 | `/deploy` | 중 | 모드 전환/서비스 재시작 등 운영 명령 실행 |

---

## 1. `/status` — 서버 상태 조회

### 목적
Slack에서 `/status` 명령 하나로 모든 서버 상태를 즉시 확인.  
watchdog heartbeat와 동일한 상태 정보를 **on-demand**로 조회.

### 데이터 흐름

```
Slack 명령 → Slack API (POST) → Flask/Simple HTTP 서버
  → cli.py status --json 호출 → 상태 수집
  → _build_heartbeat_blocks() → Block Kit JSON 응답
  → Slack response_url로 POST
```

### 상세 구현

#### Slack Apps 셋업 (필수 선행)

1. [api.slack.com/apps](https://api.slack.com/apps)에서 새 앱 생성
2. **Slash Commands** → `/status` 등록
   - Request URL: `https://devforge.example.com/slack/commands`
   - Description: "서버 상태 확인"
   - Usage Hint: "[pod-a | pod-b | all]"
3. **OAuth & Permissions** → `commands`, `chat:write`, `chat:write.public` 스코프 추가
4. Bot Token을 `~/.config/devforge/secrets.env`에 `SLACK_BOT_TOKEN`으로 저장 (이미 존재)

#### Slack 명령 수신 서버

기존 인프라와의 정합성을 위해 **단일 HTTP 엔드포인트**로 모든 Slack 상호작용 처리.

**신규 파일**: `scripts/slack/slack_app.py`

```python
#!/usr/bin/env python3
# Status: experimental
# Path: systemd:devforge-slack-app.service
"""Slack App — Slash command + Event handler for ChatOps.

Flask 기반 HTTP 서버, Caddy reverse proxy로 /slack/* 라우팅.
Slash command: /status, /investigate, /deploy
Events: message.im (AI agent)

Usage:
  python3 slack_app.py          # 127.0.0.1:9999
  python3 slack_app.py --port 9999
"""
```

**필요 의존성**: Flask (`pip install Flask`), 나머지는 전부 기존 모듈 재사용

#### 슬래시 명령 핸들러 구조

```python
from flask import Flask, request, jsonify
import subprocess, json, sys
sys.path.insert(0, "/opt/projects/server/scripts")
from lib.watchdog.notifier import _build_heartbeat_blocks, _slack_api, KST

app = Flask(__name__)

@app.route("/slack/commands", methods=["POST"])
def handle_command():
    data = request.form
    cmd = data.get("command")       # e.g. "/status"
    text = data.get("text", "")      # e.g. "pod-a"
    response_url = data.get("response_url")
    user = data.get("user_name")
    
    if cmd == "/status":
        return handle_status(text, response_url)
    elif cmd == "/investigate":
        return handle_investigate(text, response_url, user)
    elif cmd == "/deploy":
        return handle_deploy(text, response_url, user)
    return "", 200
```

#### `/status` 핸들러 상세

```python
def handle_status(args: str, response_url: str):
    """/status [pod-a | pod-b | system | all]"""
    # 1. cli.py status --json 호출
    r = subprocess.run(
        ["python3", "/opt/projects/server/scripts/cli.py", "status", "--json"],
        capture_output=True, text=True, timeout=15
    )
    state = json.loads(r.stdout)
    
    # 2. watchdog notifier의 Block Kit 빌더 재사용
    summary = build_status_summary(state, filter=args)
    blocks, fallback = _build_heartbeat_blocks(summary)
    
    # 3. response_url에 지연 응답 (3초 내 응답 필요 없음)
    import requests
    requests.post(response_url, json={
        "text": fallback,
        "blocks": blocks,
        "response_type": "ephemeral"  # 나만 보기
    })
    return "", 200  # immediate ack

def build_status_summary(state: dict, filter: str = "") -> dict:
    """cli.py status --json → heartbeat summary 변환"""
    summary = {
        "mode": state.get("mode", "?"),
        "experiment_active": bool(state.get("experiments", [])),
        "containers": [],
        "services": [],
        "timers": [],
        "memory": state.get("resources", {}).get("memory", {}),
        "probes": [],
        "metrics": state.get("llm_metrics", {}),
        "slots": state.get("llm_slots", {}),
        "events_30m": [],
    }
    # 상태 데이터 매핑 생략 (watchdog __init__.py 체크 로직 참조)
    return summary
```

#### Slack 응답 유형

| 옵션 | 효과 |
|------|------|
| `response_type: ephemeral` | 명령 실행자만 볼 수 있음 (추천) |
| `response_type: in_channel` | 채널 전체에 공개 |

#### Caddy 라우팅 추가

현재 Caddyfile에 `/slack/*` 엔드포인트 추가:

```
devforge.example.com {
    route /slack/* {
        reverse_proxy 127.0.0.1:9999
    }
    # 기존 라우팅...
}
```

#### systemd 서비스 유닛

**신규 파일**: `~/.config/containers/systemd/devforge-slack-app.service` (Rootless Quadlet)

```ini
[Unit]
Description=DevForge Slack App — ChatOps commands
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/projects/server/scripts/slack/slack_app.py
Restart=on-failure
RestartSec=5
Type=simple

[Install]
WantedBy=default.target
```

---

## 2. `/investigate` — AI Agent (Qwen)

### 목적
Slack에서 AI 에이전트에게 자연어로 질문/지시.  
기존 `bot_processor.py` (Qwen2.5-Coder-3B)를 Slack에서도 사용 가능하게 함.

### Telegram ↔ Slack 공유 로직

```
사용자 메시지
    ├── Telegram → telegram_bot.py → bot_processor.process(text)
    └── Slack    → slack_app.py     → bot_processor.process(text)  ← 동일!
```

**핵심**: `bot_processor.py`는 Telegram/Slack 양쪽에서 동일하게 재사용.  
새로운 AI 로직 불필요.

### 데이터 흐름

```
/servstat → Slack Slash Command 수신
  → intent 분류 (Qwen 3B) → status/shell/log/mode/chat
  → 응답을 Slack response_url로 POST (mrkdwn 포맷)
```

### Intent별 처리 (bot_processor.py 참조)

| Intent | 동작 | 출력 |
|--------|------|------|
| `status` | `_exec_status()` — Pod 모드 + 메모리 + 컨테이너 | Block Kit fields |
| `shell` | `!명령어` → `subprocess.run()` | 코드 블록 |
| `log` | `journalctl --user -n 20` | 코드 블록 |
| `mode` | 현재 운영 모드 | 텍스트 |
| `chat` | Qwen 3B 대화 (CMD: protocol 포함) | 마크다운 |

### 구현

#### Slash command 등록

- 명령어: `/investigate <질문>`
- Request URL: `https://devforge.example.com/slack/commands`
- 설명: "AI 에이전트에게 서버 관련 질문/지시"

#### 핸들러

```python
def handle_investigate(text: str, response_url: str, user: str):
    """Qwen AI agent에 질의 후 결과 반환"""
    if not text.strip():
        return jsonify({"text": "질문을 입력하세요. 예: `/investigate 지금 상태 어때?`"})
    
    # bot_processor 재사용
    from scripts.notice.lib.bot_processor import process, classify_intent
    
    # intent 분류 (Qwen 3B 호출)
    intent = classify_intent(text)
    
    # 처리 (최대 30초)
    result = process(text)
    
    # 지연 응답
    requests.post(response_url, json={
        "text": result,
        "response_type": "ephemeral"
    })
    return "", 200
```

#### Slack 포맷 변환

`bot_processor.py`는 현재 HTML 태그(`<b>`, `<code>`)를 반환함.  
Slack Block Kit에서는 mrkdwn 형식으로 변환 필요:

| bot_processor 출력 | Slack 변환 |
|-------------------|-----------|
| `<b>텍스트</b>` | `*텍스트*` |
| `<code>값</code>` | `` `값` `` |
| `CMD: 명령어` | 그대로 (shell 실행 결과만 반환) |

**Slack 전용 포맷터 함수 추가** (`bot_processor.py` 수정):

```python
def _to_slack_mrkdwn(html_text: str) -> str:
    """HTML 태그를 Slack mrkdwn으로 변환"""
    text = html_text.replace("<b>", "*").replace("</b>", "*")
    text = text.replace("<code>", "`").replace("</code>", "`")
    text = text.replace("<i>", "_").replace("</i>", "_")
    return text
```

### 제약사항

| 항목 | 내용 |
|------|------|
| 최대 처리 시간 | 30초 (Slack 3초 immediate ack + 30분 지연 응답 가능) |
| 응답 길이 | 4000자 제한 |
| Qwen 3B 가용성 | Pod A (:8082)가 실행 중일 때만 동작. Pod A down시 오류 메시지 반환 |
| 동시 요청 | Flask 기본 single-thread, queue 없음 → 초과 시 순차 처리 |

---

## 3. `/deploy` — 서버 운영 명령 (ChatOps)

### 목적
Slack에서 직접 서버 운영 작업 실행.  
SSH 접속 없이 채팅 한 줄로 처리.

### 지원 명령 목록

| 명령어 | 동작 | 권한 |
|--------|------|------|
| `/deploy mode day` | day 모드 전환 | admin |
| `/deploy mode night` | night 모드 전환 | admin |
| `/deploy restart pod-a` | Pod A 컨테이너 재시작 | admin |
| `/deploy restart watchdog` | watchdog 재시작 | admin |
| `/deploy restart turn-watcher` | turn-watcher 재시작 | admin |
| `/deploy kick 15m-cycle` | 15m-cycle 타이머 강제 실행 | admin |
| `/deploy list` | 실행 가능한 명령 목록 | all |

### 데이터 흐름

```
/deploy mode day → Slack Slash Command
  → payload_url 검증 → 서브프로세스 실행
  → systemctl --user set-environment MODE=day
  → systemctl --user start devforge-pod-a 등
  → 결과 → Slack response_url
```

### 권한 시스템

**간단한 rol-based 접근 제어** (복잡한 인증은 불필요):

```python
DEPLOY_ALLOWED_USERS = {"U0APJGD8CBW"}  # Slack User ID (본인)

def is_allowed(user_id: str) -> bool:
    return user_id in DEPLOY_ALLOWED_USERS
```

운영 명령은 항상 `ephemeral`로 응답 (다른 사용자에게 노출 방지).

### 핸들러

```python
import subprocess

DEPLOY_COMMANDS = {
    "mode": {
        "day": lambda: _set_mode("day"),
        "night": lambda: _set_mode("night"),
    },
    "restart": {
        "pod-a": lambda: _restart_service("container-devforge-pod-a"),
        "pod-b": lambda: _restart_service("container-devforge-pod-b"),
        "watchdog": lambda: _restart_service("devforge-watchdog"),
        "turn-watcher": lambda: _restart_service("devforge-turn-watcher"),
    },
    "kick": {
        "15m-cycle": lambda: _kick_timer("devforge-15m-cycle"),
        "nightly": lambda: _kick_timer("devforge-night-cycle"),
        "daily-structure": lambda: _kick_timer("devforge-daily-structure"),
    },
    "list": lambda: _list_commands(),
}

def _set_mode(mode: str) -> str:
    """운영 모드 전환 (day ↔ night)"""
    # 1. MODE 파일 기록
    Path("/opt/ai_data/scripts/current-system-mode.env").write_text(f"MODE={mode}\n")
    
    # 2. Pod mode 파일 기록
    if mode == "night":
        _write_pod_mode("a", "review")
        _write_pod_mode("b", "night")
        # Night 모드에 필요한 서비스 시작
        subprocess.run(["systemctl", "--user", "start", "container-devforge-pod-a"], timeout=30)
    else:
        _write_pod_mode("a", "day")
        _write_pod_mode("b", "day")
        # Pod A는 day 모드 포트로 전환 필요
    
    return f"모드 전환 완료: {mode}"

def _restart_service(name: str) -> str:
    """systemd 서비스 재시작"""
    r = subprocess.run(
        ["systemctl", "--user", "restart", name],
        capture_output=True, text=True, timeout=30
    )
    if r.returncode == 0:
        return f"{name} 재시작 완료"
    return f"{name} 재시작 실패: {r.stderr.strip()[:200]}"

def _kick_timer(name: str) -> str:
    """타이머 강제 실행"""
    svc_name = name.replace(".timer", ".service")
    r = subprocess.run(
        ["systemctl", "--user", "start", svc_name],
        capture_output=True, text=True, timeout=30
    )
    if r.returncode == 0:
        return f"{svc_name} 실행 완료"
    return f"{svc_name} 실행 실패: {r.stderr.strip()[:200]}"
```

### 안전장치

1. **권한 체크**: 허용된 사용자만 실행 가능
2. **확인 프롬프트**: 위험한 명령은 두 번 확인
   ```
   /deploy restart pod-a
   → "Pod A를 재시작하시겠습니까? (되돌릴 수 없음) [확인: /deploy confirm restart-pod-a]"
   ```
3. **실패 시 롤백 지침**: 오류 메시지에 복구 방법 포함
4. **감사 로그**: 모든 명령을 `worklog_entries` DB에 기록 (옵션)

### 확인 패턴 (선택사항)

위험도가 높은 명령은 2단계 확인:

```python
_pending_confirm = {}  # user_id -> {"action": ..., "expires": time}

@app.route("/slack/interactions", methods=["POST"])
def handle_interaction():
    """Block Kit 버튼交互 처리"""
    payload = json.loads(request.form["payload"])
    action = payload["actions"][0]["value"]
    user = payload["user"]["id"]
    
    if action.startswith("confirm:"):
        cmd = action.split(":", 1)[1]
        _pending_confirm[user] = {"cmd": cmd, "expires": time.time() + 30}
        return jsonify({"text": f"`/deploy confirm` 를 30초 내에 입력하세요."})
```

---

## Slack App 구성 요약

### Slack Apps 설정

| 항목 | 값 |
|------|-----|
| App Name | DevForge Bot |
| Slash Command 1 | `/status` → `https://devforge.example.com/slack/commands` |
| Slash Command 2 | `/investigate` → `https://devforge.example.com/slack/commands` |
| Slash Command 3 | `/deploy` → `https://devforge.example.com/slack/commands` |
| Bot Token Scope | `commands`, `chat:write` |
| Socket Mode | 불필요 (HTTP endpoint + Caddy) |

### 필요 secrets.env 항목

```
SLACK_BOT_TOKEN=xoxb-...  (기존)
SLACK_CHANNEL=U0APJGD8CBW  (기존)
```

추가 항목 불필요 — 기존 토큰 그대로 사용.

---

## 파일 변경/신규 목록

| 파일 | 유형 | 설명 |
|------|------|------|
| `scripts/slack/__init__.py` | 신규 | 패키지 |
| `scripts/slack/slack_app.py` | 신규 | Flask 앱 — 슬래시 명령 + 이벤트 처리 |
| `scripts/slack/deploy.py` | 신규 | `/deploy` 명령 처리 + 권한 + 서브프로세스 |
| `scripts/slack/status.py` | 신규 | `/status` — cli.py → Block Kit 변환 |
| `scripts/slack/investigate.py` | 신규 | `/investigate` — bot_processor 래퍼 + mrkdwn 변환 |
| `scripts/notice/lib/bot_processor.py` | 수정 | `_to_slack_mrkdwn()` 함수 추가 (선택) |
| `~/.config/containers/systemd/devforge-slack-app.service` | 신규 | systemd Quadlet |
| Caddyfile | 수정 | `/slack/*` → `127.0.0.1:9999` reverse proxy |

---

## 단계별 도입 로드맵

### Phase 1: `/status` (예상: 1일)

1. `slack_app.py` 기본 Flask 앱 작성
2. `status.py` — `cli.py status --json` 호출 → `_build_heartbeat_blocks()` 변환
3. Slack Apps 설정 (Slash Command 등록)
4. Caddy 라우팅 추가
5. systemd 서비스 등록 및 시작
6. 테스트: `/status` → Block Kit 응답 확인

### Phase 2: `/investigate` (예상: 1일)

1. `investigate.py` — `bot_processor.process()` 슬랙 포맷 래퍼
2. Slack Apps에 `/investigate` 명령 등록
3. Qwen 3B (:8082) 가용성 확인
4. 테스트: `/investigate 지금 상태 어때?` → AI 응답 확인

### Phase 3: `/deploy` (예상: 2일)

1. `deploy.py` — 명령 파서 + 권한 체크 + 서브프로세스
2. `_set_mode()`, `_restart_service()`, `_kick_timer()` 구현
3. Slack Apps에 `/deploy` 명령 등록
4. 안전장치 (권한, 확인 패턴, 감사 로그) 구현
5. 테스트: `/deploy mode night` → 모드 전환 확인

---

## 보안 고려사항

| 위험 | 대책 |
|------|------|
| 비인가자 명령 실행 | Slack User ID whitelist (현재는 1명) |
| 명령 인젝션 | `shlex.quote()` 모든 사용자 입력 적용 |
| Pod A 강제 재시작 중 손실 | 실행 전 상태 백업 + graceful shutdown 확인 |
| mode 전환 중복 실행 | 실행 중 플래그로 중복 방지 |
| Slack token 노출 | `secrets.env`에서만 읽음, 하드코딩 금지 |
| SSL/TLS | Caddy auto-HTTPS가 처리 (기존 인프라 활용) |

---

## 기존 인프라와의 관계

```
기존:                          신규 (Slack ChatOps):
┌─────────────┐               ┌──────────────────┐
│ watchdog     │──heartbeat──→│ Slack Channel    │
│ (notifier.py)│              │                  │
└─────────────┘              │ \status → 서버   │
                              │ /investigate→AI  │
┌─────────────┐               │ /deploy → 실행   │
│ bot_procesor │←──Telegram──│                  │
│ (Qwen 3B)   │              └──────────────────┘
└──────┬──────┘
       │ ←── (신규: Slack에서도 동일 bot_processor 호출)
       │
┌──────┴──────┐
│ cli.py     │←── /status → cli.py status --json
│ status     │
└────────────┘
```

- Telegram 봇과 완전히 독립적 (한쪽이 막혀도 다른 쪽은 동작)
- 단, `bot_processor`의 Qwen 3B는 Pod A 자원 공유 (경합 발생 가능)
- 상태 정보는 `cli.py status --json` → 실시간 수집 (watchdog와 동일 소스)

---

## 결정 포인트 (구현 전 확정 필요)

1. `slack_sdk` 설치 vs `urllib` 유지
   - urllib만으로 Slash command 응답은 가능하나, **Block Kit 버튼 인터랙션**은 `response_url` + urllib로 충분
   - **제안**: urllib 유지 (신규 의존성 최소화)

2. Flask vs FastAPI
   - Flask가 더 단순하고 `pip install Flask` 하나면 동작
   - **제안**: Flask

3. Slash command vs Socket Mode
   - Socket Mode는 public URL 불필요하나 WebSocket 연결 유지 필요
   - 현재 Caddy로 public HTTPS 가능 → HTTP endpoint가 더 단순
   - **제안**: HTTP (Caddy reverse proxy)

4. `/deploy`에 2단계 확인 필요 여부
   - pod-a 재시작은 서비스 중단 발생 → 확인 필요
   - mode 전환은 즉시 효과 → 확인 불필요
   - **제안**: restart 계열만 확인, mode/kick은 즉시 실행

---

## 측정 지표

도입 후 다음 항목을 관찰:

| 지표 | 측정 방법 | 목표 |
|------|----------|------|
| `/status` 호출 횟수 | Slack App analytics | 주 5회 이상 |
| AI agent 응답 시간 | 로그 | 10초 이내 |
| `/deploy` 실행 성공률 | worklog 분석 | 95% 이상 |
| SSH 접속 감소율 | `/var/log/secure` | 30% 감소 |
