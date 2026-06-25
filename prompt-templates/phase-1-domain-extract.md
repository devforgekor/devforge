# Phase 1: 도메인 용어 추출

**언제:** 새 프로젝트/모듈을 처음 분석할 때. 최초 1회만 실행.

## 프롬프트 (복사)

```
Read the project rules file first.

Analyze the directory structure and key source files under @src/ to extract the project's domain glossary.

Output a machine-readable YAML glossary at docs/domain-glossary.yaml with these fields for each term:
- term: the domain term (English, snake_case)
- definition: one-line explanation
- tables: related DB table names (if any)
- related_files: source files where this term is used

Cover at minimum:
1. Core domain entities and their relationships
2. Key value objects
3. Repository/service interfaces
4. Events or messages

Write the complete file. Do NOT skip any terms.
```

## 실행 후

- 생성된 `docs/domain-glossary.yaml` 검토
- 누락된 용어가 있으면 직접 추가
- 이후 Phase 2부터는 `@docs/domain-glossary.yaml` 참조 명시
