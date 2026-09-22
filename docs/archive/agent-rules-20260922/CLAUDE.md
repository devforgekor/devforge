@./infrastructure.md
@./llm-common-rule.md
@./llm-agent-rule.md

테스트 수행 전에 반드시 testing protocol을 확인하세요.

## Tool Routing (검색)
- **라이브러리/프레임워크 공식 문서** → `context7.get_documentation`
  예: "FastAPI 0.115의 lifespan 시그니처", "psycopg2 connection pool 옵션"
- **일반 웹 검색** → `search-proxy.web_search` (search-proxy: Brave 4키 + Tavily 4키 + youcom 폴백 = 8키 rotation)
  예: "오늘 환율", "최근 CVE", 기술 조사
- **심층 검증/분석** → `exa-search.exa_search` (semantic 검색, 고급 필터) 또는 `context7.get_documentation` (공식 문서)
  예: 논문 검증, 코드 예제 검색
- **알려진 URL의 본문 추출** → `fetch`
  예: 사용자가 링크를 직접 준 경우
- **딥다이브 분석** → `yggdrasil.deep_planning` (Sequential Thinking + Deep Planning)
- **금지**: search-proxy로 라이브러리 문서 찾기 (Context7이 더 정확)
- **금지**: fetch로 검색 (URL을 모르면 사용 불가)

## Tool Routing (시간)
- **현재 시각/타임존 변환** → `time.*` (LLM 자체 계산 금지)
- **systemd timer 해석 시 반드시 UTC↔KST 변환하여 표시
