# AI 에이전트 규칙(교칙) 오류 분석 및 적용 보고서

- 작성: 2026-09-22 (KST)
- 작성자: opencode (build)
- 상태: 분석 완료 / 개선 미착수 (승인 대기)
- 근거 세션: `ses_f373796a6ffeQLkf1a5RwgdPaw` (claude·opencode·copilot 교칙 관리 현황, 2026-09-22 11:02–11:22)

---

## 1. 요약 (TL;DR)

1. 이전 세션이 진단한 "에이전트 교칙 오류"는 **4개 항목**이며, 본 보고서에서 **현재 상태를 재검증**했다. 4개 중 **3개는 그대로 유효**, 1개는 **세부 서술 오류**(상태값)가 있어 정정한다.
2. 추가로 **2개 드리프트**를 새로 발견했다(인프라 사본 간 불일치, `#23` 교칙 드리프트).
3. 근본 원인은 단일하다: **`AGENTS.md` 자동 생성기(`sync_gemini_rules.py`)가 2026-09-15 커밋 `bbd0589`에서 삭제**되어, 이후 수동 편집이 `AGENTS.md`와 원본 소스에 **양방향으로** 쌓였다.
4. 이 오류는 **새로 검토 중인 "`AGENTS.md` 동기화 재설계"와 동일 뿌리**다. 재설계를 먼저/함께 하면 이 오류 대부분이 구조적으로 해소된다. **단, 재생성 전에 `AGENTS.md`에만 있는 최신 내용(`#23`/`#25`)을 원본 소스로 역이관(back-port)해야 한다.**
5. 미등록 타이머 편입은 독립 작업이나, 재설계로 생길 **generator 서비스**를 watchdog `ONESHOT_RESULT_TARGETS`에 등록하는 접점이 있다.

---

## 2. 조사 방법

- 이전 세션의 11:09 응답(규칙 오류 진단) 전문을 DB에서 추출.
- 진단 4개 항목을 현재 파일/git/실행 상태와 1:1 대조.
- `AGENTS.md`를 원본 소스에서 재생성(`head -7` + `infrastructure.md` + `llm-common-rule.md` + `llm-agent-rule.md`)하여 `diff`로 드리프트 전수 확인.
- 규칙 파일 위상을 디렉터리/심링크/사본 관점에서 재구성.

---

## 3. 이전 세션 진단 재검증

| # | 이전 세션 주장 | 현재 검증 | 판정 |
|---|---|---|---|
| 1 | `AGENTS.md` 생성기 삭제 → 재생성 경로 없음, 헤더엔 "자동 생성" 문구 잔존 | `sync_gemini_rules.py` 부재(커밋 `bbd0589 auto: sync 2026-09-15`에서 삭제). `AGENTS.md:9`에 `auto-generated ... 2026-09-11 22:27 KST` 문구 그대로 | **유효** |
| 2 | 양방향 드리프트(18줄) | 실측 diff는 **4개 라인**(아래 §4-A). "18줄"은 과대 | **유효(수치 정정)** |
| 3 | Claude만 자동 반영(@import), OpenCode/Copilot은 수동 | `CLAUDE.md`→`@import` 체계 유지, `AGENTS.md`는 정적 사본 | **유효** |
| 4 | `code-structure.yaml:150`이 삭제된 `sync_gemini_rules.py`를 **production**으로 기재 | 삭제 파일을 계속 기재한 점은 유효. 단 실제 `status: experimental`(production 아님) | **유효(상태값 정정)** |

---

## 4. 현재 확인된 오류/드리프트 (증거)

### A. `AGENTS.md` ↔ 원본 소스 드리프트 (4개 라인)
재생성본과 `diff` 결과, `AGENTS.md`는 **인프라 항목은 뒤처지고, 에이전트 규칙 항목은 앞서 있다**(양방향).

| 라인 | `AGENTS.md` (사본) | 원본 소스 | 방향 |
|---|---|---|---|
| 9 | `... 2026-09-11 22:27 KST` | `infrastructure.md:2` = `2026-09-14 09:00 KST` | 사본 뒤짐 |
| 63 | `daily structure \| ... \| inactive` | `infrastructure.md:63` = `activating` | 사본 뒤짐 |
| 226 | `#23`에 "현행 구현 위치: 리팩터드 `src/.../server.py`, 2026-09-20 포팅" 포함 | `llm-agent-rule.md:72` = 포팅 서술 없음 | 사본 앞섬 |
| 228 | `#25` **완료**(base=150/240/450/420/150/810/180, `668011e`) | `llm-agent-rule.md:74` = **미착수** | 사본 앞섬 |

