# 프롬프트 템플릿 — 로컬 LLM 바이브 코딩

로컬 LLM(Qwen, DeepSeek 등)에게 작업을 지시할 때 **복사해서 붙여넣기** 하는 단계별 템플릿입니다.

## 사용 흐름

1. **Phase 1:** 도메인 용어 추출 (프로젝트 첫 분석)
2. **Phase 2:** 실패하는 테스트(Red) 먼저 작성
3. **Phase 3:** 테스트 통과(Green) 구현
4. **Phase 4:** 리팩토링 (선택사항)
5. **Troubleshooting:** 문제 발생 시 대응

## 규칙

- LLM에 보내는 프롬프트는 **영어**로 작성 (llm-common-rule.md §Communication)
- 각 Phase는 독립적 — 중간에 승인 단계 필요
- `@파일명` 으로 컨텍스트를 명시적으로 좁혀야 함
