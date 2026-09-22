# AI 에이전트 규칙 — 콘텐츠 감사 및 개정안

- 작성: 2026-09-22 (KST)
- 상태: 분석 완료 / 개정안 초안 (미적용)
- 관련: `docs/reports/agent-rules-analysis-20260922.md` (드리프트·동기화 진단), `docs/plans/agents-sync-redesign-guide.md` (배선 재설계)
- 범위: 규칙 **내용**(무엇을 넣고 뺄까)을 2026 업계 표준과 대조. 배선(동기화)은 별도 문서.

---

## 1. 업계 표준 (2026 조사)

| 항목 | 표준 | 출처 |
|------|------|------|
| 길이 | **<150줄 권장**(2,500 repo), **<300줄 합의 상한**, HumanLayer <60. >150줄은 수확 체감 + **비용 20–23%↑** | GitHub 2,500-repo 분석 · betterclaw · techriseups |
| 작성 주체 | **손으로 작성**. LLM/자동 생성은 **5/8 설정 성공률 하락** +2.45–3.92 스텝 | ETH Zurich |
| 삭제 대상 | 일반 엔지니어링 원칙, README 중복, file pointer 없는 아키텍처 개요, prose 스타일, 디렉터리 트리, 기술 소개, 결정 근거, 린터가 강제하는 규칙, **이력/일기, 임시 태스크 상태** | appxlab · atlan · nyk |
| 포함 대상 | 정확한 명령(플래그 포함), 버전 스택, **3단 경계(Always/Ask first/Never)**, 비직관적 gotcha, 실제 코드 1개, `file:line` 포인터 | reynders · atlan |
| 판단 기준 | "에이전트가 코드를 읽어 알 수 있는가?" → 예면 삭제 | appxlab |
| 동적 상태 | **금지**(드리프트) | nyk |
| 최고 ROI 1줄 | `Never commit secrets` | 2,500-repo 분석 |
| 언어 | 영어(모델 토큰 효율↑) | 사용자 지적 + 토큰 경제 |

---

## 2. 현 규칙 감사

### 2.1 정량 (매 세션 주입)

| 파일 | 줄 | 문자 | ~토큰 |
|------|----|------|-------|
| `infrastructure.md` (자동생성) | 72 | 2,923 | ~730 |
| `llm-common-rule.md` | 74 | 4,785 | ~1,200 |
| `llm-agent-rule.md` | 130 | 9,328 | ~2,330 |
| **AGENTS.md 합계** | **284** | 17,404 | **~4,350** |
| `CLAUDE.md` routing | 22 | 839 | ~210 |

### 2.2 발견 사항

**A. 삭제 후보 (표준 위반)**

| # | 위치 | 내용 | 문제 | 표준 위반 |
|---|------|------|------|-----------|
| A1 | `agent:71-75` | Deep Dive "참고(구현 상태)" — task #23/#24/#25 (5줄·1,870자·~470토큰 = AGENTS.md의 11%) | 구현 **이력** ≠ 지시. **#23/#25 드리프트 원천** | "incident diary 금지" |
| A2 | `infra:42-57` | Key Services 표의 **Status 컬럼** | **라이브 상태** 정적 스냅샷 (F3 드리프트) | "동적 상태 금지" |
| A3 | `infra:4-40` | Overview 스펙·diagram·Network·Storage (45줄·~400토큰) | 에이전트가 코드에서 읽음. 문서 ≠ 지시 | "개요 삭제" |
| A4 | `common:9-17,55-60` | Evaluation SQL 8줄, File Header 예시 6줄 | 포인터로 대체 가능(경미) | "포인터 > 복사" |

**B. 시스템 변화와 불일치**

