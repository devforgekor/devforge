# DevForge — Agent Rules (Claude Code & Cloud AI)

## MCP Tools
MCP 설정 파일: `/home/opc/.claude/mcp.json`. 설치된 MCP를 내장 도구보다 우선 사용하고, 실패 시 내장 도구로 fallback합니다.

| MCP (전환기) | 사용 타이밍 |
|-----|------------|
| `devforge-mcp` | DevForge 커스텀 도구 (HTTP 8000). 노출 툴은 아래 필터 적용분만 |
| `yggdrasil` | 복잡한 문제 분석/설계 시 사고 구조화 (deep_planning, sequential_thinking, list_plans, get_plan) |
| `lsp` | 코드 인텔리전스 (pyright). 노출: `blast_radius`·`find_references`·`find_symbol`·`inspect_symbol`·`get_symbol_source`·`get_diagnostics`·`suggest_fixes`·`rename_symbol`·`detect_lsp_servers`·`start_lsp` |

> **태스크/체크포인트(서버측)**: `cli.py task list|add|update|show` — 태스크 상태 SSOT. 다중 파일/아키텍처 변경은 Deep Dive(`deepdive_step_*`) 체크포인트 사용.

> **리서치(서버측, 권장)**: MCP 대신 `cli.py research` 사용.
> - `cli.py research search "<질의>" [--mode auto|web|exa]` — 웹/시맨틱 검색
> - `cli.py research docs "<라이브러리>" "<질문>"` — 라이브러리/프레임워크 문서 (Context7)
> - `cli.py research fetch <url>` — URL 본문 추출
> - **제거된 MCP(opencode)**: `search-proxy`·`exa-search`·`fetch`·`context7`·`filesystem`·`github`·`time`·`shrimp-task-manager`.
>   대체: 파일검색→내장 도구, github→`gh`, time→`date`, 웹검색→내장 `websearch`, 문서/URL→`cli.py research`, 태스크→`cli.py task`(+Deep Dive `deepdive_step_*`). (Claude Code 설정은 별도 정리 예정.)

## Task + LSP 코드 수정 워크플로우

**적용 범위**: Deep Dive 트리거 없이 단독 사용 가능한 것은 **단일 파일, 국소적(local) 변경**(오탈자, 로그 문구, 상수 값, 단일 함수 내부 리팩터 등)에 한정. 여러 파일에 걸치거나 아키텍처/공개 인터페이스에 영향을 주는 변경은 반드시 Deep Dive(아래 절)를 선행한다.

코드 수정은 **`cli.py task`(추적) + `lsp`(영향 분석/진단)** 세트로 진행한다. **강제 락이 아니라** 상태·검증 기록으로 가시화한다:

```
Task 예시: cli.py task add "extract.py _sanitize_predicate 함수 리팩터"
  Step 1: lsp.inspect_symbol — 정의/참조 파악
  Step 2: lsp.find_references — 호출부 전수 확인
  Step 3: lsp.blast_radius — 변경 영향도 분석
  Step 4: (직접 편집)
  Step 5: lsp.get_diagnostics / suggest_fixes — 진단 재확인
  Step 6: cli.py task update <id> --status completed  (+ 검증 결과 기록)
```

원칙:
- 코드 수정은 `cli.py task`로 추적한다(다중 파일/인터페이스 변경은 Deep Dive의 `deepdive_step_*` 체크포인트 사용). 추적 없는 직접 편집 금지.
- 각 step 전에 lsp 툴로 현 상태 진단부터.
- blast_radius로 영향도 확인 후에만 편집.
- 편집 후 get_diagnostics / suggest_fixes로 진단 재확인하고 결과를 task/obs에 기록(증거 기반).
- 편집 후 진단이 **스테일**(unknown symbol/attribute)이면 `lsp_restart_lsp_server`로 재색인 후 재진단한다.
- blast_radius 결과가 단일 파일 범위를 벗어나면 즉시 중단하고 Deep Dive로 격상.

## Deep Dive 워크플로우

**트리거**: (a) 사용자가 "**딥다이브**"/"**deep dive**"라고 명시하거나, (b) 위 Task+LSP 적용 범위를 벗어나는 변경(다중 파일, 아키텍처/인터페이스 영향, 신규 파일 생성)일 때 자동 적용.

**Devin 패러다임**: 아래 7단계는 Devin(Cognition)의 자율 `plan→research→implement→verify` 루프를 MCP 툴로 재현한 것이다 (원형 근거: 2026-06-28 세션, `memory/deepdive-origin-devin.md`).

