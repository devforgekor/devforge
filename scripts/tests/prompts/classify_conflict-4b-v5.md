# classify_conflict — 4B v5 (pseudocode + explicit loop structure)

```
Write Python function classify_conflict. Output ONLY valid Python code. No markdown, no backticks, no explanation.

Requirements:
- from difflib import SequenceMatcher
- from typing import List, Dict, Tuple

def classify_conflict(result_a: List[Dict], result_b: List[Dict]) -> Tuple[str, List[Dict]]:

Input dict keys: subject, predicate, object, chunk_id

Pseudocode:
1. if both empty -> ("EMPTY", [])
2. if only one empty -> ("CONFLICT", the non-empty one)
3. normalize each fact: subject.lower() + " " + predicate.lower() + " " + object.lower()
4. all_a_in_b = is every normalized_a string similar (ratio>=0.85) to ANY normalized_b string?
   implement as nested: for each a, check if exists b where SequenceMatcher(None, a, b).ratio() >= 0.85
5. all_b_in_a = same but reversed: every b is similar to SOME a
6. if both match and len equal -> ("CONSENSUS", result_a)
7. if all_a_in_b and len(b)>len(a) -> ("CONSENSUS", result_b)
8. if all_b_in_a and len(a)>len(b) -> ("CONFLICT", result_a)
9. otherwise -> ("CONFLICT", result_a + result_b)
```

- system: "You are a Python expert. Output ONLY valid Python code. No markdown, no backticks, no explanation."
- temp=0.0, max_tokens=1024
- **Result**: imports/data-structure ✅, no fence ✅, nested-loop ✅ (generates correct double for-loop)
- **Recommended**: Final choice. Pseudocode + explicit loop structure is sufficient for 4B to generate correct code. Next LLM (Arbiter) covers minor bugs.
