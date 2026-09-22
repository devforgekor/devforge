# AGENTS.md 동기화 재설계 + 무결성 복원 — 설계·실행 가이드

- 작성: 2026-09-22 (KST)
- 상태: **SUPERSEDED (2026-09-22)** — Option B(수동 canonical 병합 + `agent_docs` 포인터)로 대체됨.
  Phase 1(재생성)/Phase 2(생성기 + `.path`)는 **기각** (수동 파일이면 재생성 diff가 존재하지 않음).
  Phase 3(미등록 타이머 편입)은 **완료 (2026-09-22, commit `a35af00`)** — §5 참조. 상세 결론: `docs/reports/agent-rules-content-audit-20260922.md` §4.
- 선행 보고서: `docs/reports/agent-rules-analysis-20260922.md` (Phase 2 제안도 본 문서 결정으로 기각)
- 범위: Phase 1(무결성 복원) · Phase 2(재발 방지: 생성기 + `.path`) · Phase 3(미등록 타이머 편입)

---

## 0. 검증된 사실 (설계 전제)

실제 파일/git으로 재현 확인한 값만 사용한다.

| # | 사실 | 근거 |
|---|------|------|
| F1 | `sync_gemini_rules.py`는 커밋 `bbd0589`(2026-09-15)에서 삭제, 파일 부재 | `git log --diff-filter=D` |
| F2 | `AGENTS.md` = `head -7` + `infrastructure.md` + `llm-common-rule.md` + `llm-agent-rule.md` (재현 diff 4줄) | §부록 B-1 |
| F3 | 드리프트 4줄: `AGENTS.md:9,63`(뒤짐), `:226,228`(앞섬) | 동일 |
| F4 | `CLAUDE.md` = `@./infrastructure.md` + `@./llm-common-rule.md` + `@./llm-agent-rule.md` + 라우팅 | 파일 확인 |
| F5 | `.claude/CLAUDE.md` = `@/home/opc/CLAUDE.md`, `server/CLAUDE.md` = `@/home/opc/CLAUDE.md` | 파일 확인 |
| F6 | `copilot-instructions.md`는 **어디에도 없음**. `~/.copilot/`은 존재(instructions 없음) | `find` |
| F7 | `/home/opc/infrastructure.md:40-41` ≠ 정본 `docs/architecture/infrastructure.md:40` | `diff` |
| F8 | `code-structure.yaml:150`에 삭제된 `sync_gemini_rules.py` 잔존(`status: experimental`) | `grep` |
| F9 | `AGENTS.md` 권한 `600`, `CLAUDE.md` 권한 `644` | `ls -l` |
| F10 | 활성 watchdog는 **legacy** `scripts/watchdog.py`(`Type=notify`, `WatchdogSec=900`) | unit 확인 |
| F11 | `.path` unit 없음. unit 미러는 `systemd/user/*.service|*.timer`만 sync됨 | `ls`, `sync-units.sh:38` |
| F12 | `TIMER_TARGETS` 12종 vs 라이브 타이머 21종 → 미등록 후보 9종 | §5 |
| F13 | 정본 infra는 `state_collector`가 생성(`Status: production`) | `state_collector/main.py` |

### 0.2 도구별 규칙 파일 해석 (2026 웹 조사)

| # | 사실 | 출처 |
|---|------|------|
| T1 | **AGENTS.md = 사실상 표준**(Linux Foundation AAIF, 28+ 도구·60k+ repo). **OpenCode·Codex·Cursor·Copilot·Windsurf 네이티브 읽기**. 스키마 없음, 근접 우선(nearest wins) | agents.md · thefalcon.dev · vibecoding.app |
| T2 | **Claude Code는 AGENTS.md를 읽지 않는다**(Anthropic 공식). 해법 = `@AGENTS.md` import 또는 symlink | Anthropic memory docs (복수 인용) |
| T3 | **Copilot은 AGENTS.md 네이티브** + `.github/copilot-instructions.md`가 **우선순위 더 높음**(path `.instructions.md` > `copilot-instructions.md` > agent instructions). copilot 파일을 만들면 AGENTS.md를 **가림** | GitHub response-customization docs |
| T4 | 권장 패턴 = **canonical 1개 + 나머지 symlink/얇은 import 스텁**. "Declare → Generate → Verify" | dev.to(takashimatsuyama) · ethical.institute |
| T5 | Git은 symlink(mode `120000`)을 그대로 추적. Windows만 Developer Mode 필요(본 서버는 Linux) | dev.to · devcheolu.com |
| T6 | 규칙 파일은 **자동 주입되는 신뢰 입력** → 프롬프트 인젝션 경로. 2026년 6개 에이전트 symlink RCE. **코드처럼 PR 리뷰** | thefalcon.dev |

