# Handover System Audit — 문서 vs 실제 파일시스템 불일치 분석

**분석일시**: 2026-05-12T22:00+09:00
**대상 문서**: agent-system.md (최종 통합 문서)
**비교 기준**: /opt/projects/ 파일시스템 실제 상태

---

## 1. agent.sh — 문서 내 코드 vs 실제 파일 (17개 불일치)

문서(agent-system.md)에 삽입된 agent.sh 코드 블록은 **과거 버전과 현재 버전이 뒤섞인 하이브리드**다. 방어 레이어 테이블은 실제 agent.sh의 기능을 설명하지만, 코드 블록은 그 기능들이 구현되기 **전**의 상태다. 즉, 문서 내부에서 테이블과 코드가 상충한다.

| # | 문서 내 agent.sh | 실제 /opt/projects/agent.sh | 심각도 |
|---|-----------------|----------------------------|--------|
| 1 | `export TZ=UTC` 없음 | `export TZ=UTC` (4행) | 중간 |
| 2 | `LOG_FILE="$(mktemp /tmp/agent_handover_XXXXXX.log)"` | `LOG_DIR="${SCRIPT_DIR}/server/logs"`, `mktemp "${LOG_DIR}/..."` | **심각** — 문서는 이미 폐기된 /tmp 경로 사용 |
| 3 | `CURRENT_TIME="$(date -Iseconds)"` | `CURRENT_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"` | 중간 |
| 4 | `LOG_DIR` 변수 없음 | `LOG_DIR`, `mkdir -p "$LOG_DIR"` | 경미 |
| 5 | `cleanup()` 함수 및 `trap cleanup EXIT` 없음 | 존재 (28-31행) | **심각** — 비정상 종료 시 flock fd 누수 |
| 6 | `AGENT_COMMAND="$*"`, `$AGENT_COMMAND` 실행 | `AGENT_ARGS=("$@")`, `"${AGENT_ARGS[@]}"` | **심각** — 공백 포함 인자 분할 버그 |
| 7 | `grep -E -q` + `awk > patch` 이중 스캔 | `PATCH_CONTENT=$(awk ...)` 단일 패스 | 경미 |
| 8 | `stdbuf` 없음 | `stdbuf -oL -eL` + fallback | 중간 — systemd/journald 호환성 |
| 9 | `flock 200` (타임아웃 없음) | `flock -w 300 200` (5분) | **심각** — 무한 대기 가능 |
| 10 | `version_ge()` yq 버전 확인 없음 | `YQ_VERSION` + `version_ge()` 존재 | 경미 |
| 11 | git 컨텍스트 주입 (branch, commit, recent_files) 없음 | 전체 git 컨텍스트 주입 블록 존재 | 중간 |
| 12 | `. as $item select(fileIndex == 0) as $base \|` | `select(fileIndex == 0) as $base \|` (`. as $item` 없음) | **심각** — 문서의 yq 표현식은 파이프 누락으로 구문 오류 가능성 |
| 13 | yq merge에 `completed_log` 키 없음 | `.completed_log = ($patch.completed_log // $base.completed_log)` | **심각** — completed_log 병합 불가 |
| 14 | `MAX_COMPLETED=50` 캡핑 로직 없음 | 168-174행에 존재 | 중간 |
| 15 | `git add` 후 바로 commit (staged 확인 없음) | `! git diff --cached --quiet` 가드 존재 | 경미 |
| 16 | 섹션 번호: 7→9→8 (순서 엉망) | 6→7→8 순차적 | 경미 — 들여쓰기 깨짐으로 인한 구조적 오류 동반 |
| 17 | cleanup 시 `rm` 위치가 lock 해제 전 | lock 해제 후 cleanup | 경미 |

**핵심 문제**: 문서의 방어 레이어 테이블은 "Log directory isolation: server/logs/", "trap cleanup EXIT", "Word splitting prevention: ${AGENT_ARGS[@]}", "Single awk pass", "stdbuf", "flock timeout" 등을 설명하지만, 문서에 포함된 **코드 블록에는 이 중 7개가 구현되어 있지 않다**. 테이블은 실제 구현을 설명하고, 코드는 옛날 버전이다.

---

## 2. .gitignore 불일치

**문서**:
```gitignore
server/handover.yaml.bak
server/.handover.lock
server/*.patch.tmp
server/logs/
```

**실제 파일** — 다음 2개 항목이 추가로 존재:
```gitignore
server/handover_recent.yaml
server/archive/
```

누락된 두 항목은 rotate_handover.sh의 3-Tier 아카이빙 시스템이 생성하는 파일들이다. 문서의 .gitignore로는 git이 아카이브 파일을 추적하게 된다.

---

## 3. rotate_handover.sh — 문서에서 완전히 누락

3-Tier 아카이빙 시스템(rotate_handover.sh)은 **문서 어디에도 언급되지 않았다**. 방어 테이블에도 없고, 스크립트 경로도 없고, cron 설정 예시도 없다.

실제 `/opt/projects/scripts/rotate_handover.sh`:
- Tier 1: handover.yaml → handover_recent.yaml (30일)
- Tier 2: handover_recent.yaml → archive/handover_YYYY-QX.yaml (90일)
- Tier 3: 3년 초과 아카이브 삭제
- `export TZ=UTC`, `flock`, `yq` 검증 모두 포함

---

## 4. handover.yaml 구조 — 문서 예시 vs 실제

