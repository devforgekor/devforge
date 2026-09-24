# 문제 해결 (Troubleshooting)

로컬 LLM 사용 중 자주 발생하는 문제와 대응 프롬프트.

---

## 문제 1: 코드 중간에 끊김

LLM이 출력 도중 토큰 제한에 걸려 `// ... 나머지 코드` 등으로 생략할 때.

**대응 프롬프트:**

```
Do NOT skip or abbreviate the code. Continue writing [FUNCTION_NAME] from where you stopped.
Output the COMPLETE function — no "// ... rest of code" or "same as above" placeholders.
```

---

## 문제 2: 존재하지 않는 라이브러리 Import

로컬 LLM이 환각으로 없는 라이브러리를 import 할 때.

**대응 프롬프트:**

```
This project ONLY uses libraries listed in @pyproject.toml / @uv.lock (or @package.json).
Do NOT import any external library not already present in the project.
If you need a library that is not available, list the requirement and ask before adding it.
```

---

## 문제 3: 엉뚱한 파일 수정

컨텍스트가 꼬여서 지정하지 않은 파일을 수정하려 할 때.

**대응 프롬프트:**

```
Focus ONLY on these files: @src/target.py @tests/test_target.py.
Do NOT read, modify, or suggest changes to any other file.
If the task requires changes outside these files, stop and list what needs to change.
```

---

## 문제 4: 같은 내용 반복

LLM이 동일한 수정을 계속 제안할 때 (Semi-Agentic 루프 방지).

**대응 프롬프트:**

```
You suggested this change already and I rejected it. Try a different approach.
First, explain what went wrong with the previous approach in one sentence.
Then propose an alternative solution.
```

---

## 문제 5: 테스트가 계속 실패

구현 후 테스트가 통과하지 않을 때.

**대응 프롬프트:**

```
The test still fails. Here is the error log:

[PASTE ERROR LOG HERE]

Diagnose the root cause in one sentence, then fix the implementation.
Do NOT change the test — the test defines the expected behavior.
```

---

## 문제 6: 모델이 규칙을 무시

LLM이 `.cursorrules`나 `AGENTS.md`의 지시를 따르지 않을 때.

**대응 프롬프트:**

```
Re-read the project rules file at the project root.
Rule [RULE_NUMBER, e.g. "#3"] states: [RULE_CONTENT].
You are violating this rule. Correct the previous response accordingly.
```