### B. 인프라 문서 사본 간 드리프트 (신규)
동일 헤더(09-14 09:00)인데 내용이 다름:

```
/home/opc/infrastructure.md:40-41
  - /opt/workspace (6GB) — development workspace (common-lib, minihome, archive, azure)
  - /opt/projects (10G) — DevForge server + agent system
docs/architecture/infrastructure.md:40
  - /opt/workspace (6GB) — out of scope (consolidated into /opt/projects/server/)
```

→ H2 리팩터 이후 `/opt/workspace`가 out of scope로 합쳐졌는데, 에이전트가 읽는 `/home/opc/infrastructure.md`는 반영되지 않음.

### C. `code-structure.yaml` 스테일 (신규 확인)
- `code-structure.yaml:150`에 삭제된 `sync_gemini_rules.py`가 `status: experimental`로 잔존.
- 동 파일 헤더 `:11`에 "**MANUALLY MAINTAINED since 2026-09-14** (`gen_architecture.py` retired)". 즉 자동 생성기가 이미 폐기되어 **수동 갱신 누락**이 원인(생성기 버그 아님).

### D. 규칙 파일 위상(사본 지형) — 정리
| 역할 | 경로 | 성격 |
|---|---|---|
| 인프라 정본(생성) | `docs/architecture/infrastructure.md` | state_collector 생성 |
| 인프라 사본(에이전트용) | `/home/opc/infrastructure.md` | 수동/복사, **B에서 드리프트** |
| 공통 규칙(수동) | `/home/opc/llm-common-rule.md` | SSOT |
| 에이전트 규칙(수동) | `/home/opc/llm-agent-rule.md` | SSOT |
| Claude 인덱스 | `/home/opc/CLAUDE.md` → `@import` 3종 | 참조(자동 반영) |
| Claude 진입점 | `/home/opc/.claude/CLAUDE.md`, `/opt/projects/server/CLAUDE.md` | 참조 |
| OpenCode/Copilot용 | `/home/opc/AGENTS.md` | **정적 concatenate 사본, A에서 드리프트** |

→ 인프라 내용이 **정본 + 사본 + AGENTS.md 내장** = 최대 3중으로 존재.

---

## 5. 근본 원인

```
state_collector ──> docs/architecture/infrastructure.md   (정본, 자동)
        │ (수동 복사/편집)
        ├──> /home/opc/infrastructure.md     ──┐
/home/opc/llm-common-rule.md                  ├──> (생성기 삭제됨)
/home/opc/llm-agent-rule.md  ─────────────────┘        │
                                                       ▼
                                        /home/opc/AGENTS.md  ← 정적 사본(드리프트)
```

- 생성기 삭제(2026-09-15) 이후 `AGENTS.md`는 **수동 유지보수 파일**로 전환됐으나, 헤더는 여전히 "auto-generated"로 표기(D-거짓 신호).
- 같은 기간 `AGENTS.md`에 직접 반영된 최신 정보(`#23` 포팅, `#25` 완료)와, 소스에만 반영된 정보(인프라 09-14)가 **서로 어긋난 채** 남음.
- 결과적으로 "어느 파일이 진실인가"가 판별 불가한 상태가 됨.

---

## 6. 적용 분석 — 3개 작업의 연관성

| 작업 | 내용 | 관계 |
|---|---|---|
| **A. 미등록 타이머 편입** | `config.py TIMER_TARGETS`에 6개 추가 | 독립. 단 재설계(B)가 만들 generator 서비스 감시에 재사용 |
| **B. `AGENTS.md` 동기화 재설계** | 소스 → `AGENTS.md` 생성기 + systemd `.path` unit | **C(규칙 오류)의 구조적 해결책** |
| **C. 규칙 오류 수정** | 드리프트 정정 + 스테일 문서 갱신 | 대부분 B로 흡수. B 선행 조건 포함 |

### 선후 관계 (중요)
`AGENTS.md`를 원본에서 그냥 재생성하면 **`#23`/`#25` 최신 내용이 유실**된다(사본이 앞서 있으므로). 따라서 순서는 반드시:

