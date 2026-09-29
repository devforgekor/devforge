# PRJ 세션 핸드오버 (2026-09-29)

> Status: record · Date: 2026-09-29 · Owner: devforge
> Related: `architecture/prj.md`(정본) · `scripts/lib/prj/` · `/home/opc/agent_docs/prj.md`

> 설계·운영의 정본은 `architecture/prj.md`다. 본 문서는 **다음 세션이 미완료 항목과
> 함정을 빠르게 확인하기 위한 스냅샷**이며, 설계가 바뀌면 정본을 갱신한다.

## 상태: 완료. working tree 깨끗함.

## 세션에서 만든 것

`prj` — 웹 LLM(DeepSeek/Qwen/Duck.ai) 교차검증 CLI. API 키·과금 없이
브라우저 세션으로 검색하고 P-R-J로 합의 여부를 확인한다.

| 커밋 | 내용 |
|---|---|
| `6c58525` feat(prj) | HCP-MAD 오케스트레이션 + 방어선 7개 (최초) |
| `9b00651` fix(prj) | 재논쟁 프롬프트 상한 (총량 예산화) |
| `8121fdc` feat(prj) | 장문 `--file` + map-reduce |
| `34e59fb` feat(prj) | 크기 기반 자동 모드 선택 (플래그 불필요) |
| `f436e67` docs(prj) | `docs/architecture/prj.md` + INDEX 등록 |
| +5건 | `chore(state)` |

## 사용

```bash
prj run "<질문>"                        # 일반 — 사용자가 "prj" 명시 시에만
prj run "<질문>" --file <경로>            # 장문 (50k 초과 시 자동 map-reduce)
prj status|brief <task_id>
prj decide <task_id> --verdict confirmed --by user --note "원문 확인" --promote
```

## 미확인 (다음 세션이 볼 것)

1. **Duck.ai J 경로 미검증** — `DuckAIEngine`가 `cli.py research ask`를 호출하지만
   실제 호출을 돌려본 적이 없다. 프록시 과금 때문에 E2E를 미뤘다.
   → 실패해도 프록시만 아끼면 된다. 어댑터만 고치면 됨.
2. **openclaw 미연동** — `~/.openclaw/openclaw.json`에 `mcp` 키가 아예 없고
   plugin 5개만 있다. MCP가 아니라 plugin 방식이라 별도 작업.
3. **reduce 분할 미구현** — 청크 20개 초과 시 reduce 입력이 50k 초과. 필요 시 그때.
4. **`web-llm-cli.sh` 미버전관리** — 개인 자산이라 git 밖. 백업 파일만 존재.

## 알아둘 함정 (재발하면 조용히 실패)
1. `type_into`의 `2>&1` — 브라우저 오류를 버리고 5회 재시도. "느린 것"이 아니라
   "입력이 잘린 것"이다. 실측 경계 55.8k자 통과 / 58.5k자 실패.
2. argv 252KB에서 `Argument list too long` — `--file`로 우회.
3. awk `RS=""`는 문단 경계만 자른다 — 개행 없는 초장문은 문장 종결 기호로 2차 분할 필요.
4. 이 계정의 `tr`는 user 래퍼 — `-d '[:space:]'`, `\0` 인자를 오류로 해석.
5. grep/sed 부정 브래킷 안의 `\[ \]`가 확장 브래킷 표현식으로 오인되어 매칭 0건.

## 서버 상태

`devforge-inference` unreachable (Connection reset). PRJ는 로컬 모델을 쓰지 않아
영향 없음. 다른 파이프라인에 영향 가능.

## 교훈 (다음 세션에)

- **unit test는 E2E를 대체하지 못한다.** 42개 통과 상태에서 실전으로 4개 결함 발견
  (출처 0건 합의 통과 / 반박문 오인 / handoff 상태 불일치 / 6분 소요).
- **"제거"라고 표현하지 말 것.** 사고 과정 유출을 막은 것이지 사고를 끊은 게 아니다.
  실측: thinking 2,059자 수행, 최종 본문 203자만 전달.
- **추측이 결함을 만들었다.** "신뢰성 문제"로 뭉뚱그렸는데 실제로는 셀렉터 파싱 버그.
