# Phase 3: 구현 (Green)

**언제:** Phase 2의 테스트 코드가 준비되고 실패(Red)를 확인한 후.

## 프롬프트 (복사)

```
Good. I ran the test and it failed as expected (Red confirmed).

Now write the implementation code in @src/target.py to make ALL tests pass (Green).

Rules:
- Use exactly the same interface and variable names defined in the test code.
- Do NOT modify the test file.
- Do NOT touch files outside the specified domain.
- Output the COMPLETE file content — no "// ... rest of code" or "# TODO" placeholders.
- If the implementation requires changes to other files, list them but ask before modifying.
```

## 실행 후

- 테스트 다시 실행: `pytest -x --tb=short tests/test_target.py`
- 통과(Green) 확인
- 코드가 지저분하면 Phase 4로, 아니면 완료