| # | 현 규칙 | 실제 | 판정 |
|---|---------|------|------|
| B1 | `agent:18` "**제거된** MCP(opencode)" | `opencode.json`에 전부 존재, `enabled=false` | 표현 정정("비활성") |
| B2 | `agent:4` MCP 경로 `/home/opc/.claude/mcp.json` | OpenCode는 `~/.config/opencode/opencode.json` | 도구별 분리 |
| B3 | `common:20` "**Claude** MUST answer in Korean" | common(전 도구 공용) | 도구명 → "agents" |
| B4 | `agent:64,72` `scripts/lib/watchdog/fixloop.py`, `scripts/mcp_server.py` | Phase 2 v2.1 `src/devforge/` 전환 중 | legacy 경로 |
| B5 | `CLAUDE.md:21` `time.*` | opencode `time` 비활성 | Claude 전용 명시 |

**C. 중복**
- C1: infra 내용이 정본 + `/home/opc` 사본 + AGENTS.md 내장 = **3중**
- C2: `cli.py status --json`(라이브)이 있는데 정적 스냅샷을 박음

---

## 3. 제시 템플릿 검토

사용자 제공 "AI 에이전트 코드 유지보수 및 작성 가이드" (9장).

### 3.1 표준 부합 (채택 권장)
- §0 Guardrails(8항) — 사용자/테스트/불확실성/자동화금지/테스트무결성/보안/고위험/SSOT: **업계 3단 경계와 정합**.
- §1 컨텍스트 우선순위(Reading/Writing) — 표준 부합.
- §3 주석 `[WHY]/[WARNING]/[WORKAROUND]` + 금지 목록 — **우수**.
- §4 테스트(서술형·엣지·**Characterization**) — 표준 부합, DevForge 기존 규칙과 일치.
- §5 Conventional Commits + PR 4요소(Context/Changes/Verification/Risks) — 표준 부합.
- §6 SOP 경량/전체 경로 분리 — **우수**(과잉 프로세스 방지).
- §8 예외 처리, §9 요약 — 표준 부합.

### 3.2 충돌/수정 필요
| 항목 | 템플릿 | 우리 조사 결론 |
|------|--------|----------------|
| SSOT 방향 | §0.8 "**AGENTS.md가 Master**" | **소스가 SSOT**, AGENTS.md는 생성물 (D1) ※ §4에서 Option B로 변경: AGENTS.md = 수동 canonical |
| 사본 동기화 | §0.8/§7.1 "watchdog 60초 복사" | **systemd `.path`(inotify)** (D4) |
| Copilot | §0.8/§7.1 `copilot-instructions.md` **사본** | **파일 미생성** — Copilot이 AGENTS.md 네이티브 + copilot 파일이 우선순위로 AGENTS.md를 **가림** (D5, T3) |
| Claude | §7.1 `CLAUDE.md` **사본** | **`@AGENTS.md` import** + Claude 전용 (D3) |
| 명령 | §7.2 `npm/gradle` placeholder | **`pytest`/`ruff`/`mypy`/`devforge`** |
| 언어 | 한국어 | **영어**(토큰 절감) |

### 3.3 사실 검증 (템플릿 §7.3 — 정확)
- Obsidian cron `*/1 * * * * $HOME/Obsidian/scripts/auto-sync.sh` ✅
- netdata active ✅ · watchdog `CHECK_INTERVAL=60` ✅

> 결론: 템플릿의 **본문 구조(§0–6, §8–9)는 채택**, **§0.8/§7.1(동기화 모델)만 우리 결론으로 교체**, §7.2/7.3은 DevForge 실제값으로.

---

## 4. 개정 원칙 (Option B 확정)

**Option B = 1개 수동 canonical 파일 + 포인터.** (Option A의 3소스+생성기는 기각)

1. **영어**(machine-facing) — 한국어 대비 ~2–3x 토큰 절감
2. **1개 수동 파일**: `llm-common-rule.md` + `llm-agent-rule.md`를 **`AGENTS.md`로 병합**, 중복(secrets·DB쿼리) 삭제
3. **≤150줄** 목표 (명령 우선)
4. **3단 경계**: Always / Ask first / Never
5. **infra는 포인터**: `infrastructure.md`는 **소스에서 제외**(자동생성·라이브상태) → `docs/architecture/infrastructure.md` + `cli.py status --json` 인용
6. **`CLAUDE.md = @AGENTS.md`**, **copilot 파일 없음** (Copilot이 AGENTS.md 네이티브)
7. **생성기/`.path`/verify 타이머 불필요** — AGENTS.md가 수동 파일이므로 "재생성 diff" 자체가 없음
8. 상세(Deep Dive/Session/Testing/MCP)만 `agent_docs/*.md`로 **점진 공개**

