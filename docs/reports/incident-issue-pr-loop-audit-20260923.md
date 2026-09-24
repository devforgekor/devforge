# incident → GitHub 이슈 → PR 루프 감사 (2026-09-23)

> Status: record · Date: 2026-09-23 · Owner: devforge
> Related: `plans/detection-remediation-architecture.md`(D1, 상위 라우팅 — 본 루프를 **C(escalate) 트랙**으로 편입), `plans/error-record-analysis-design.md`(D2, 기록), `reports/logic-recording-tracking-audit-20260923.md`, `scripts/lib/dev_pipeline.py`, `scripts/lib/watchdog/incidents.py`
> 목적: `AGENTS.md §6`("`# TODO` 금지 → 이슈 트래커") / `§12`("History → git/PR")의 **구현 로직**이 이전 문제 상황과 연결되는지 실측 검증.

---

## 0. 결론 (요약)

**연결된다 — 그리고 루프가 "PR 단계에서 멈춰" 있다.**
- 이전 문제(반복 incident) → **자동 GitHub 이슈 #8/#9** → `dev-poll` **claim** 까지 자동.
- 그러나 **PR 미생성**(`pr_created={}`) → 이슈 **수일~수주 OPEN 방치**.
- 이 자동화는 **legacy 전용** — **v2(hex)에 미이식** → 컷오버 시 소실 위험.

---

## 1. 구현 로직 (§6/§12 대응)

| § | 규칙 | 구현 |
|---|---|---|
| §6 | `# TODO` 금지 → 이슈 트래커 | 반복 incident → **GitHub 이슈 자동 생성**(`scripts/lib/watchdog/incidents.py:129,162`, `TASK_THRESHOLD=3`/7일, 라벨 `auto-safe`) |
| §6 | 이슈 처리 | `scripts/lib/dev_pipeline.py`(`poll_issues:62`, `claim_issue:112`, `_write_auto_task:192`, `create_pr:226`), `cli.py dev {poll,claim,pr}` |
| §6 | 자동 실행 | `devforge-dev-poll.service`(`cli.py dev poll --auto-safe --claim --once`, 10분) |
| §12 | History → git/PR | git 커밋/푸시 + `create_pr` |
| §12 | Warnings → comments | `[WHY]/[WARNING]/[WORKAROUND]` 마커(**관례, lint 미강제**) |

> **라우팅 편입(D1 §5.1)**: 이 루프는 `prefix`가 아니라 **반복 빈도**(`TASK_THRESHOLD=3`/7일)로 발화 → `plans/detection-remediation-architecture.md`의 **C(escalate) 분기**(두 번째 축: `repeat_count`).

---

## 2. 이전 문제 ↔ 이 루프 연결 (실측)

