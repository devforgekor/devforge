# classify_conflict — 4B v4 (example + exact code snippet)

```
Write Python function classify_conflict. Output ONLY code. No markdown, no backticks.

from difflib import SequenceMatcher
from typing import List, Dict, Tuple

def classify_conflict(result_a: List[Dict], result_b: List[Dict]) -> Tuple[str, List[Dict]]:
    # result_a and result_b are lists of dicts with keys: subject, predicate, object, chunk_id

    # STEP 1: Both empty
    if not result_a and not result_b: return ("EMPTY", [])
    # STEP 2: One empty
    if not result_a: return ("CONFLICT", result_b)
    if not result_b: return ("CONFLICT", result_a)

    # STEP 3: Normalize each fact: subject.lower() + " " + predicate.lower() + " " + object.lower()
    def norm(f):
        return " ".join([str(f.get("subject","")).strip().lower(),
                         str(f.get("predicate","")).strip().lower(),
                         str(f.get("object","")).strip().lower()])
    na = [norm(f) for f in result_a]  # list of strings
    nb = [norm(f) for f in result_b]  # list of strings

    # STEP 4: Semantic match using SequenceMatcher(None, s1, s2).ratio() >= 0.85
    # STEP 5: NESTED all/any check — compare every element of na against every element of nb
    # Use this EXACT code for the nested check:
    all_a_in_b = all(any(SequenceMatcher(None, a, b).ratio() >= 0.85 for b in nb) for a in na)
    all_b_in_a = all(any(SequenceMatcher(None, b, a).ratio() >= 0.85 for a in na) for b in nb)

    # STEP 6-9: Determine status
    if all_a_in_b and all_b_in_a and len(result_a) == len(result_b):
        return ("CONSENSUS", result_a)
    if all_a_in_b and len(result_b) > len(result_a):
        return ("CONSENSUS", result_b)
    if all_b_in_a and len(result_a) > len(result_b):
        return ("CONFLICT", result_a)
    return ("CONFLICT", result_a + result_b)
```

- system: "You are a Python expert. Output ONLY valid Python code. No markdown, no backticks, no explanation."
- temp=0.0, max_tokens=1024
- **Result**: imports/data-structure ✅, no fence ✅, nested-loop ✅ (but examples may degrade long-prompt generation)