문서는 `last_checkpoint.agent` 필드를 표시하지만, **실제 파일에는 agent 필드가 없다**. 현재 handover.yaml은 update_handover.py가 마지막으로 썼기 때문이다. agent.sh는 merge 시 agent를 주입하지만 update_handover.py는 그렇지 않다. 문서는 두 기록계의 출력 차이를 반영하지 못했다.

실제 `last_checkpoint` 구조:
```yaml
last_checkpoint:
  time: '2026-05-12T21:52:29.304372+09:00'
  recent_files: [...]     # 실제 데이터 있음
  git:
    /opt/projects/seedling: {...}  # 프로젝트별 중첩 구조
```

문서의 예시:
```yaml
last_checkpoint:
  time: "2026-05-12T19:30:00+09:00"
  agent: "claude"         # 실제 파일에 없음
  recent_files: []        # 빈 배열로 표시
  git: {}                 # 빈 객체로 표시
```

실제 파일에는 `completed_log` 키가 **존재하지 않는다**. 문서 구조에는 있지만, update_handover.py가 이 필드를 출력하지 않기 때문이다. agent.sh merge 시에만 추가된다.

---

## 5. 아키텍처 다이어그램 누락 파일

문서의 아키텍처 트리에 누락된 실제 파일들:
- `server/state.yaml` — gen_server_state.py 출력
- `server/.last-structural-hash` — 변경 감지 해시
- `server/DevForge_v3.0_FINAL.md` — 마이그레이션 문서
- `server/scripts/` 디렉터리 (update_handover.py, gen_server_state.py 등)

---

## 6. 리뷰 과정에서 거부된 주장 및 기각 사유

### 6.1 "yq eval-all 구문 오류" 주장 → 거짓

여러 AI 리뷰어가 `select(fileIndex == 0) as $base |` 구문이 yq에서 동작하지 않는다고 주장했다. **yq v4.53.2 실제 테스트로 정상 동작 확인**. 제안된 대체 문법(`if/then/else/end`)은 jq 전용으로, yq에서는 `Error: lexer: invalid input text`가 발생한다. 리뷰어들은 jq와 yq의 표현식 언어 차이를 혼동했다.

### 6.2 "5개 스크립트 누락" 주장 → 거짓

리뷰어가 manual_handover.sh, vscode_handover.sh, pycharm_handover.sh, rotate_handover.sh, copilot_edits_handover.sh가 없다고 주장했다. 처음 4개는 **이미 존재**했다. copilot_edits_handover.sh는 manual_handover.sh로 충분하여 의도적으로 생성하지 않았다.

### 6.3 "cp 백업이 flock 바깥에 있다" 주장 → 거짓

`cp "$HANDOVER_FILE" "${HANDOVER_FILE}.bak"`는 처음부터 flock 블록 **내부**에 있었다. 리뷰어가 코드를 잘못 읽었다.

### 6.4 "stdbuf 적용 안 됨" 주장 → 거짓

stdbuf는 리뷰 이전부터 이미 적용되어 있었다. 리뷰어가 이론적 우려를 제기했으나 이미 `set +e`/`set -e`로 감싸 해결된 상태였다.

### 6.5 "/tmp 경로 보안 문제" → 수용 (더 근본적인 해결책 선택)

`/tmp`에 로그 파일이 남는다는 지적은 **타당**했다. 그러나 .gitignore만 수정하는 대신 로그 디렉터리를 `server/logs/`로 완전히 이전했다. 더 근본적인 해결.

### 6.6 "AGENT_COMMAND word splitting" → 수용

`$*`와 따옴표 없는 변수 사용으로 인한 공백 분할 문제는 **실제 버그**였다. `"${AGENT_ARGS[@]}"` 배열 방식으로 수정했다.

### 6.7 ".gitignore의 /tmp/ 경로" → 수용 (다른 해결책)

`.gitignore`의 `/tmp/agent_handover_*.log` 패턴이 프로젝트 상대경로만 매칭해 시스템 `/tmp`를 못 잡는다는 지적은 **기술적으로 맞다**. 로그 위치를 `server/logs/`로 옮겨 원천 해결했다.

---

## 7. 문서 전반의 구조적 문제

1. **자기모순**: 방어 레이어 테이블은 최신 버전을, 코드 블록은 구버전을 기술
2. **rotate_handover.sh 완전 누락**: 3-Tier 아카이빙은 핵심 컴포넌트인데 문서에 없음
3. **update_handover.py는 산문만**: 코드 블록이 없어 실제 구현과 비교 불가
4. **섹션 번호 붕괴**: 7→9→8 순서는 들여쓰기 손상으로 인한 마크다운 파싱 오류 가능성
5. **문서 내 agent.sh의 yq 표현식이 실제로는 오류일 가능성**: `. as $item select(...)`는 `|` 누락으로 yq에서 파싱 실패 가능
6. **cron 설정 누락**: rotate_handover.sh의 cron(0 3 * * *)과 handover-gen.timer(10분 주기) 모두 문서에 없음

---

## 8. 결론

최종 통합 문서는 **의도는 최신 버전을 담으려 했으나**, 삽입된 코드가 여러 이전 버전에서 짜깁기되어 실제 파일시스템과 17곳에서 불일치한다. 특히 `/tmp` 경로, word splitting, flock 타임아웃 누락, trap 누락은 **프로덕션에서 실제 장애로 이어질 수 있는 심각한 차이**다. 문서 기반으로 시스템을 재구축하려 하면 이전 버전의 버그가 재현된다.

검토 제출 시 핵심 포인트: **문서의 방어 레이어 테이블과 코드 블록이 상충하므로, 테이블 내용이 올바른 구현 상태(의도)이고 코드 블록은 폐기된 초안임을 명시해야 한다.**