| 항목 | 값 |
|---|---|
| `svc:ebook-watcher:down` | **fail_count 443 / reopen_count 441**(재시작 폭주) + 후속 13/1, 32/0 등 |
| `syssvc:netdata:down` | 반복(이슈 #8 원인) |
| GitHub 이슈 **#8** | `[watchdog] 반복 실패: syssvc:netdata:down` — OPEN, `watchdog,auto-safe`, 생성 **2026-09-14** |
| GitHub 이슈 **#9** | `[watchdog] 반복 실패: svc:ebook-watcher:down` — OPEN, `watchdog,auto-safe`, 생성 **2026-09-20** |
| dev_pipeline state | `seen:[8,9]`, `claimed:{8,9}`(assignee 1, `claimed_at`), **`pr_created:{}`** |

→ 즉 **"이전 문제 상황"이 곧 이 이슈들의 원인**이다(연결 확인).

---

## 3. 정체 지점 (핵심) ⚠️

```
반복 incident 감지(legacy)  ✅
      → GitHub 이슈 자동 생성  ✅ (#8/#9)
      → claim(브랜치/태스크)    ✅ (assignee, claimed_at)
      → PR 생성               ❌ pr_created={}   ← 여기서 멈춤
      → 종결(resolve)          ❌ 이슈 OPEN 방치
```
- `#8`(9/14)·`#9`(9/20) 생성 후 **PR 없이 OPEN**. 현재 두 서비스는 `active`이나 **근본 수정·PR 완결 없음**.
- 함의: **인간/에이전트 트랙(incident→issue→PR)이 완결되지 않음** → 사용자가 제안한 A(fix)/B(catch-up) **자동화의 실제 필요 근거**.

---

### 3.1 와치독 기록(recording)과의 연결

이 루프의 **이슈 본문은 `watchdog_incidents` 기록을 그대로 소비**한다 — `scripts/lib/watchdog/incidents.py:177`이 `symptom, fail_count, action, action_result, context`를 읽어 `### context (masked)`(line 189)로 삽입. 즉 **기록 → 이슈 → claim → PR** 이 한 파이프라인.

| 계층 | 문서 | 연결 상태 |
|---|---|---|
| 기록(legacy, `context` text 500자·1패턴 마스킹) | — | ✅ 이슈 본문에 소비 |
| 기록(v2, `context_jsonb`+`action_error`, 4패턴) | `plans/error-record-analysis-design.md` | ❌ 이슈 미연결 + v2 미이식 + 마이그레이션 미적용 |
| 루프(이슈→PR) | 본 문서 | ⚠️ PR에서 정체 |

→ **기록 품질이 이슈/RCA 품질을 결정**하나, 현재는 **legacy `context`(text)로만 연결**되고 **개선 기록(§1)은 끊겨** 있다. `error-record-analysis-design.md`(기록)와 본 문서(루프)는 **한 파이프라인의 양 끝**.

---

## 4. 컷오버 리스크 (v2 미이식)

- incident→이슈 자동화는 **legacy 전용**(`scripts/lib/watchdog/incidents.py`). **v2(`src/devforge`)에는 없음**(config 키 `MY_GITHUB_TOKEN_KEY`만).
- → P2/컷오버로 v2가 리더가 되면 **이슈 생성 경로 소실**.
- + `WATCHDOG-CONTAINER-TOOLS`로 v2는 **감지 오탐** → 이슈 생성 불가(**이중 갭**).

---

## 5. 권고

| # | 조치 | 우선 |
|---|---|---|
| 1 | **PR 정체 원인 진단** — `create_pr` 실패 사유(`pr_created` 미기록) 확인, #8/#9 완결 | P1 |
| 2 | **v2에 incident→issue 이식** — 도메인/어댑터 분리(포트), 컷오버 전 | P1 |
| 3 | §6 **기계 강제** — `# TODO` 탐지 lint 룰 추가(현재 AGENTS 텍스트만) | P2 |
| 4 | 루프 완결성 모니터링 — claim 후 PR 미생성/장기 OPEN 이슈 감지 | P2 |

---

### 5.1 표준 해법 (업계: Open SWE / SWE-agent / Kanban)

우리 루프는 **Open SWE 패턴의 반쪽**(트리거+claim만). 표준은:

| 표준 | 내용 | 우리 대응 |
|---|---|---|
| **Agent Issue→PR** | 라벨(`auto-safe`)→에이전트(**Plan→Implement→Test→Review**)→**PR 자동 오픈 + 이슈 링크** | **Implement/Review·자동 PR 미배선**(§3 정체 원인) |
| **GitHub 완결 강제** | required status checks + merge queue | PR 미생성 → PR 후 적용 |
| **Aging WIP / Stalled Work** | work-item age > **SLE** → 경보 | #8/#9 = **SLE 초과**(claim 후 PR 없음) |

**표준 정합**: 위 3표준은 `plans/detection-remediation-architecture.md` §6(C 트랙) · `plans/fitness-functions-heartbeat-drift-guide.md` §3.5(Aging WIP)·§2.2 #3(완결)와 일치 — 본 감사는 그 **실측 근거**.

**수정 방향(표준 기반)**: ① 에이전트 실행 연결(Implement→Review) ② `create_pr` 자동 배선(+ahead>0 가드) ③ Aging WIP 경보(SLE) ④ (선택) required checks.

---

## 6. 검증 명령 (재현)

```bash
# 이슈 상태
GH_TOKEN=$(python3 scripts/deploy/kv-fetch-env.py env --keys MY-GITHUB-TOKEN-KEY | sed -n 's/^MY_GITHUB_TOKEN_KEY=//p') \
  gh issue list --repo devforgekor/devforge --state all --limit 10
# 파이프라인 state
python3 -c "import sys;sys.path.insert(0,'scripts');from lib.dev_pipeline import _load_state;print(_load_state())"
# 반복 incident
podman exec postgres psql -U devforge -d devforge_app -c \
  "SELECT dedup_key, fail_count, reopen_count, status FROM watchdog_incidents ORDER BY fail_count DESC LIMIT 5;"
```
