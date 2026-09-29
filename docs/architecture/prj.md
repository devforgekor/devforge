# PRJ — 웹 LLM 교차검증 (HCP-MAD)

> Status: active · Date: 2026-09-29 · Owner: devforge
> Related: `scripts/lib/prj/` · `code-structure.yaml` · `/home/opc/agent_docs/prj.md`

웹 LLM(DeepSeek·Qwen·Duck.ai)을 **API 키·과금 없이 브라우저 세션으로** 사용해
정보를 검색하고 교차검증한다. 에이전트는 `search` MCP(빠른 조회)를 쓰고,
**느리고 값싼 검증**은 `prj`가 담당한다.

---

## 1. 왜 별도 도구인가

`search` MCP(Brave·Tavily·you.com·Exa)는 빠르고 출처가 구조화되어 있다. 그건
바뀌지 않는다. PRJ는 그 뒤에 오는 단계다:

| | `search` MCP | `prj` |
|---|---|---|
| 소요 | 1~2초 | 2~6분 |
| 출처 | 구조적(snippet+URL) | 본문 + 인용 URL(비구조) |
| 호출 | 에이전트가 자유롭게 | **사용자가 "prj" 명시 시에만** |
| 판정 | 없음 (검색 결과) | P-R 합의 + 사람이 1차 출처 대조 |

PRJ는 "더 많은 검색 API"가 아니다. **검색한 내용을 서로 반박시켜 합의 여부를 확인하고,
그래도 갈리면 사람이 원문을 확인하는 경로**다.

## 2. 트리거 — 명시적으로만

`agent_docs/prj.md`가 실행 규칙의 정본이다. 요약:

- **"prj"를 명시한 경우에만** 실행한다: "X를 prj로 검색해줘", "prj로 검증해줘"
- "검증해줘" 단독으로는 **실행하지 않는다** (deepdive와 동일한 컨벤션)
- 이유: 2~6분 소요 + 브라우저 세션 점유. 조용히 도는 게 맞지 않는다

## 3. P-R-J 역할 배치

| 역할 | 엔진 | 근거 |
|---|---|---|
| **P** proposer | DeepSeek (web) | `--think` 사고 + 검색 |
| **R** reviewer | Qwen (web) | 이종 계열 |
| **J** adjudicator | Duck.ai (web) | **3회 불일치 시에만.** warm cache로 2회부터 비용 급감 |

**P와 R은 반드시 다른 계열이어야 한다.** 같은 모델을 쓰면 코드가 즉시 거부한다.
동종 pair는 echo-chamber를 만들어 "합의"가 무의미해지기 때문이다 (HCP-MAD).

**J는 LLM 판정을 확정에 쓰지 않는다.** `prj decide`가 유일한 승격 경로이며,
엔진 이름(`--by deepseek` 등)을 판정자로 주면 거부한다.

> 근거: 독립 인스턴스 4개가 같은 오류에 56% 일치(독립 가정 대비 140× 위반).
> 계열을 바꿔도 3-way 55%로 same-family와 통계적 차이가 없다(p=0.41).
> → LLM 합의는 검증이 아니라 재인용이다.

## 4. 3단계 — HCP-MAD

```
HCV  ── P·R 독립 실행 → 이종 pair 합의면 즉시 종료
        │               (이종 합의의 정확도 >75%, 토큰 50% 절감)
        │ 불일치
HPAD ── 상호 비판. 종료조건 3가지:
        │   consensus / answer-exchange(η_e=2) / deadlock(η_d=2)
        │   최대 3라운드
        │ 미해결
ECT  ── Duck.ai 가중투표 → 사용자에게 결정 제안 (자동 승격 아님)
```

근거: arXiv 2604.09679 (HCP-MAD), 2410.04663 (D3), 2506.06020 (SR-DCR).
고정 라운드는 토큰 2배 + 정확도 하락이므로 조기 종료가 필수다.

## 5. 방어선 (fail-closed)

| # | 방어선 | 막는 것 |
|---|---|---|
| 1 | 동종 엔진 거부 | echo-chamber 합의 |
| 2 | consensus는 verify 통과 필수 | 근거 없는 "합의" 통과 |
| 3 | 인용 URL 0건이면 claim 생성 거부 | 검증 불가 주장 |
| 4 | 반박문이 원 주장으로 채택되지 않음 | 라운드 2의 "동의 …"가 답이 됨 |
| 5 | 자기검토 금지 (R은 P와 다른 엔진) | 자기 검증 |
| 6 | J는 사람이 1차 출처 대조 | 3개 LLM 합의의 허위성 |
| 7 | 판정 후 재검토 차단 | 검증 결과 뒤집기 |

모두 E2E에서 실제 실패를 재현한 뒤 unit test로 고정했다.

## 6. 입력 제한 — 실측 경계

2026-09-29, DeepSeek web 기준:

| 경로 | 한계 |
|---|---|
| argv (기본) | 252KB에서 `Argument list too long` (ARG_MAX) |
| `--file` | 55,800자 통과 / 58,500자부터 **브라우저 타이핑이 잘려** 실패 |
| 자동 map-reduce | 162,066자 20청크 → 4분 |