```
1. Yggdrasil (deep_planning) — 문제 구조화 (init→clarify)   ← Devin planning
2. 내장 도구 (Read/Grep/Glob/Bash) — 대상 코드 확인           ← Devin 코드 탐색
3. LSP — 심볼 분석, 참조 추적, blast_radius                   ← Devin 코드 탐색
4. cli.py research (docs 또는 web/exa) — 외부 문서/사실 검증  ← Devin research
5. Yggdrasil (deep_planning evaluate→finalize) — 종합, 수정 방안 도출 ← Devin planning 확정
6. Task + LSP — 구현 실행                                  ← Devin implement
7. 검증 (LSP 진단 또는 pytest)                               ← Devin verify
   └─ 실패 → 재판정 후 아래 기준으로 피드백 루프 (max 3회, 회차마다 실패 원인 기록)
```

**4단계 선택 기준**: 라이브러리/프레임워크 API·버전 스펙이면 `cli.py research docs "<lib>" "<질문>"`, 그 외 개념·사례·논문·일반 기술 검증이면 `cli.py research search "<질문>" --mode web|exa`. 판단이 모호하면 `docs` 우선(공식 문서가 더 정확).

**7단계 피드백 분기 기준** (분류 체계는 `scripts/lib/watchdog/fixloop.py:_classify_failure` 참조 — retryable/non-retryable 이분법을 재계획/재도출 이분법으로 대응):
- 검증 실패 원인이 **구현 버그**(코드 자체 오류, 진단/테스트 실패 — fixloop 기준 "retryable")면 → 5단계(방안 재도출)부터 재시작.
- 검증 실패 원인이 **전제 오류**(요구사항 오독, 영향 범위 재산정 필요, 애초 계획이 틀림 — fixloop 기준 "non-retryable")면 → 1단계(재계획)부터 재시작.
- 재시도 시 이전 회차의 실패 원인·시도한 방안을 다음 프롬프트/컨텍스트에 반드시 포함한다(맨땅에서 재시도 금지).

**3회 초과 시**: 자동 재시도 중단. 실패 이력(원인·시도한 방안)을 사용자에게 보고하고 다음 지시를 요청한다. 임의로 4회차 이상 진행 금지.