---

## 5. 개정안 (Draft)

> **병합 초안 전문**: `docs/plans/agents-md-merged-draft.md` (영어, 12개 섹션)

```
0 Guardrails        ← 템플릿 §0 + 기존 secrets/자동커밋 금지
1 Commands          ← 신규 (pytest/ruff/mypy/lint-imports/cli.py/podman)
2 Boundaries        ← 3단 (Always / Ask first / Never)
3 Communication     ← common (Korean/English, UTC/KST, Code is SSOT)
4 Context priority  ← 템플릿 §1
5 Code              ← common "Code Generation" + "Code as Documentation"
6 Comments          ← [WHY]/[WARNING]/[WORKAROUND] + no WHAT
7 File header       ← common (Status/Path) — 1곳으로 통합
8 Tests             ← 템플릿 §4 + common Red-Green + agent Characterization
9 Git               ← 템플릿 §5 (Conventional Commits + PR 4요소)
10 Workflow         ← 템플릿 §6 (경량/전체) + Evaluation-first + Deep Dive 포인터
11 Pointers         ← infra/session/testing/mcp/glossary/layout
12 Philosophy       ← 템플릿 §9
```

**중복 제거**: secrets(common↔agent), DB 쿼리(common↔agent) → Guardrails/Commands 1곳.
**인라인 유지**(포인터 아님): Guardrails·Boundaries·Commands·Code·Comments·File header — 에이전트가 항상 봐야 하는 규칙.
**포인터로 이동**: infra, Deep Dive, Session, Testing, MCP — 길고 다른 곳에 존재.

### 5.1 분리 대상 (`agent_docs/`)
| 파일 | 내용(현 소스에서 이동) |
|------|------------------------|
| `agent_docs/deep-dive.md` | 7단계 Deep Dive + 피드백 분기 (구현 이력 A1 **제외** → `handover.yaml`) |
| `agent_docs/session.md` | Session Start/End, NewHand Protocol |
| `agent_docs/testing.md` | Pod B Testing Protocol |
| `agent_docs/mcp.md` | MCP 툴 표 + 리서치 라우팅 |
| (infra) | **파일 미생성** — 정본 `docs/architecture/infrastructure.md` 인용만 |

---

## 6. 적용 계획

1. **선행(데이터 보존)**: A1 구현 이력(`#23`/`#25`) → `handover.yaml`로 이관. `cp -a`로 `/home/opc/{AGENTS.md,CLAUDE.md,llm-*.md}` 백업.
2. **병합**: `docs/plans/agents-md-merged-draft.md` → `/home/opc/AGENTS.md` (영어, 수동).
3. **소스 정리**: `llm-common-rule.md` + `llm-agent-rule.md` → 삭제(내용은 AGENTS.md/agent_docs로 흡수). `/home/opc/infrastructure.md` 사본 삭제.
4. **`CLAUDE.md`**: 3중 `@import` → **`@AGENTS.md`** + Claude 전용 라우팅만.
5. **agent_docs/**: 상세 4종 생성, AGENTS.md §11에서 포인터.
6. **미생성 확인**: `.github/copilot-instructions.md` 없음. **생성기/`.path`/verify 불필요**.
7. **검증**: `wc -l /home/opc/AGENTS.md` ≤150; 각 에이전트에서 로드 확인(`/context` 등).
8. **리뷰**: 규칙 변경은 코드처럼 PR 리뷰(D9).

### 예상 효과
- AGENTS.md 284 → **~120줄**, 매 세션 ~4,350 → **~1,800토큰**(≈60% 절감)
- 드리프트 원천 A1·A2 **원천 제거**(생성기 없음), 3중 중복(C1) 해소
- 배선 단순화: 생성기·`.path`·verify 타이머·copilot 파일 **전부 불필요**