> 결론: 현 시스템은 **copilot 파일·infra 사본·3중 `@import`가 모두 불필요**하다. `AGENTS.md` 하나를 canonical로 두고 `CLAUDE.md`는 `@AGENTS.md` + Claude 전용 라우팅만 남긴다.

---

## 1. 설계 결정 (Design Decisions)

| ID | 결정 | 근거 |
|----|------|------|
| **D1** | **SSOT = 소스 3종** (`docs/architecture/infrastructure.md`, `llm-common-rule.md`, `llm-agent-rule.md`). `AGENTS.md`는 **canonical 파생 실파일**. "AGENTS.md가 마스터" 안은 **기각** | 보고서 Risk 1 |
| **D2** | 정본 infra = `docs/architecture/infrastructure.md`. 생성기가 **정본을 직접 읽는다**. `/home/opc/infrastructure.md` 사본은 **제거** | F7 제거. T4 |
| **D3** | `CLAUDE.md` = **`@AGENTS.md` + Claude 전용 라우팅**. 3중 `@import` 폐기 (라우팅은 OpenCode에서 제거된 MCP를 지시하므로 Claude 전용) | F4, T2, T4 |
| **D4** | 트리거는 **systemd `.path`(inotify)**, watchdog 루프 폴링 아님 | 보고서 Phase 2-2 |
| **D5** | **copilot 파일 미생성**. Copilot이 AGENTS.md를 네이티브로 읽으며, copilot 파일은 오히려 AGENTS.md를 가린다 | F6, T3 |
| **D6** | 쓰기는 **원자적**(temp+`os.replace`), 변경 감지는 **sha256**, 로그는 **logging**(print 금지) | llm-common-rule |
| **D7** | 신규 코드는 `scripts/gen_agents.py` 단일 파일(stdlib만). watchdog v2.1(`src/devforge`)과 **결합하지 않음** | Phase 2 v2.1 리팩터와 충돌 회피 |
| **D8** | **Declare → Generate → Verify**. 배선 검증을 일일 타이머(`--check`)로 강제 | T4 |
| **D9** | `AGENTS.md`는 **코드처럼 PR 리뷰**(자동 주입 입력·인젝션 경로) | T6 |
| **D10** | (선택) repo root `/opt/projects/server/AGENTS.md`에 프로젝트 스코프 사본 → 에이전트가 repo에서 실행될 때도 규칙 적용 | T1(근접 우선) |

> 기각: copilot 파일 생성(T3 역효과), `/home/opc/infrastructure.md` 사본 유지(D2), CLAUDE.md 3중 `@import`(D3), symlink 전면 채택(라우팅이 Claude 전용이므로 import가 정답), watchdog `orchestrator.py` 삽입, `print()`, mtime 등호 감지, 비원자적 `shutil.copy2`.

---

## 2. 목표 아키텍처

```
[SSOT — 수동/자동 생성]
  docs/architecture/infrastructure.md  ← state_collector (F13)
  /home/opc/llm-common-rule.md         ← 수동
  /home/opc/llm-agent-rule.md          ← 수동
        │
        │  devforge-agents-gen.path (inotify) ──► devforge-agents-gen.service (oneshot)
        │                                              │  gen_agents.py
        │                                              │  · header 템플릿 + 3 소스 concat
        │                                              │  · sha256 비교 → 원자적 쓰기
        ▼                                              ▼
[canonical 실파일]
  /home/opc/AGENTS.md   (600)  ← 생성 산출물, 직접 수정 금지

[소비자]
  OpenCode  → AGENTS.md 직접 읽기 (T1)
  Copilot   → AGENTS.md 직접 읽기 (T3, 별도 파일 없음)
  Claude    → CLAUDE.md = @AGENTS.md + Claude 전용 라우팅 (T2)

[참조 — CLAUDE.md 1단계 전환]
  /home/opc/CLAUDE.md            = @AGENTS.md + 라우팅 (3중 @import 폐기)
  /home/opc/.claude/CLAUDE.md    = @/home/opc/CLAUDE.md
  /opt/projects/server/CLAUDE.md = @/home/opc/CLAUDE.md
```