```
1) 역이관(back-port): AGENTS.md:226(`#23` 포팅), :228(`#25` 완료) → llm-agent-rule.md:72,74
2) 인프라 사본 정정: /home/opc/infrastructure.md ← docs/architecture/infrastructure.md
3) 재생성: AGENTS.md = 헤더 + infrastructure.md + llm-common-rule.md + llm-agent-rule.md
4) 문서 스테일: code-structure.yaml:150 sync_gemini_rules.py 제거
5) 자동화(재발 방지): 생성기 + systemd .path unit, 드리프트 시 alert
```

이 순서면 C는 B 구현에 포함되고, 별도 작업이 최소화된다.

---

## 7. 통합 개선 계획 (제안)

> **UPDATE 2026-09-22**: Option B 확정으로 Phase 1(역이관+재생성)·Phase 2(생성기+.path)는 **기각** —
> `AGENTS.md`가 수동 canonical이 되어 재생성/diff가 존재하지 않음. 대체안: `docs/reports/agent-rules-content-audit-20260922.md` §4.
> Phase 3(미등록 타이머 편입)은 유효.

### Phase 1 — 무결성 복원 (저위험, 즉시)
1. `llm-agent-rule.md`에 `AGENTS.md` 최신 2건 역이관.
2. `/home/opc/infrastructure.md`를 정본에서 재복사.
3. `code-structure.yaml`에서 삭제 파일 항목 제거.
4. `AGENTS.md:9`의 가짜 "auto-generated" 헤더를 "manual/generated by <generator>"로 정정.
5. 검증: 재생성 `diff` = 0.

### Phase 2 — 재발 방지 (B + A 접점)
1. 소스 3종 + 헤더 템플릿 → `AGENTS.md` 생성기(`gen_agents.py`, oneshot) 신설.
2. systemd `.path` unit이 소스 변경 감시 → generator 트리거(watchdog 루프에 넣지 않음).
3. watchdog `ONESHOT_RESULT_TARGETS`에 generator 서비스 등록(생성 실패만 관측).
4. 주기(예: daily) 드리프트 체크: 재생성 후 `diff != 0`이면 alert.

### Phase 3 — watchdog 타이머 편입 (A)
- `TIMER_TARGETS`에 6종 추가(별도 설계 보고서 참조), `golden-image-deploy-check`/`workspace-autocommit` 포함.
- generator가 timer라면 감시 대상에도 자동 편입.

---

## 8. 리스크 및 결정 필요 사항

| # | 항목 | 리스크/결정 |
|---|---|---|
| 1 | SSOT 방향 | "`AGENTS.md`가 마스터" vs "소스 3종이 마스터" — 이전 세션 제안(AGENTS.md 마스터)은 `@import` 체계와 충돌. **소스 마스터 + 생성기** 권장 |
| 2 | 인프라 정본 | `docs/architecture/...`(정본) vs `/home/opc/infrastructure.md`(사본) — 사본을 없애고 참조/복사 자동화 필요 |
| 3 | 데이터 유실 | 역이관(Phase 1-1) 없이 재생성 시 `#23`/`#25` 유실 확정 |
| 4 | Copilot 경로 | `.github/copilot-instructions.md` 부재 — 실제로는 cwd `AGENTS.md`/`~/.copilot/` 사용. 타깃 확정 필요 |
| 5 | 권한/경로 | `AGENTS.md`(600) 등 권한 차이, 재생성 시 보존 정책 필요 |

---

## 9. 결론

- 이전 세션의 교칙 오류 진단은 **방향과 결론이 타당**하나, 일부 수치/상태값(18줄→4줄, production→experimental)은 부정확했다.
- 오류의 단일 근본 원인은 **생성기 삭제 후 방치**이며, 이는 현재 검토 중인 **`AGENTS.md` 동기화 재설계로 구조적으로 해결**된다.
- **선행 조건**: 재생성 전 `AGENTS.md` → 소스 역이관(데이터 유실 방지).
- 미등록 타이머 편입(A)은 독립적으로 즉시 가능하며, 재설계(B)의 generator를 감시 대상으로 등록하는 접점만 갖는다.

## 부록 — 증거 명령

```bash
# A. 드리프트 전수
cd /tmp && head -7 /home/opc/AGENTS.md > gen_agents.md \
  && cat /home/opc/infrastructure.md /home/opc/llm-common-rule.md /home/opc/llm-agent-rule.md >> gen_agents.md \
  && diff /home/opc/AGENTS.md gen_agents.md

# B. 인프라 사본 간
diff /home/opc/infrastructure.md /opt/projects/server/docs/architecture/infrastructure.md

# C. 삭제 파일 참조
grep -n "sync_gemini_rules" /opt/projects/server/docs/architecture/code-structure.yaml
git -C /opt/projects/server log --oneline -1 -- scripts/sync_gemini_rules.py   # bbd0589
```
