# Phase 4: 리팩토링

**언제:** Phase 3에서 테스트는 통과했지만 코드 품질이 낮을 때.

## 프롬프트 (복사)

```
All tests pass. Now refactor @src/target.py.

Requirements:
- Business logic behavior must NOT change — all existing tests must still pass.
- Remove duplicate code.
- Split large functions for readability.
- Keep the public API identical.

Output the refactored code and summarize the changes in 3 bullet points (before → after).
```

## 실행 후

- 테스트 다시 실행: `pytest -x --tb=short tests/test_target.py`
- 여전히 통과하는지 확인
- 통과하면 완료