**감시 경로(`.path`)**: `docs/architecture/infrastructure.md`, `llm-common-rule.md`, `llm-agent-rule.md`
**감시 제외**: `/home/opc/AGENTS.md` (파생물 → 루프 방지)
**제거**: `/home/opc/infrastructure.md` (D2), `.github/copilot-instructions.md` (D5, 생성 안 함)

---

## 3. Phase 1 — 무결성 복원 (저위험, 즉시)

> 목표: **재생성 diff = 0**. 순서 엄수(역이관 없이 재생성하면 `#23/#25` 유실).

### P1-1. 역이관: `AGENTS.md:226/228` → `llm-agent-rule.md:72/74`

`llm-agent-rule.md`의 두 bullet을 `AGENTS.md` 최신본으로 **교체**한다(내용 추가, 삭제 아님).

- `:72` `#23` bullet 끝에 추가:
  `(현행 구현 위치: 리팩터드 \`src/devforge/adapters/driving/mcp/server.py\`, 2026-09-20 포팅; \`scripts/mcp_server.py\`는 비활성 레거시)`
  그리고 `→ 2주 후 percentile 기반 계수 재교정 예정(미착수).` 문구 **삭제**.
- `:74` `#25` bullet을 `AGENTS.md:228`의 **완료** 서술로 교체:
  `(완료, task #25) … 2026-09-20 적용: base = 1:150 2:240 3:450 4:420 5:150 6:810 7:180 (ceil30(max(p90*1.5, 실측max)), 완료 57건/0 overrun) … 리팩터드 라이브 서버 반영(commit 668011e). 표본 증가 시 재검토.`

검증:
```bash
diff <(sed -n '226p;228p' /home/opc/AGENTS.md) <(sed -n '72p;74p' /home/opc/llm-agent-rule.md)
# 목표: 출력 없음
```

### P1-2. 재생성 (정본 infra 직접 읽기)

`/home/opc/infrastructure.md` 사본을 쓰지 않고 **정본에서 직접** 재생성한다(D2).
`AGENTS.md`를 새로 써서 `:9`/`:63` 인프라 라인 드리프트를 해소한다.

```bash
cd /tmp
head -7 /home/opc/AGENTS.md > gen.md
cat /opt/projects/server/docs/architecture/infrastructure.md \
    /home/opc/llm-common-rule.md /home/opc/llm-agent-rule.md >> gen.md
diff /home/opc/AGENTS.md gen.md        # 재생성 전: 4줄(:9,:63,:226,:228)
cp /tmp/gen.md /home/opc/AGENTS.md
chmod 600 /home/opc/AGENTS.md          # 권한 유지
```
> 사본 `/home/opc/infrastructure.md`는 Phase 2(P2-3)에서 제거한다. 그때까지는 방치(참조되지 않음).

### P1-3. `code-structure.yaml` 스테일 제거

`docs/architecture/code-structure.yaml`에서 아래 블록 삭제:
```yaml
    sync_gemini_rules.py:
      status: experimental
      purpose: Sync shared rules into OpenCode AGENTS.md.
```

### P1-4. 재생성 검증 (Gate 1)

P1-1·P1-2 완료 후 재생성 결과가 정본과 일치하는지 확인한다.

