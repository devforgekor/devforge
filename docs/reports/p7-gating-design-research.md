# P7 설계 조사 보고서 — shrimp 게이팅 대체 방식

> 작성: 2026-09-11 · 목적: `shrimp-task-manager` 제거(P7) 시 **step-gating 대체 설계** 결정
> 관련: `docs/plans/mcp-consolidation-server-side.md`(P7), `docs/plans/mcp-consolidation-patch.md`
> 데이터: opencode 실사용 77콜(execute_task 24·verify_task 22·split_tasks 7·plan/analyze/reflect 각 6)

---

## 0. 질문
`shrimp`의 가치는 (a) 태스크 저장인가, (b) **step-gating(편집 전 강제 절차)** 인가? 제거 시 무엇으로 대체해야 하는가?
- 후보: (a) 완전 제거+`cli.py task` CRUD / (b) `deepdive_step_*` 패턴으로 게이팅 재구현 / (c) 현행 유지.

## 1. 웹 조사 결과

### 1.1 Plan-and-Act (arXiv 2503.09572, ICML 2025)
- 고수준 **Planner**와 저수준 **Executor** 분리가 장기(long-horizon) 과제 성공률을 크게 올림: WebArena-Lite **57.58%** vs no-planner **9.85%**.
- **핵심 경고**: *정적(static) 계획은 취약* → 실행 후 매 단계 **dynamic replanning**으로 계획을 갱신. "계획은 로드맵이되 **유연성**을 허용".
- 시사: **구조/계획은 가치 있으나, 고정·경직 게이팅은 해롭다.**

### 1.2 Structured Prompting 체크리스트 (arXiv 2605.20149, 2026)
- 체크리스트 개선 프롬프트가 **최고 점수 7.50/8**(raw 5.67, clarifying 6.67), **토큰은 가장 적고 1턴**에 수렴.
- 시사: **명시적 체크리스트/단계 검증은 품질↑·노력↓** → step 리스트 자체는 유효.

### 1.3 Workflows vs Agents (Orkes) + 다단계 실패 분석
- **워크플로우**: 재현·감사(observability)·거버넌스/체크포인트에 강함. 단 **사전 정의된 결정점 필요**, 설계 부담.
- **에이전트**: 유연·적응적이나 **예측 불가·추적 어려움**.
- 권고: **하이브리드** — "워크플로우로 에이전트를 통제, 에이전트로 유연성 주입". 프로토타입은 에이전트 → 프로덕션은 워크플로우.
- 다단계 실패: *"짧은 체인 + 단계 간 검증 + 위험 행동 human-in-the-loop + 가드레일"*.
- 반대 근거: "Why Multi-Step Prompts Fail" — **단계가 많을수록 추론 부하·희석(dilution)** → 과도한 강제는 역효과.

## 2. 검증 (주장 → 판정)
| 주장 | 판정 | 근거 |
|---|---|---|
| 단계 분리/계획이 장기 과제 성공률을 올린다 | **지지** | Plan-and-Act: 9.85%→57.58% |
| 단계 간 **검증**이 실패를 줄인다 | **지지** | arXiv 2605.20149(체크리스트 7.50/8), Orkes/k8slens(verify between steps) |
| **경직·정적 게이팅은 취약**하다 | **지지** | Plan-and-Act: static plan 한계 → dynamic replanning |
| 단계를 과도하게 강제하면 성능이 떨어진다 | **지지** | prompt dilution(다단계 실패), Orkes: 워크플로우는 설계부담·경직 |
| 기록/감사(태스크·이벤트)는 유효하다 | **지지** | Orkes: 워크플로우 traceability 우위 |

**종합**: 게이팅은 **"소프트 체크포인트"** 로서 가치가 있고, **"하드 락/정적 강제"** 로서는 해롭다.

## 3. 결론 및 권고 — **(b) 소프트 체크포인트 재구현**
shrimp의 hard gating(`execute_task`→`verify_task` 강제)을 그대로 베끼지 말고, **검증 가능한 체크포인트 + 기록**으로 대체한다. 기존 자산을 재사용하므로 신규 개발 최소:

| 목적 | 대체 수단 (기존) |
|---|---|
| 태스크 저장/상태 | `tasks` DB + `cli.py task` (SSOT) |
| step 체크포인트·hang 감지 | `deepdive_steps` + `deepdive_step_*` (이미 구현) |
| 검증 기록 | `cli.py task` 상태 + `obs_write`(test_result) 또는 verify 이벤트 |
| 변경 영향 사전검사 | `lsp blast_radius` (이미 규칙) |

**설계 원칙**
1. **강제 락 금지**: 편집을 *차단*하지 않는다. 상태·기록으로 **가시화**만.
2. **경로 분리**: 국소 변경=`cli.py task`(가벼움), 다중 파일/아키텍처=`Deep Dive`(`deepdive_steps` 게이팅 + heartbeat).
3. **검증 우선**: 편집 후 `lsp get_diagnostics`/`pytest` 결과를 `tasks`/`obs`에 기록(증거 기반).
4. **경량**: 툴 증가 금지 — 기존 `cli.py task`로 충분(필요 시 `depends_on`/`verify` 컬럼만 추가).

## 4. 적용 설계 (P7)
- **제거**: `shrimp-task-manager` MCP (`opencode.json` disabled → 파일/등록 제거).
- **규칙 개정** (`llm-agent-rule.md` → `AGENTS.md` 재생성):
  - "코드 수정 = 반드시 shrimp 태스크" → "**국소 변경 = `cli.py task`로 추적; 다중 파일/인터페이스 = Deep Dive(`deepdive_step_*`)**; 편집 전 `lsp blast_radius`, 후 진단 기록".
  - Shrimp+LSP 섹션 → "Task+LSP" 섹션으로 개칭.
- **tasks DB 확장(선택)**: `depends_on uuid[]`, `verify_result jsonb` 컬럼(멱등 마이그레이션).
- **절감**: ~2,500 tok/turn.

## 5. 리스크
- **게이팅 상실 우려**: 하드 락 제거로 절차 누락 가능 → **Deep Dive 게이팅은 유지**(무거운 변경에 한함) + 검증 기록 의무화로 완화.
- **규칙-현실 불일치**: 규칙 개정 누락 시 에이전트가 없는 shrimp 툴 호출 → P7에서 규칙 동시 갱신.
- **태스크 세분화 손실**: shrimp `split_tasks` 대체로 `cli.py task` 다건 등록 + `depends_on`.

## 6. 대안 비교
| 방식 | 장점 | 단점 | 판정 |
|---|---|---|---|
| (a) 완전 제거+CRUD | 최경량 | 게이팅/감사 상실 | △ |
| **(b) 소프트 체크포인트**(권고) | 구조 유지·경직 회피·기존 자산 재사용 | 규칙 개정 필요 | **◎** |
| (c) 현행 유지 | 변화 없음 | MCP 토큰·중복 SSOT 유지 | ✕ |

## 7. 출처
- Plan-and-Act: arXiv 2503.09572v3 (ICML 2025) — Planner/Executor 분리, static plan 한계, dynamic replanning
- Less Back-and-Forth: arXiv 2605.20149v1 (2026) — 체크리스트 7.50/8, 턴/토큰 효율
- Orkes, "Agentic AI Explained: Workflows vs Agents" — traceability, governance, hybrid 권고
- k8slens, "The Math Behind Why Multi-Step AI Agents Fail" — 짧은 체인·단계 검증·가드레일
- "Why Multi-Step Prompts Fail? Prompt Dilution" — 과도한 단계의 역효과
- 실측: opencode DB shrimp 77콜