```bash
prj run "<질문>" --file <경로>   # 크기만 보고 mode 자동 선택
prj run "<질문>" --file <Obsidian 디렉터리>
```

**플래그를 기억할 필요 없다.** 50,000자 초과 시 자동으로 map-reduce로 전환한다.

### 알려진 함정 3개 (재발 가능)

1. **`type_into`의 `2>&1`** — 브라우저 오류를 버리고 재시도 5회 후 실패만 알린다.
   장문 실패는 "느린 것"이 아니라 "입력이 잘린 것"이다.
2. **awk `RS=""`는 문단 경계만 자른다** — 개행 없는 초장문은 상한을 넘는다.
   문장 종결 기호 2차 분할이 필요하다.
3. **이 계정의 `tr`는 user 래퍼** — `-d '[:space:]'`, `\0` 인자를 오류로 해석한다.
   `/usr/bin/tr` 절대경로를 쓰거나 awk 안에서 처리한다.

## 7. 사용

```bash
prj run "<질문>"                          # 일반
prj run "<질문>" --file <경로>             # 장문 (자동 분할)
prj status <task_id>                      # 라운드 이력
prj brief <task_id>                       # 사용자에게 보여줄 요약
prj decide <task_id> --verdict confirmed --by user --note "원문 확인" --promote
```

`decide`는 사람이 1차 출처를 직접 연 뒤에만 부른다. 이게 없으면 신뢰 저장소에
올라가지 않는다.

### 로컬 문서의 한계

로컬 문서 질문에는 원래 출처 URL이 없다. 그래서 `verify`가 `no_sources`로 막고
`decide`로 승격할 수 없다. **문서 기반 확인은 사람이 직접 한다.** 이건 결함이
아니라 설계다 — 검색 결과에는 출처가, 로컬 문서에는 원본이 있으므로 확인 대상이 다르다.

## 8. 구현 배치

| 영역 | 위치 | 성격 |
|---|---|---|
| 오케스트레이션 | `scripts/lib/prj/` (저장소) | CI·테스트 대상 |
| 브라우저 세션 | `~/.local/share/chrome-web-llm/scripts/` | 개인 자산, 리팩터링 금지 |
| handoff 상태 | `~/.local/share/chrome-web-llm/handoffs/` | 프로바이저너별 |
| 실행 규칙 | `/home/opc/agent_docs/prj.md` | 에이전트 읽음 |
| CLI | `~/.local/bin/prj` | PATH |

저장소는 **로직만** 소유한다. 브라우저 세션은 사용자 자산이라 저장소가 아니다
(`scripts/chrome_ingest.py`의 선례와 동일).

## 9. 제한 — 알려진 미해결

- **청크 20개 초과**: reduce 입력이 다시 50,000자에 걸린다. 그때는 분할 reduce 필요.
- **시간 선형**: 162k자 4분. 1MB면 25분쯤. 그 전까지 조용히 돌아간다.
- **1개 Chromium 탭 공유**: 병렬 호출 불가. P·R은 직렬이다.
- **프론트엔드 의존**: 셀렉터 변경 시 파싱이 깨진다. 조용히 실패할 수 있어
  `is_valid_answer` 방어선이 있다.
- **같은 계열끼리의 검증은 독립성 없음**: `qwen` ↔ `qwen-cn`은 독립 검증이 아니다.

## 10. 계보 — 재사용하지 않은 것

`scripts/_archive/`에 850줄의 P-R-J 선행 구현이 있다
(`_archive/phase4/pipelines/prj_cycle.py`, `lib/prj/core.py`).
P-R-J 고정 역할을 이미 구현했으나 **dead code**로, 그대로 되살리지 않고 새로 썼다.

| 기존 | 판정 |
|---|---|
| `python_verify` (97줄) 결정론적 검증 | 개념만 참고. `_is_valid_answer`와 합쳐 새로 작성 |
| `_dedup_findings` 0.92 코사인 | 제외 — `sentence_transformers`를 호출마다 로드 |
| `_batch_p/_r/_j` (126줄) | 제외 — `llm_call(로컬 모델)`이며 web LLM으로 전부 교체 필요 |
| `run_propose_review_judge` P-R-J 1패스 | 참조 — HCP-MAD 조기 종료로 확장 |
| `J`가 `approved`/`rejected` 확정 | **반대** — §3의 근거로 사람이 판정하도록 뒤집음 |
| `_get_turn`/`_get_facts`/`_build_p_context` (227줄) | 무관 — DevForge DB 스키마, 검색·검증 용도와 무관 |

원본 `_archive`는 읽기 전용 기록으로 그대로 둔다.

## 11. 변경 이력

| 날짜 | 내용 |
|---|---|
| 2026-09-29 | 최초 구현. HCP-MAD 3단계(HCV/HPAD/ECT), 방어선 7개 |
| 2026-09-29 | 장문 지원. `--file` + 자동 map-reduce(50,000자 기준 자동 선택) |
| 2026-09-29 | `web-llm-cli.sh` 신뢰성 수정 — 본문 전용 셀렉터, thinking 유출 차단, 검색 없는 fallback 금지 |