```bash
cd /tmp
head -7 /home/opc/AGENTS.md > gen.md
cat /opt/projects/server/docs/architecture/infrastructure.md \
    /home/opc/llm-common-rule.md /home/opc/llm-agent-rule.md >> gen.md
diff /home/opc/AGENTS.md /tmp/gen.md && echo "GATE1 OK"   # 0
```
> `AGENTS.md:9`의 `auto-generated ... 2026-09-11` 헤더는 `state_collector`가 정본 infra를 재생성할 때 갱신된다(직접 손대지 않음). `gen_agents.py`의 헤더 템플릿에는 타임스탬프를 넣지 않는다(P2-2).
> P2 완료 후에는 `head -7` 대신 헤더 템플릿 + `gen_agents.py --check`를 사용한다.

---

## 4. Phase 2 — 재발 방지 (생성기 + `.path`)

### P2-1. `scripts/gen_agents.py` (신규, stdlib만)

> 전체 코드: `docs/plans/agents-sync-redesign-guide-appendix.md` → 부록 C.

### P2-2. 헤더 템플릿 (신규, 버전관리)

`systemd/agents/agents-header.md` — 현재 `AGENTS.md` `head -7`과 **정확히 일치**시킨다(타임스탬프 없음):
```
# OpenCode Instructions (AGENTS.md)

> 이 파일은 AGENTS.md — DevForge 서버 에이전트 규칙
> OpenCode 전용 설정은 `~/.config/opencode/opencode.json`에서 관리하세요.

---
```
> ⚠️ `head -7`이 `---` 다음 빈 줄까지 포함해야 concat 결과가 기존 파일과 동일하다(§부록 B-1).

### P2-3. `CLAUDE.md` 전환 + 사본 제거

`CLAUDE.md`의 3중 `@import`를 `@AGENTS.md` 하나로 바꾸고, Claude 전용 라우팅만 남긴다(D3).
`/home/opc/infrastructure.md` 사본을 삭제한다(D2). **copilot 파일은 만들지 않는다**(D5).

`/home/opc/CLAUDE.md` (신규 내용):
```markdown
@AGENTS.md

<!-- 아래는 Claude 전용 (OpenCode에서는 제거된 MCP를 사용하므로 공유 대상 아님) -->
## Tool Routing (검색)
... (기존 라우팅 블록 유지)
## Tool Routing (시간)
... (기존 라우팅 블록 유지)
```

```bash
cp /home/opc/CLAUDE.md /tmp/agents-bak-20260922/CLAUDE.md.orig   # 백업
# 1) 첫 3줄(@import 3종)을 @AGENTS.md로 교체, 라우팅 블록은 그대로 유지
# 2) 사본 제거
rm -f /home/opc/infrastructure.md
diff <(head -1 /home/opc/CLAUDE.md) <(echo '@AGENTS.md') && echo "CLAUDE.md OK"
ls /home/opc/infrastructure.md 2>&1 | grep -q "No such" && echo "infra copy removed"
```
> 라우팅 블록이 실제로 Claude 전용인지 확인(P1 검증). 만약 공유 대상이면 `llm-agent-rule.md`로 승격하고 `CLAUDE.md`를 symlink로 전환(T4).

### P2-4. systemd units (신규)

`systemd/user/devforge-agents-gen.service`:
```ini
[Unit]
Description=DevForge — regenerate AGENTS.md from rule SSOT
After=devforge-agents-gen.path

[Service]
Type=oneshot
WorkingDirectory=/opt/projects/server
ExecStart=/usr/bin/python3.12 /opt/projects/server/scripts/gen_agents.py
StandardOutput=journal
StandardError=journal
```

`systemd/user/devforge-agents-gen.path`:
```ini
[Unit]
Description=DevForge — watch rule sources for AGENTS.md regeneration

[Path]
PathChanged=/opt/projects/server/docs/architecture/infrastructure.md
PathChanged=/home/opc/llm-common-rule.md
PathChanged=/home/opc/llm-agent-rule.md
Unit=devforge-agents-gen.service

[Install]
WantedBy=default.target
```

일일 드리프트 체크(이중 안전망) `systemd/user/devforge-agents-drift.{service,timer}`:
```ini
# .service
[Unit]
Description=DevForge — daily AGENTS.md drift check
[Service]
Type=oneshot
ExecStart=/usr/bin/python3.12 /opt/projects/server/scripts/gen_agents.py --check

# .timer
[Unit]
Description=Daily AGENTS.md drift check
[Timer]
OnCalendar=*-*-* 04:30:00
Persistent=true
[Install]
WantedBy=timers.target
```

