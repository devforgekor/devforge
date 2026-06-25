# Phase 2: 실패하는 테스트 (Red) 작성

**언제:** 새 기능을 추가할 때. **구현 코드보다 먼저** 실행.

## 프롬프트 (복사)

```
Read @docs/domain-glossary.yaml for domain terminology.

I need to add [FEATURE, e.g. "discount coupon application"] to [DOMAIN, e.g. "cart module"]. 
The relevant file is @src/target.py.

Rules:
1. First, list the input/output/edge-cases of this feature in a table.
2. Then write FAILING test code only — pytest style, covering normal cases AND edge cases (empty, null, special chars).
3. Do NOT write any implementation code yet.
4. After writing the test, tell me the exact terminal command to run it and verify it fails (Red).

Stop after writing the test code. Wait for my approval before implementing.
```

## 실행 후

- 터미널에서 테스트 실행해서 실패(Red) 확인
- `pytest -x --tb=short tests/test_target.py`
- 실패 확인 후 Phase 3으로 이동
