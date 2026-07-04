# classify_conflict — 4B v3 (arXiv guidelines, abstract pseudocode)

```
Write Python code. Output ONLY code. No markdown, no backticks, no explanation.

You must write function classify_conflict(result_a, result_b):

from difflib import SequenceMatcher
from typing import List, Dict, Tuple

def classify_conflict(result_a: List[Dict], result_b: List[Dict]) -> Tuple[str, List[Dict]]:
    """Compare two fact lists. Return (status, facts)."""

The input dicts have keys: subject, predicate, object, chunk_id.

Algorithm:
1. Both empty -> ("EMPTY", [])
2. One empty -> ("CONFLICT", non-empty list)
3. Normalize (lower, strip): subject + " " + predicate + " " + object
4. Use SequenceMatcher(None, s1, s2).ratio() >= 0.85 for semantic match
5. Check: all facts in A match at least one in B? All B match one in A?
6. Both match and len equal -> ("CONSENSUS", result_a)
7. All A in B but len(B) > len(A) -> ("CONSENSUS", result_b)
8. All B in A but len(A) > len(B) -> ("CONFLICT", result_a)
9. Otherwise -> ("CONFLICT", result_a + result_b)
```

- system: "You are a Python expert. Output ONLY valid Python code. No markdown, no backticks, no explanation."
- temp=0.0, max_tokens=1024
- **Result**: imports/data-structure ✅, no fence ✅, nested-loop ❌ (compares only first element)