### P2-5. `sync-units.sh`에 `.path` 포함

`scripts/deploy/sync-units.sh:38,61`의 글롭에 `*.path` 추가:
```bash
for f in "$REPO_ROOT"/systemd/user/*.service "$REPO_ROOT"/systemd/user/*.timer "$REPO_ROOT"/systemd/user/*.path; do
```

### P2-6. watchdog 관측 등록

`scripts/lib/watchdog/config.py`의 `ONESHOT_RESULT_TARGETS`에 추가:
```python
    "devforge-agents-gen.service",   # AGENTS.md 생성 (실패 시 alert-only)
    "devforge-agents-drift.service", # 일일 드리프트 체크
```
> v2.1 watchdog(`src/devforge`) 전환 후에는 `WatchdogConfig`/health port 등록으로 이관한다(D7).

### P2-7. 배포 순서

```bash
cd /opt/projects/server
# 1) P2-3: CLAUDE.md 전환 + infra 사본 제거 (백업 후)
# 2) units 미러 → live
scripts/deploy/sync-units.sh --check          # 기존 드리프트 확인
scripts/deploy/sync-units.sh                  # 미러 → live 반영 + daemon-reload
systemctl --user daemon-reload
systemctl --user enable --now devforge-agents-gen.path
systemctl --user enable --now devforge-agents-drift.timer
# 3) 초기 정렬 + 검증
scripts/gen_agents.py                          # 1회 수동 실행
scripts/gen_agents.py --check                  # exit 0 확인
head -1 /home/opc/CLAUDE.md                    # @AGENTS.md 확인
```

### P2-8. (선택) repo-root `AGENTS.md` (D10)

에이전트가 `/opt/projects/server`에서 실행될 때도 규칙을 읽도록, repo root에 프로젝트 스코프
사본을 둔다(근접 우선, T1). 전역(`/home/opc`)과 내용이 다르면 안 되므로 **심링크**로 건다.

```bash
ln -sfn /home/opc/AGENTS.md /opt/projects/server/AGENTS.md
ls -la /opt/projects/server/AGENTS.md          # -> /home/opc/AGENTS.md
```
> repo에 커밋할 경우 `git ls-files -s`가 `120000`(symlink)인지 확인(T5). 홈 경로 절대 심링크는
> 다른 사용자/CI에서 깨질 수 있으므로, 다중 사용자 환경이면 repo-로컬 파일 + `@AGENTS.md` import를 검토.

---

## 5. Phase 3 — 미등록 타이머 편입 ✅ 완료 (2026-09-22)

- 커밋: `a35af00` — `scripts/lib/watchdog/config.py` (+16)
- 사용자 승인: 가이드 §5 따름(6종), ONESHOT 제안대로 3종
- G5 통과: `comm -23` 잔여 = 의도 제외 3종 + OS/일시 유닛만

### 후보 산출(정확히)
```bash
# list-timers 컬럼: NEXT LAST UNIT ACTIVE → UNIT은 $(NF-1), $NF는 ACTIVE(service)
comm -23 \
  <(systemctl --user list-timers --all --no-legend | awk '{print $(NF-1)}' | grep '\.timer$' | sort -u) \
  <(python3.12 -c "import sys;sys.path.insert(0,'/opt/projects/server/scripts');from lib.watchdog import config as c;print('\n'.join(sorted(c.TIMER_TARGETS)))")
```
> ⚠️ `$(NF-1)` 필수 — `$NF`는 service명이라 `grep '\.timer$'`가 항상 빈 출력.