**참고(구현 상태)**:
- **대화형 세션 hang 감지 (구현 완료, Phase 1+2)** — devforge-mcp에 heartbeat 브릿지 구현: `deepdive_steps` 테이블 + `deepdive_step_enter/exit/session_heartbeat/session_status` 툴 4종 + 만료 감지 백그라운드 태스크(60s). 단계별 base/min/max bound(`DEEPDIVE_STEP_BUDGETS`), 1·2회 초과는 Slack 경고만, 3회 연속 초과 시 ABORTED + Slack 에스컬레이션(재진입은 `force=true` 없이는 거부). 만료 판정은 `max_bound_sec`(heartbeat 무관 절대 상한) + `base_timeout_sec` 기준 `last_heartbeat_at` staleness(dead man's switch) 이원 검사. Phase 2: `deepdive_step_enter`에 LSP blast_radius 결과를 `affected_files`로 전달하면 `base + affected_files*DEEPDIVE_FILE_MARGIN_SEC(120s)`로 max_bound를 동적 재계산(min/max clamp), 생략 시 Phase 1 정적 max와 동일(하위호환). 실측 `elapsed_sec` 기록 → 2주 후 percentile 기반 계수 재교정 예정(미착수). 기존 와치독 dead man's switch(`HEARTBEAT_STALE_SEC`)와는 별도 경로. task #23.
- **7단계 검증 샌드박스 격리 (구현 완료, task #24)** — 기존 `lib/action_queue.py`(production, MCP는 쓰기만/Watchdog이 실행하는 보안 경계) 브릿지 재사용: `deepdive_verify_sandbox(project_dir, test_cmd, affected_files)` 툴로 큐 등록(`action_type='sandbox_verify'`) → watchdog이 `_exec_sandbox_verify()`에서 fixloop.py `_sandbox_verify` 패턴(podman run --rm --network none --read-only --memory)을 테스트 실행용으로 확장(`SANDBOX_VERIFY_TIMEOUT=120s`, `SANDBOX_VERIFY_MEM_LIMIT=256m`, 마운트 `:ro,z` SELinux relabel) → `action_poll_results`로 결과 조회(재시도 3회/`HUMAN_REQUIRED` 에스컬레이션은 기존 흐름 그대로). devforge-mcp에 podman.sock 마운트(DinD)는 기존 보안 경계를 깨서 기각. affected_files가 .md만이면 샌드박스 생략(리소스 절약). 1차 지원: 외부 의존성 없는 표준 라이브러리 코드(pytest가 sandbox 이미지에 미설치). sessionId=dp-20260817-deepdive-sandbox-verify. task #24.
- **heartbeat 계수 percentile 재교정 (미착수, task #25)** — Phase1/2 배포일(2026-08-17)로부터 2주간 `deepdive_steps`에 축적되는 실측 `elapsed_sec`/`heartbeat_stale_sec` 데이터를 percentile(p50/p90 등) 기반으로 분석해 `DEEPDIVE_STEP_BUDGETS`의 base/min/max bound와 `DEEPDIVE_FILE_MARGIN_SEC`(현 120s/파일)을 재교정. 2026-08-31 이후 착수 가능.
- fixloop.py가 `experimental` 상태이므로, 프로덕션 승격 전까지는 패턴만 참조하고 코드는 연동하지 않는다.

원칙:
- 리서치는 `cli.py research`로 일원화. 동일 질의 중복 호출 금지.
- 검증 실패 시 피드백 루프 필수(무한루프 방지를 위해 max 3회), 3회 초과 시 사용자 보고 후 대기

## Session Start
- **FIRST**: Run `python3 /opt/projects/server/scripts/cli.py status --json` — live system state (containers, models, timers, services, resources, tasks, experiments, config, alerts). This replaces reading CLAUDE.yaml for system state.
- Read `/opt/projects/server/CLAUDE.yaml` for rules, entry points, storage layout, and procedural constraints only. System state in CLAUDE.yaml may be stale — `status --json` always takes precedence.
- Read `/opt/projects/server/handover.yaml` for decisions and known_issues (DB auto-generated via update_handover.py).
- Read `cli.py task list` for current/pending tasks (DB-backed view).
- Reference DB `glossary_terms` table (`cli.py status --json` -> glossary) for domain terminology.
- **Live state over document**: When status --json output disagrees with any YAML document, the live status command is correct. Update the stale document.
- **Criteria first**: For tasks needing external reference (benchmark, best practice, API spec), use WebSearch/WebFetch to research → draft criteria reflecting server state → present to user for finalization. Implement only after criteria confirmed.

## File Modification
1. **Read only**: `state.yaml`, `changelog.yaml` (past entries)
2. **Append only**: `changelog.yaml` (new entries), worklog DB
3. **Update freely**: tasks DB (`cli.py task update`), handover DB (update_handover.py), source code
4. **Ask first**: `docs/*.md` (design documents)

### Audit Rules — Status-Based Risk Triage
When auditing code for risks (SQL injection, exec/eval, unsafe parsing, etc.), check the file's `# Status:` header BEFORE assigning severity:
- `Status: production` → risk found = P0, propose fix
- `Status: experimental` → risk found = warn only, do NOT propose P0 fix
- `Status: deprecated` → do not modify, note for removal
- Missing header → treat as `production` (fail safe)
- The `# Status:` line in the file is SSOT. `code-structure.yaml` is advisory only.
- **Glossary SSOT**: `docs/domain-glossary.yaml`. Edit YAML only, then run `cli.py glossary sync`. Never write to DB glossary_terms directly.

## Infrastructure
- Containers: Quadlet only (`~/.config/containers/systemd/`). Secrets: `~/.config/devforge/secrets.env` only, never inline.
- DB queries: `podman exec postgres psql -U devforge -d devforge_app`. Never `psql -h localhost`.

## Testing Protocol
When running any test that modifies Pod B mode:
0. **사전 확인**: `cli.py status --json`으로 현재 Pod B mode와 timer 상태 확인
1. **Cycle stop**: `systemctl --user stop devforge-day-cycle.{service,timer}` (night: `night-cycle`)
2. **Setup**: `test_setup(name, description)` 호출 — heartbeat pulse 등록 + 중복 실행 방지
3. **Pod B 전환**: `ensure_model(key, skip_if_healthy=True)` 사용. port는 MODEL_METADATA에서 자동 결정
4. **Heartbeat**: 주기적으로 `test_heartbeat(detail)` 호출 (watchdog staleness 방지)
5. **Cleanup**: 종료 시 `test_complete()` 호출 — pulse resolve
6. **Cycle 재시작**: 테스트 완료 후 `systemctl --user start devforge-day-cycle.{service,timer}`

## Session End
- Update tasks via `cli.py task update` (mark done, promote pending).
- Update handover decisions/known_issues (update_handover.py auto-generates DB + YAML).
- Run `python3 /opt/projects/server/scripts/cli.py activity recent --today`.
- Present changed file list.

## NewHand Protocol
- Trigger: 사용자 지시 ("newhand 실행" 또는 "핸드오버"). 자가 판단 트리거 금지.
- Write comprehensive summary to handover DB (decisions, known_issues, current_context) via `update_handover.py` or DB tool.
- Output: `핸드오버 완료. 이 세션을 종료하고 터미널에서 newhand (또는 Pro: newhand pro)를 실행하세요.`
- Do NOT exit — let the user end the session.