### 미등록 후보 9종 (검증 F12) — 확정 판정
| 타이머 | 판정 | max_idle |
|--------|------|----------|
| `golden-image-deploy-check.timer` | **편입** (보고서 지정) | 1350 (15m×1.5) |
| `workspace-autocommit.timer` | **편입** (보고서 지정) | 2700 (30m×1.5) |
| `devforge-summary-retry.timer` | **편입** | 10800 (2h×1.5) |
| `baseline-daily.timer` | **편입** | 129600 (24h×1.5) |
| `kv-backup.timer` | **편입** (백업 = 데이터 리스크) | 907200 (주×1.5) |
| `golden-image-yearly-check.timer` | **편입** (연 1회) | 34560000 (400일, 지정) |
| `devforge-refresh-reminder.timer` | **제외** (연 3회 알림성) | — |
| `activity-summarizer-safety.timer` | **제외** (알림성, 가이드 §5) | — |
| `devforge-watchdog-liveness.timer` | **제외** (watchdog 자체 liveness) | — |

> 보고서는 "6종"이라 했으나 실측은 9종. 최종 6종 편입·3종 제외로 확정(사용자 승인).

### ONESHOT_RESULT_TARGETS +3 (실패 감지)
`kv-backup.service` · `workspace-autocommit.service` · `golden-image-deploy-check.service` (기존 4종 유지 → 총 7종)

### 검증 결과
- G5 residual (의도된 잔여): `activity-summarizer-safety`, `devforge-refresh-reminder`, `devforge-watchdog-liveness` + `grub-boot-success`, `systemd-tmpfiles-clean`, 일시 hash 유닛 3종
- ruff / mypy(src) / lint-imports(4 KEPT) / unit+characterization 195 passed (2 skipped) / LSP diagnostics 0

---

## 6. 검증 게이트

| Gate | 조건 |
|------|------|
| **G1 (Phase 1)** | 재생성 `diff` = 0 (§3 P1-4) |
| **G2 (Phase 2)** | `gen_agents.py --check` exit 0; `AGENTS.md` mtime 갱신; `.path` 발화 로그 확인; `head -1 CLAUDE.md` = `@AGENTS.md` |
| **G3 (드리프트 내성)** | 소스 1줄 수정 → 60초 내 `AGENTS.md` 반영 → `--check` exit 0 |
| **G4 (무회귀)** | `lint-imports` 4 KEPT; `pytest tests/unit tests/characterization` green; `mypy src/devforge` clean |
| **G5 (Phase 3)** | `comm -23` 결과가 의도한 잔여만 남음 | ✅ 통과 (2026-09-22, `a35af00`) |

---

## 7. 롤백

| 단계 | 롤백 |
|------|------|
| P1 | `git checkout` 불가(`/home/opc`는 비-git) → **선행 백업**: `cp -a /home/opc/{AGENTS.md,infrastructure.md,llm-agent-rule.md} /tmp/agents-bak-20260922/` |
| P2 | `systemctl --user disable --now devforge-agents-gen.path devforge-agents-drift.timer`; `rm` units; `sync-units.sh` 재적용 |
| P2 (데이터) | 생성기 중단 후 수동 편집 복원(`/tmp/agents-bak-*`) |
| P3 | `config.py` 되돌림 (git tracked) |

---

## 8. 리스크

| # | 리스크 | 완화 |
|---|--------|------|
| 1 | `head -7` 오프셋이 틀리면 concat 결과가 달라짐 | P2-2에서 `diff` 검증, 템플릿 파일을 정확히 7줄로 고정 |
| 2 | `.path`가 파생물을 감시 → 무한 루프 | 감시 대상에서 `AGENTS.md` 제외(사본은 제거됨) |
| 3 | 라우팅이 실제로는 공유 대상일 수 있음 | P2-3에서 확인. 공유면 `llm-agent-rule.md`로 승격 후 symlink 전환(T4) |
| 4 | 권한 변화(600) | `atomic_write`에 모드 명시(`AGENTS.md=600`) |
| 5 | state_collector가 정본 infra를 재생성 → `.path` 발화 | 정상 동작(자동 정렬). 폭주 시 `PathChanged` debounce 확인 |
| 6 | legacy watchdog에 등록 → v2.1 전환 시 누락 | D7/P2-6 주석에 이관 명시 |
| 7 | **규칙 파일 = 자동 주입 입력(인젝션 경로)** | D9: `AGENTS.md` 변경을 코드처럼 PR 리뷰 (T6) |

---

## 부록

파일 인벤토리와 증거/검증 명령은 `docs/plans/agents-sync-redesign-guide-appendix.md` 참조.
