#!/usr/bin/env python3
# Status: production
# Path: extract.py — submodule for LLM extraction
"""LLM extraction submodule for the Extract Pipeline.

Contains all LLM interaction code: system prompts, 8082 recovery,
_extract_edcr_freeform (section-major KV cache batch, EDC+R pipeline),
JSON parsing, EDC predicate canonicalization helpers.
"""

import json
import math
import os
import re
import sys
import threading
import time
from typing import Any, Dict, Generator, List, Optional, Tuple

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.common import strip_think
from lib.llm.json_parser import parse_llm_json, save_dlq
from lib.llm_client import call_llm
from lib.model_registry import MODEL_METADATA
from lib.pod_manager import ensure_model as _ensure_model_pod
from lib.watchdog.messenger import heartbeat

_8082_RECOVERY_LOCK = threading.Lock()

# ── 8082 Auto-Recovery ──────────────────────────────────────────

_CONNECTION_ERROR_SUBSTRINGS = (
    "Remote end closed",
    "Connection reset",
    "Connection refused",
    "Broken pipe",
    "RemoteDisconnected",
)


def _is_8082_connection_error(e: Exception) -> bool:
    err = str(e)
    if "8082" not in err and "extractor" not in err:
        return False
    return any(s in err for s in _CONNECTION_ERROR_SUBSTRINGS)


def _recover_8082() -> None:
    if not _8082_RECOVERY_LOCK.acquire(blocking=False):
        print("  [recovery] Another recovery in progress, waiting...", flush=True)
        _8082_RECOVERY_LOCK.acquire(blocking=True)
        print("  [recovery] Recovery finished by other thread", flush=True)
        _8082_RECOVERY_LOCK.release()
        return
    try:
        print("  [recovery] Reloading 8082...", flush=True)
        _ensure_model_pod("day-extractor", skip_if_healthy=False)
        print("  [recovery] 8082 ready", flush=True)
    except Exception as recover_err:
        print(f"  [recovery] 8082 reload failed: {recover_err}", flush=True)
    finally:
        _8082_RECOVERY_LOCK.release()


def _call_with_8082_retry(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        if _is_8082_connection_error(e):
            print(f"  [recovery] 8082 error: {type(e).__name__}", flush=True)
            _recover_8082()
            return fn(*args, **kwargs)
        raise


# ── Constants ────────────────────────────────────────────────────

TIMEOUT_EXTRACT = 900
MAX_TOKENS_BASE = 512  # 8B Q8 generates concise JSON; 30B MoE needed 768
TOKENS_PER_300CH = 50  # 300ch당 약 1 fact 추가, ceil 적용
TEMP_EXTRACT = 0.0
TIMEOUT_BASE = 60
TIMEOUT_PER_CHAR = 0.2
TIMEOUT_PER_TOK = 1.2  # ~0.83 tok/s decode (20% safety margin)
GEN_TIME_BUF = 90  # spike/GC/swap buffer
CAP = 1800  # hard cap (절대 초과 금지)
MIN_USEFUL_TOKENS = 256  # 이 미만이면 명시적 거절
MAX_CHARS_SOLO = 5000
MAX_INPUT_CHARS_ADVERTISED = 6500  # API 에러 메시지용 광고 한계 (실제 6,714 여유)

_SIGTERM_RECEIVED = threading.Event()


def _sigterm_handler(signum, frame):
    try:
        pid = os.getpid()
        chain = []
        for _ in range(5):
            try:
                with open(f"/proc/{pid}/status") as f:
                    for line in f:
                        if line.startswith("Name:"):
                            chain.append(line.split(":", 1)[1].strip())
                        elif line.startswith("PPid:"):
                            pid = int(line.split(":", 1)[1].strip())
                            break
            except (OSError, ValueError):
                break
        print(f"\n  [SIGTERM] from parent chain: {' > '.join(chain)}", flush=True)
    except Exception:
        print("\n  [SIGTERM] (source chain unavailable)", flush=True)
    _SIGTERM_RECEIVED.set()


# ── Optimized prompts ────────────────────────────────────────
# _SYSTEM_*_EXTRACT_FREE → 4B (simple, reduced rules)
# _SYSTEM_*_EXTRACT_FREE_8B → 8B (more specific, richer examples)

_SYSTEM_USER_EXTRACT_FREE = """\
Extract factual triples from the USER MESSAGE. Each fact: (subject, predicate=snake_case, object).

RULES:
1. Max 4 facts. Fewer clean facts > many noisy ones.
2. Predicate is snake_case (2-5 words). NO: empty, Korean, "has"/"is"/"사용합니다".
   YES: "deploys_on_port", "requires_version", "configures_timeout_to".
3. Object = extracted value. NOT a raw copy of evidence (anti-tautology).
4. Evidence = direct quote ending with period.
5. Self-contained: resolve pronouns.
6. Skip: flow markers, greetings, speculation, reasoning steps.

Output: {"extractions": [{"evidence":"...","category":"code|decision|explanation|requirement|other","subject":"...","predicate":"snake_case","object":"...","source_context":"..."}]}

Empty: {"extractions":[]}. Noise/gibberish: {"skip_verdict":"skip"}."""

_SYSTEM_TEXT_EXTRACT_FREE = """\
Extract factual triples from the ASSISTANT RESPONSE. Each fact: (subject, predicate=snake_case, object).

RULES:
1. Max 4 facts. Fewer clean facts > many noisy ones.
2. Predicate is snake_case (2-5 words). NO: empty, Korean, "has"/"is"/"사용합니다".
   YES: "deploys_on_port", "increases_to", "writes_log_to".
3. Object = extracted value. NOT a raw copy of evidence.
4. Evidence = direct quote from source ending with period.
5. Self-contained: resolve pronouns.
6. Skip: reasoning steps, speculation, flow markers.

Output: {"extractions": [{"evidence":"...","category":"code|decision|explanation|requirement|other","subject":"...","predicate":"snake_case","object":"...","source_context":"..."}]}

Empty: {"extractions":[]}."""

# ── 8B-specific prompts ──────────────────────────────────────
# 8B has higher capacity — use richer guidance for quality.
# Production (day-extractor) uses 8B Q8 → SYSTEM_DAY_EXTRACT uses these.

_SYSTEM_USER_EXTRACT_FREE_8B = """\
You are a precise fact extractor. Extract factual (subject, predicate, object) triples from the USER MESSAGE.

CATEGORY (pick the best match):
- code → function names, CLI commands, file paths, ports, config keys, literal values
- decision → design choice, rationale, trade-off accepted, alternative rejected
- explanation → causal relationship, mechanism, how something works
- requirement → constraint, dependency, version pin, prerequisite, must-have
- other → status, observation, metadata (only if none of the above fits)

PREDICATE: Concise action verb phrase in snake_case (2-5 words).
  Preferred: "increases_to", "peaked_at", "resolved_via", "decreased_to", "disabled_during", "configured_to", "replaced_with"
  Action verbs capture the relationship more precisely than stative verbs.

SUBJECT: Must be a specific entity name explicitly mentioned in the text. Avoid generic placeholders ("system", "it", "the process", "application").

OBJECT: Extract the core value in normalized form. For numbers use digits ("30000" not "thirty thousand"). When the object contains a value with a qualifier (e.g. "503 errors for 12% of requests"), extract the core as object and add details as qualifiers.

3 RULES:
1. Prioritize facts that are specific, actionable, and explicitly stated. Skip filler, greetings, reasoning traces.
2. Evidence must be a direct quote ending with a period.
3. Up to 4 facts per response. Fewer precise facts > many noisy ones.

Output ONLY valid JSON. No markdown fences.
{"extractions": [{"evidence":"...","category":"code|decision|explanation|requirement|other","subject":"specific_entity","predicate":"snake_case","object":"value","source_context":"...","qualifiers":{"key":"value"}}]}
Empty: {"extractions":[]}."""

_SYSTEM_TEXT_EXTRACT_FREE_8B = """\
You are a precise fact extractor. Extract factual (subject, predicate, object) triples from the ASSISTANT RESPONSE.

CATEGORY (pick the best match):
- code → function names, CLI commands, file paths, ports, config keys, literal values
- decision → design choice, rationale, trade-off accepted, alternative rejected
- explanation → causal relationship, mechanism, how something works
- requirement → constraint, dependency, version pin, prerequisite, must-have
- other → status, observation, metadata (only if none of the above fits)

PREDICATE: Concise action verb phrase in snake_case (2-5 words).
  Preferred: "increases_to", "peaked_at", "resolved_via", "decreased_to", "disabled_during", "configured_to", "replaced_with"
  Action verbs capture the relationship more precisely than stative verbs.

SUBJECT: Must be a specific entity name explicitly mentioned in the text. Avoid generic placeholders ("system", "it", "the process", "application").

OBJECT: Extract the core value in normalized form. For numbers use digits ("30000" not "thirty thousand"). When the object contains a value with a qualifier (e.g. "503 errors for 12% of requests"), extract the core as object and add details as qualifiers.

3 RULES:
1. Prioritize facts that are specific, actionable, and explicitly stated. Skip filler, greetings, reasoning traces.
2. Evidence must be a direct quote ending with a period.
3. Up to 4 facts per response. Fewer precise facts > many noisy ones.

Output ONLY valid JSON. No markdown fences.
{"extractions": [{"evidence":"...","category":"code|decision|explanation|requirement|other","subject":"specific_entity","predicate":"snake_case","object":"value","source_context":"...","qualifiers":{"key":"value"}}]}
Empty: {"extractions":[]}."""


# ── Chunking utility ──────────────────────────────────────────


def _split_atomic(text: str, max_chars: int = 700) -> list[str]:
    """Split text into ~max_chars chunks at sentence/paragraph boundaries."""
    paragraphs = re.split(r"\n\s*\n", text)
    chunks = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            chunks.append(para)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", para)
        current = ""
        for sent in sentences:
            if len(current) + len(sent) + 1 <= max_chars:
                current = (current + " " + sent).strip()
            else:
                if current:
                    chunks.append(current)
                current = sent
        if current:
            chunks.append(current)
    merged = []
    for c in chunks:
        if merged and len(c) < 40:
            merged[-1] += " " + c
        else:
            merged.append(c)
    return merged


# ── EDC-style predicate canonicalization helpers ──────────────

_definition_cache_edc: dict[str, str] = {}
_DEFINITION_PROMPT_EDC = (
    "Define this predicate briefly — what relation does it express?\n\nPredicate: {}"
)


def _get_predicate_definition(raw_predicate: str) -> str:
    if not raw_predicate:
        return ""
    if raw_predicate in _definition_cache_edc:
        return _definition_cache_edc[raw_predicate]
    prompt = _DEFINITION_PROMPT_EDC.format(raw_predicate)
    try:
        import urllib.request as _ur

        body = json.dumps(
            {"model": "test", "messages": [{"role": "user", "content": prompt}]}
        ).encode()
        req = _ur.Request(
            "http://127.0.0.1:8082/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            definition = data["choices"][0]["message"]["content"].strip().strip("\"'")
            if definition and len(definition) > 5:
                _definition_cache_edc[raw_predicate] = definition
                return definition
    except Exception as e:
        print(f"    [def-gen] error '{raw_predicate[:40]}': {e}")
    return raw_predicate


def _snake_case(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def _raw_pred(fact: dict) -> str:
    return fact.get("predicate_raw", fact.get("predicate", ""))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na * nb > 0 else 0.0


# ── EDC: LLM-as-judge (single 8082) ────────────────────────────

_llm_judge_stats_edc: dict[str, int] = {"calls": 0, "merged": 0, "split": 0, "uncertain": 0}
_embed_cache_edc: dict[str, list[float]] = {}


def _llm_judge(pred_a: str, pred_b: str) -> float:
    """LLM-as-judge via 8082."""
    global _llm_judge_stats_edc
    prompt = (
        f"Do these two predicates mean the same thing?\n\n"
        f"A: '{pred_a}'\nB: '{pred_b}'\n\n"
        f"Answer ONLY: equivalent | different | uncertain"
    )
    import urllib.request as _ur

    body = json.dumps({"model": "test", "messages": [{"role": "user", "content": prompt}]}).encode()
    try:
        req = _ur.Request(
            "http://127.0.0.1:8082/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            raw = data["choices"][0]["message"]["content"].strip().lower()
    except Exception:
        _llm_judge_stats_edc["uncertain"] += 1
        return 0.5
    _llm_judge_stats_edc["calls"] += 1
    if "equivalent" in raw:
        _llm_judge_stats_edc["merged"] += 1
        return 0.85
    if "different" in raw:
        _llm_judge_stats_edc["split"] += 1
        return 0.0
    _llm_judge_stats_edc["uncertain"] += 1
    return 0.5


def _embed_text_8081(text: str) -> Optional[list[float]]:
    """Embed text via 4B embed on :8081. Returns None on failure."""
    import urllib.request as _ur

    body = json.dumps({"input": text, "model": "default"}).encode()
    try:
        req = _ur.Request(
            "http://127.0.0.1:8081/v1/embeddings",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data["data"][0]["embedding"]
    except Exception as e:
        print(f"    [embed] embedding failed: {e}", flush=True)
        return None


def _cached_embed_edc(text: str) -> Optional[list[float]]:
    if not text:
        return None
    if text in _embed_cache_edc:
        return _embed_cache_edc[text]
    vec = _embed_text_8081(text)
    if vec:
        _embed_cache_edc[text] = vec
    return vec


def _group_predicates(facts: list[dict]) -> list[dict]:
    """Group similar predicates via 3-tier: SeqMatcher → Embedding → LLM-as-judge.

    Tier 1: SequenceMatcher 0.85 blocking (non-transitive)
    Tier 2: Embedding definition cosine (0.85 merge / 0.65-0.85 LLM / <0.65 split)
    Tier 3: LLM-as-judge for all 0.65-0.85 pairs (no margin skip)

    Falls back gracefully when embed server (:8081) is unavailable.
    """
    if not facts:
        return facts

    from difflib import SequenceMatcher

    global _llm_judge_stats_edc
    _llm_judge_stats_edc = {"calls": 0, "merged": 0, "split": 0, "uncertain": 0}

    # Stage 1: Non-transitive SequenceMatcher grouping
    groups = []
    for i, fa in enumerate(facts):
        pa = _snake_case(_raw_pred(fa))
        matched = False
        for g in groups:
            rep_i = min(g)
            pb = _snake_case(_raw_pred(facts[rep_i]))
            if SequenceMatcher(None, pa, pb).ratio() >= 0.85:
                g.add(i)
                matched = True
                break
        if not matched:
            groups.append({i})

    # Stage 2+3: Embedding 3-tier → LLM-as-judge for ambiguous
    rep_map: dict[int, tuple[int, set]] = {}
    for g in groups:
        if not g:
            continue
        members = [facts[i] for i in g]
        raw_counts: dict[str, int] = {}
        for m in members:
            raw = _raw_pred(m)
            raw_counts[raw] = raw_counts.get(raw, 0) + 1
        best = max(raw_counts, key=raw_counts.get)
        rep_idx = next(i for i in g if _raw_pred(facts[i]) == best)
        rep_map[id(g)] = (rep_idx, g)

    rep_ids = list(rep_map.keys())
    merged_group_ids: set[int] = set()
    embed_ok = False
    embed_count = 0
    if len(rep_ids) >= 2:
        # Try embed — if :8081 unavailable, skip to LLM-as-judge for all pairs
        test_vec = _embed_text_8081("test")
        if test_vec:
            embed_ok = True
            print(f"    [embed] checking {len(rep_ids)} group representatives...")

        for i in range(len(rep_ids)):
            if rep_ids[i] in merged_group_ids:
                continue
            ri, gi = rep_map[rep_ids[i]]
            fi = facts[ri]
            if embed_ok:
                ei = _cached_embed_edc(_get_predicate_definition(_raw_pred(fi)))
                if ei is None:
                    continue
                embed_count += 1
            for j in range(i + 1, len(rep_ids)):
                if rep_ids[j] in merged_group_ids:
                    continue
                rj, gj = rep_map[rep_ids[j]]
                fj = facts[rj]

                if embed_ok:
                    ej = _cached_embed_edc(_get_predicate_definition(_raw_pred(fj)))
                    if ej is None:
                        continue
                    embed_count += 1
                    sim = _cosine_similarity(ei, ej)
                else:
                    sim = 0.70  # force all pairs through LLM-as-judge

                if embed_ok and sim >= 0.85:
                    gi |= gj
                    merged_group_ids.add(rep_ids[j])
                    print(
                        f"    [def-embed] merged '{_raw_pred(fi)}' -> '{_raw_pred(fj)}' (cos={sim:.3f}, tier-1)"
                    )
                elif sim >= 0.65:
                    verdict = _llm_judge(_raw_pred(fi), _raw_pred(fj))
                    if verdict == 0.85:
                        gi |= gj
                        merged_group_ids.add(rep_ids[j])
                        print(
                            f"    [judge] merged '{_raw_pred(fi)}' -> '{_raw_pred(fj)}' (LLM: equivalent)"
                        )
                    elif verdict == 0.0:
                        print(
                            f"    [judge] split '{_raw_pred(fi)}' != '{_raw_pred(fj)}' (LLM: different)"
                        )
                    else:
                        print(
                            f"    [judge] uncertain '{_raw_pred(fi)}' vs '{_raw_pred(fj)}' (LLM uncertain, keeping separate)"
                        )

    final_groups = [g for g in groups if id(g) not in merged_group_ids]
    merged_count = sum(1 for g in final_groups if len(g) > 1)
    if embed_ok:
        tier = "3-tier" if embed_count >= 2 else "0-tier (all definitions failed)"
        print(f"    [embed] {tier}: {merged_count} groups formed (embed+judge)")
    elif merged_count:
        print(f"    [embed] {merged_count} groups formed (judge)")
    if _llm_judge_stats_edc["calls"] > 0:
        print(
            f"    [judge] calls={_llm_judge_stats_edc['calls']} "
            f"merged={_llm_judge_stats_edc['merged']} split={_llm_judge_stats_edc['split']} "
            f"uncertain={_llm_judge_stats_edc['uncertain']}"
        )

    # Assign canonical
    for group in final_groups:
        members = [facts[i] for i in group]
        rc = {}
        for m in members:
            r = _raw_pred(m)
            rc[r] = rc.get(r, 0) + 1
        canonical = max(rc, key=rc.get)
        canonical_snake = _snake_case(canonical)
        for idx in group:
            facts[idx]["predicate"] = canonical_snake
            facts[idx]["predicate_group"] = canonical
    return facts


def _group_entities(facts: list[dict], field: str = "subject") -> list[dict]:
    """Group equivalent entity surface forms via 3-tier: SeqMatcher → Embed → LLM.

    Normalizes subjects/objects so 'ETL pipeline processing time' and
    'etl_pipeline' resolve to the same canonical form.
    """
    if not facts:
        return facts
    from difflib import SequenceMatcher

    entities = sorted({f.get(field, "") for f in facts if f.get(field, "")})
    if len(entities) <= 1:
        return facts

    global _llm_judge_stats_edc
    _llm_judge_stats_edc = {"calls": 0, "merged": 0, "split": 0, "uncertain": 0}

    # Stage 1: SequenceMatcher blocking (case-sensitive — embed stage handles case variants)
    groups = []
    for i, ea in enumerate(entities):
        matched = False
        for g in groups:
            rep = entities[min(g)]
            if SequenceMatcher(None, ea, rep).ratio() >= 0.85:
                g.add(i)
                matched = True
                break
        if not matched:
            groups.append({i})

    # Stage 2+3: Embed 3-tier for groups with multiple distinct forms
    embed_ok = False
    embed_count = 0
    merged_group_ids: set[int] = set()
    if len(groups) >= 2:
        test_vec = _embed_text_8081("test")
        if test_vec:
            embed_ok = True

        for i in range(len(groups)):
            if id(groups[i]) in merged_group_ids:
                continue
            ea = entities[min(groups[i])]
            if embed_ok:
                ei = _cached_embed_edc(ea)
                if ei is None:
                    continue
                embed_count += 1
            for j in range(i + 1, len(groups)):
                if id(groups[j]) in merged_group_ids:
                    continue
                eb = entities[min(groups[j])]
                if embed_ok:
                    ej = _cached_embed_edc(eb)
                    if ej is None:
                        continue
                    embed_count += 1
                    sim = _cosine_similarity(ei, ej)
                else:
                    sim = 0.70

                if embed_ok and sim >= 0.85:
                    groups[i] |= groups[j]
                    merged_group_ids.add(id(groups[j]))
                elif sim >= 0.65:
                    verdict = _llm_judge_entity(ea, eb)
                    if verdict == 0.85:
                        groups[i] |= groups[j]
                        merged_group_ids.add(id(groups[j]))

    # Build entity → canonical mapping (skip dead groups)
    entity_to_canonical = {}
    for g in groups:
        if id(g) in merged_group_ids:
            continue
        members = [entities[i] for i in g]
        # Pick shortest form as canonical (most concise)
        canonical = min(members, key=lambda x: (len(x), x))
        for m in members:
            entity_to_canonical[m] = canonical

    # Apply mapping
    for f in facts:
        original = f.get(field, "")
        canonical = entity_to_canonical.get(original, original)
        if canonical != original:
            f[field] = canonical
            f[f"{field}_original"] = original

    merged = sum(1 for g in groups if len(g) > 1)
    if merged:
        print(f"    [entity-{field}] {merged} groups canonicalized ({len(entities)}→{len(groups)})")
    return facts


def _llm_judge_entity(name_a: str, name_b: str) -> float:
    """LLM-as-judge for entity equivalence via 8082."""
    global _llm_judge_stats_edc
    prompt = (
        f"Do these two entity names refer to the same real-world entity?\n\n"
        f"A: '{name_a}'\nB: '{name_b}'\n\n"
        f"Answer ONLY: equivalent | different | uncertain"
    )
    import urllib.request as _ur

    body = json.dumps({"model": "test", "messages": [{"role": "user", "content": prompt}]}).encode()
    try:
        req = _ur.Request(
            "http://127.0.0.1:8082/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            raw = data["choices"][0]["message"]["content"].strip().lower()
    except Exception:
        _llm_judge_stats_edc["uncertain"] += 1
        return 0.5
    _llm_judge_stats_edc["calls"] += 1
    if "equivalent" in raw:
        _llm_judge_stats_edc["merged"] += 1
        return 0.85
    if "different" in raw:
        _llm_judge_stats_edc["split"] += 1
        return 0.0
    _llm_judge_stats_edc["uncertain"] += 1
    return 0.5


# ── Post-processing: Multi-value Expansion ──────────────────────

_FIXED_PHRASES = frozenset({
    "research and development", "rock and roll", "back and forth",
    "up and down", "left and right", "black and white",
    "pros and cons", "dos and don ts", "by and large",
})


def _is_splittable_conjunction(obj: str) -> list[str]:
    """Split object on ' and ' if right side starts with an action verb.

    Returns [original] (no split) or [part1, part2, ...].
    """
    low = obj.lower().strip()
    if low in _FIXED_PHRASES:
        return [obj]
    if " and " not in obj:
        return [obj]
    idx = obj.index(" and ")
    left, right = obj[:idx].strip(), obj[idx + 5:].strip()
    if not left or not right:
        return [obj]
    right_words = right.split()
    _ACTION_STARTS = frozenset({"tuned", "added", "removed", "fixed", "set", "configured"})
    if right_words and right_words[0].lower() in _ACTION_STARTS:
        return [left, right]
    return [obj]


def _expand_multi_value(facts: list[dict]) -> list[dict]:
    """Expand facts whose object contains ' and ' + action into separate facts.

    No LLM calls. Expanded facts keep predicate_raw untouched; caller
    must re-run _normalize_predicate and _group_predicates.
    """
    expanded = []
    for f in facts:
        obj = f.get("object", "")
        parts = _is_splittable_conjunction(obj)
        if len(parts) <= 1:
            expanded.append(f)
            continue
        # First part keeps original fact
        first = dict(f)
        first["object"] = parts[0]
        expanded.append(first)
        # Subsequent parts become new facts
        for part in parts[1:]:
            new_f = dict(f)
            new_f["object"] = part
            expanded.append(new_f)
    return expanded


# ── Post-processing: Qualifier Splitting ────────────────────────

_QUALIFIER_PATTERNS: list[tuple[str, str]] = [
    (r",?\s*for\s+(\d+\s*%[^,]*)$", "percentage"),
    (r",?\s*during\s+(.+?)$", "context"),
    (r",?\s*of\s+(\w+\s*%)$", "percentage"),
    (r",?\s*with\s+(.+?)$", "condition"),
]


def _split_qualifiers(facts: list[dict]) -> list[dict]:
    """Extract trailing qualifier phrases from objects into qualifiers dict.

    Modifies facts in place. No LLM calls.
    """
    for f in facts:
        obj = f.get("object", "")
        if not obj:
            continue
        quals = f.get("qualifiers", {}) or {}
        for pattern, qual_key in _QUALIFIER_PATTERNS:
            m = re.search(pattern, obj, re.IGNORECASE)
            if m:
                quals[qual_key] = m.group(1).strip()
                obj = obj[:m.start()].strip().rstrip(",").strip()
        f["object"] = obj
        f["qualifiers"] = quals
    return facts


def _normalize_predicate(fact: dict) -> dict:
    """Normalize predicate: store raw, write snake_case canonical form."""
    raw = fact.get("predicate", "")
    fact["predicate_raw"] = raw
    fact["predicate"] = _snake_case(raw)
    return fact


def _dedup_post_norm(facts: list[dict]) -> list[dict]:
    """Re-dedup after normalization: collapse (subject, normalized_pred, object)."""
    seen = {}
    for f in facts:
        key = (f.get("subject", ""), f.get("predicate", ""), f.get("object", ""))
        conf = f.get("qualifiers", {}).get("confidence", 1.0)
        if key not in seen or conf > seen[key].get("qualifiers", {}).get("confidence", 0):
            seen[key] = f
    result = list(seen.values())
    result.sort(key=lambda f: f.get("qualifiers", {}).get("confidence", 1.0), reverse=True)
    return result


def _normalize_freeform_pipeline(facts: list[dict]) -> list[dict]:
    """Full EDC normalization pipeline: subject → predicate → post-process."""
    if not facts:
        return facts
    before = len(facts)
    facts = _group_entities(facts, field="subject")
    facts = _group_entities(facts, field="object")
    for f in facts:
        _normalize_predicate(f)
    raw_preds = sorted({_raw_pred(f) for f in facts})
    print(f"    Unique raw predicates ({len(raw_preds)}): {raw_preds}")
    facts = _group_predicates(facts)
    norm_preds = sorted({f.get("predicate", "") for f in facts})
    print(f"    After grouping: {len(norm_preds)} unique predicates")
    facts = _dedup_post_norm(facts)

    # ── Post-processing: multi-value expansion + qualifier split ──
    pp_before = len(facts)
    facts = _expand_multi_value(facts)
    facts = _split_qualifiers(facts)
    # Re-normalize predicates for expanded facts (no LLM — just snake_case)
    for f in facts:
        _normalize_predicate(f)
    facts = _group_predicates(facts)
    facts = _dedup_post_norm(facts)
    pp_added = len(facts) - pp_before
    if pp_added:
        print(f"    Post-process: +{pp_added} facts (multi-value + qualifier split)")
    # ───────────────────────────────────────────────────────────────

    print(f"    Dedup: {before} -> {len(facts)} (ent+pred+dedup removed {before - len(facts)})")
    return facts


SYSTEM_DESCRIBE_FILE = """\
You are a file description agent for a developer server. Given a filename,
MIME type, and file content (or first 2 KB for text files), produce a one-line
description and keyword tags for search/discovery.

Output STRICT JSON:
{
  "description": "One-line summary of what this file contains (max 15 words)",
  "tags": ["tag1", "tag2", "tag3"]
}

Rules:
- description must be factual and based only on filename, type, and content
- tags: 2-5 relevant keywords for search (include file type, source, purpose)
- For binary/non-text files, describe based on filename and mime_type alone
- For text files, use the content sample to determine the topic
- If content is empty or unreadable, describe by filename and extension only"""

SYSTEM_FALLBACK = """\
You are a fact extraction specialist handling a difficult turn. The initial extractor
failed twice to extract faithful facts from this turn — previous extractions
contained hallucinated content not present in the source. Be EXTRA cautious:

1. Verify every extracted fact exists verbatim in the source.
2. When in doubt, OMIT the fact rather than include it.
3. Prefer under-extraction over hallucination.

=== EVALUATION RUBRIC (self-assessment) ===
Rate your OWN fallback extraction on:
- Caution (0-10): Are extractions conservative — omitted when unsure?
- Faithfulness (0-10): Is every extraction verifiable in source?
- Usefulness (0-10): Does this provide value above initial extraction failures?

Output STRICT JSON:
{
  "fallback_note": "Why the initial extraction may have struggled (1 sentence)",
  "extractions": [
    {
      "fact_type": "user|thinking|text",
      "evidence": "Exact quote from the source",
      "category": "requirement|decision|explanation|code|reasoning|other"
    }
  ],
  "rubric_evaluation": {
    "caution": "0-10",
    "caution_justification": "...",
    "faithfulness": "0-10",
    "faithfulness_justification": "...",
    "usefulness": "0-10",
    "usefulness_justification": "..."
  }
}"""


# ── JSON parser ─────────────────────────────────────────────────


def _parse_json(raw: str, label: str = "LLM", attempt: int = 1) -> Optional[Dict[str, Any]]:
    cleaned = strip_think(raw)
    result = parse_llm_json(cleaned)
    if result is None:
        save_dlq(
            raw, stage=f"extract_{label}", error="parse_llm_json returned None", attempt=attempt
        )
    return result


# Backward compat aliases for test files
# Production (day-extractor 8B Q8) → 8B prompt
SYSTEM_DAY_EXTRACT = _SYSTEM_TEXT_EXTRACT_FREE_8B


def _calc_max_tokens(text_len: int) -> Optional[int]:
    """CAP 이내 실현 가능한 max_tokens로 역산. 부족 시 None 반환.

    ceil(300ch/1fact) 기반 wanted와 CAP 역산 achievable 중
    작은 쪽 선택 → 절대 캡 초과 요청 불가.
    TIMEOUT_PER_TOK=1.2는 solo section-major(동시성=1) 기준.
    """
    extra = math.ceil(text_len / 300) * TOKENS_PER_300CH
    wanted = MAX_TOKENS_BASE + extra

    overhead = TIMEOUT_BASE + int(text_len * TIMEOUT_PER_CHAR) + GEN_TIME_BUF
    if overhead >= CAP:
        print(
            json.dumps(
                {
                    "event": "max_tokens_overflow",
                    "text_len": text_len,
                    "overhead_sec": overhead,
                    "cap_sec": CAP,
                    "reason": "prefill_exceeds_cap",
                }
            ),
            flush=True,
        )
        return None  # prefill만으로 CAP 초과
    achievable = int((CAP - overhead) / TIMEOUT_PER_TOK)
    if achievable < MIN_USEFUL_TOKENS:
        print(
            json.dumps(
                {
                    "event": "max_tokens_overflow",
                    "text_len": text_len,
                    "achievable": achievable,
                    "min_useful": MIN_USEFUL_TOKENS,
                    "reason": "below_min_useful",
                }
            ),
            flush=True,
        )
        return None  # 생성 가능 token이 너무 적음 → 명시적 거절

    final = min(wanted, achievable)
    if final < wanted:
        print(
            json.dumps(
                {
                    "event": "tokens_truncated",
                    "wanted": wanted,
                    "achievable": achievable,
                    "text_len": text_len,
                }
            ),
            flush=True,
        )
    return final


def _calc_timeout(total_chars: int, max_tokens: int) -> int:
    est = (
        TIMEOUT_BASE
        + int(total_chars * TIMEOUT_PER_CHAR)
        + int(max_tokens * TIMEOUT_PER_TOK)
        + GEN_TIME_BUF
    )
    return min(est, CAP)


def _merge_usage(target: Dict[str, int], usage: Dict) -> None:
    if not usage:
        return
    for k in ("prompt_tokens", "completion_tokens"):
        v = usage.get(k, 0) or 0
        target[k] = (target.get(k, 0) or 0) + v


def _checkpoint_sections(extractions):
    return set(
        e.get("fact_type")
        for e in extractions
        if e.get("fact_type") in ("user", "thinking", "text")
    )


# ── Free-form extraction with Stop-and-Swap lazy embed ──────────


def _ensure_embed_8081() -> bool:
    """Start/ensure embed-4b on :8081 inside inference container without restart.

    Returns True if :8081 healthy. Non-blocking on existing healthy instance.
    """
    import urllib.request as _ur

    # Check if already healthy
    try:
        req = _ur.Request("http://127.0.0.1:8081/health")
        with _ur.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                return True
    except Exception:
        pass

    meta = MODEL_METADATA.get("embed-4b")
    if not meta:
        print("  [embed] FATAL: embed-4b not in MODEL_METADATA")
        return False

    port = meta["port"]
    model_file = meta["file"]
    ctx = meta.get("ctx", 2048)
    threads = meta.get("threads", 2)
    threads_batch = meta.get("threads_batch", 2)
    parallel = meta.get("parallel", 1)
    cpus = meta.get("cpus", "")

    launch_cmd = ["/app/llama-server"]
    if cpus:
        launch_cmd = ["taskset", "-c", cpus] + launch_cmd

    cmd = (
        ["podman", "exec", "-d", "devforge-inference"]
        + launch_cmd
        + [
            "-m",
            f"/models/{model_file}",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--ctx-size",
            str(ctx),
            "--parallel",
            str(parallel),
            "--threads",
            str(threads),
            "--threads-batch",
            str(threads_batch),
            "--timeout",
            "28800",
            "--batch-size",
            "512",
            "--ubatch-size",
            "512",
            "--embedding",
            "--pooling",
            "last",
            "--embd-normalize",
            "-1",
            "--cont-batching",
            "--no-mmap",
            "-lv",
            "6",
            "--metrics",
        ]
    )

    print(f"  [embed] launching {model_file} on :{port} via podman exec...")
    import subprocess as _sp

    r = _sp.run(cmd, capture_output=True, timeout=30, text=True)
    if r.returncode != 0:
        print(f"  [embed] launch failed (rc={r.returncode}): {r.stderr.strip()[:200]}")
        return False

    from lib.pod_manager import wait_health as _wh

    ok = _wh(port, timeout=120)
    if ok:
        print(f"  [embed] :{port} healthy with {model_file}")
    else:
        print(f"  [embed] :{port} health timeout")
    return ok


def _stop_embed_8081() -> None:
    """Kill embed-4b llama-server on :8081."""
    import subprocess as _sp

    _sp.run(
        ["podman", "exec", "devforge-inference", "pkill", "-f", "llama-server.*8081"],
        timeout=10,
        capture_output=True,
    )


def _cleanup_all_llms(keep_8082: bool = False) -> None:
    """Kill llama-server instances to free memory before enrich.

    If keep_8082=True, preserves port 8082 (day-extractor) for NLI verify
    and only kills 8081 (embed).
    """
    import subprocess as _sp

    if keep_8082:
        _sp.run(
            ["podman", "exec", "devforge-inference", "pkill", "-f", "llama-server.*8081"],
            timeout=10,
            capture_output=True,
        )
        print("  [cleanup] killed embed:8081, kept day-extractor:8082", flush=True)
    else:
        _sp.run(
            ["podman", "exec", "devforge-inference", "pkill", "-f", "llama-server"],
            timeout=10,
            capture_output=True,
        )
        print("  [cleanup] all llama-server instances killed", flush=True)


def _extract_edcr_freeform(
    dual_turns: List[dict],
    pulse_context: Optional[str] = None,
    dry_run: bool = False,
) -> Generator[Tuple[dict, Optional[Dict], Optional[str]], None, None]:
    """Free-form extraction with single model (8082 parallel=2) + EDC normalization.

    Phase 1 (OIE): Single 8B Q8 on 8082, processes chunks sequentially.
      llama-server's parallel=2 handles slot scheduling internally.
    Phase 2 (EDC normalize): embed-4b on 8081 → SeqMatcher → Embed 3-tier → dedup
      Falls back to LLM-as-judge when embed server unavailable.
    Phase 3 (Cleanup): Kill embed, keep 8082 for NLI verify.

    Key design:
    1. Free-form prompts (no snake_case constraints — Taxonomy Trap fix)
    2. 400-char sentence/paragraph chunking (max 4 facts per chunk)
    3. llama-server parallel=2 provides intra-chunk slot concurrency
    4. Stop-and-Swap: start embed 8081 → EDC normalization → stop embed
    5. Cleanup: keep 8082 for NLI, free all other LLM memory
    """
    from difflib import SequenceMatcher

    from extract import _load_checkpoint, _save_checkpoint

    _ENTITY_RESOLVER_4B = """\
You are an Entity Resolver. Determine if two entity names refer to the same thing.
Answer YES only if they clearly refer to the same real-world entity.
Answer NO if they are different entities.

Examples:
- "day-extractor" vs "day-extractor model" → YES
- "extract_llm.py" vs "extract.py" → NO
- "PostgreSQL" vs "Postgres" → YES
- "GPU memory" vs "CPU memory" → NO
- "threads=4" vs "4 threads" → YES

Return ONLY valid JSON:
{"same": "yes", "reason": "why (≤10 words)"}
or
{"same": "no", "reason": "why (≤10 words)"}"""

    # Section-major extraction with chunking — single model on 8082
    def _strict_freeform(section_type: str, source_text: str) -> Optional[Dict]:
        if not source_text:
            return {"extractions": [], "usage": {}, "timings": {}, "elapsed_ms": 0}
        prompt = _SYSTEM_USER_EXTRACT_FREE_8B if section_type == "user" else _SYSTEM_TEXT_EXTRACT_FREE_8B
        max_tok = _calc_max_tokens(len(source_text))
        if max_tok is None:
            return None
        max_tok = min(4096, max_tok * 2)
        timeout = _calc_timeout(len(source_text), max_tokens=max_tok)
        try:
            meta = _call_with_8082_retry(
                call_llm,
                [{"role": "system", "content": prompt}, {"role": "user", "content": source_text}],
                model="day_extract",
                max_tokens=max_tok,
                temperature=TEMP_EXTRACT,
                timeout=timeout,
                json_mode=True,
                return_meta=True,
            )
        except Exception as e:
            print(f"  [extract] call failed: {e}", flush=True)
            return None
        raw = meta["content"]
        parsed = _parse_json(raw, f"free_{section_type}")
        if parsed is None:
            return None
        ex = parsed.get("extractions", [])
        if not isinstance(ex, list):
            return None
        for e in ex:
            e["fact_type"] = section_type
        before = len(ex)
        fixed = 0
        cleaned = []
        for e in ex:
            ev = (e.get("evidence") or "").strip()
            if not ev:
                continue
            if not ev.endswith((".", "!", "?")):
                if len(ev) < 5:
                    continue
                e["evidence"] = ev + "."
                fixed += 1
            cleaned.append(e)
        ex = cleaned
        if before != len(ex) or fixed:
            print(f"    [extract] dropped {before - len(ex)}, fixed {fixed}", flush=True)
        return {
            "extractions": ex,
            "usage": meta["usage"],
            "timings": meta["timings"],
            "elapsed_ms": meta["elapsed_ms"],
        }

    def _extract_section(section_type: str, source_getter) -> int:
        targets = [
            t
            for t in dual_turns
            if source_getter(t)
            and (section_type != "user" or len(source_getter(t).strip()) >= 15)
            and not any(
                e.get("fact_type") == section_type for e in turn_data[t["id"]]["extractions"]
            )
        ]
        if not targets:
            return 0
        if dry_run:
            for t in targets:
                print(f"  [{section_type}] {t['id'][:8]} (dry-run skip)", flush=True)
            return 0
        print(f"  [{section_type}] {len(targets)} turns (8082 parallel=2)", flush=True)

        def _process_one_turn(t: dict) -> int:
            """Process one turn: chunk → single model extraction → checkpoint."""
            src = source_getter(t)
            if not src:
                return 0
            chunks = _split_atomic(src)
            extractions: List[Dict] = []

            for ci, chunk in enumerate(chunks):
                res = _strict_freeform(section_type, chunk)
                if res and res.get("extractions"):
                    extractions.extend(res["extractions"])
                    _merge_usage(turn_data[t["id"]]["total_usage"], res.get("usage", {}))
                n = len(res.get("extractions", [])) if res else 0
                print(f"      {section_type} ch{ci}/{len(chunks)}: {n} facts", flush=True)

            if not extractions:
                print(f"    [{t['id'][:8]}] returned empty", flush=True)
                return 0

            turn_data[t["id"]]["extractions"].extend(extractions)
            n_chunks = len(chunks)
            print(
                f"    [{t['id'][:8]}] => {len(extractions)} facts ({n_chunks} chunks)",
                flush=True,
            )
            if not dry_run:
                _save_checkpoint(t["id"], turn_data[t["id"]]["extractions"])
            return 1

        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import as_completed as _as_completed

        # Controlled 2-slot dispatch: match llama-server parallel=2.
        # Submit all turns — ThreadPoolExecutor queue holds excess.
        # When a slot finishes, next turn auto-starts (no slot-level
        # partitioning in llama-server, so cap at 2 to bound contention).
        n_w = min(2, len(targets))
        with ThreadPoolExecutor(max_workers=n_w) as exe:
            fut_to_idx = {exe.submit(_process_one_turn, t): i for i, t in enumerate(targets)}
            counts = []
            for f in _as_completed(fut_to_idx):
                try:
                    counts.append(f.result())
                except Exception:
                    counts.append(0)
        return sum(counts)

    turn_data: Dict[str, dict] = {}
    for t in dual_turns:
        ckpt = _load_checkpoint(t["id"]) if not dry_run else None
        if ckpt:
            turn_data[t["id"]] = {"extractions": ckpt, "total_usage": {}, "skip": False}
        else:
            turn_data[t["id"]] = {"extractions": [], "total_usage": {}, "skip": False}

    t0 = time.monotonic()

    _extract_section("user", lambda t: t.get("user_turn", "") or "")
    heartbeat("day_extract", "free user done")
    time.sleep(6)

    _extract_section("text", lambda t: t.get("text") or "")
    heartbeat("day_extract", f"free text done, total={time.monotonic() - t0:.0f}s")

    # ── Cross-section dedup ──
    for t in dual_turns:
        all_facts = turn_data[t["id"]]["extractions"]
        if len(all_facts) < 2:
            continue
        before = len(all_facts)
        # Exact dedup
        seen = set()
        deduped = []
        for f in all_facts:
            key = (
                f.get("subject", "").strip().lower(),
                f.get("predicate", "").strip().lower(),
                f.get("object", "").strip().lower(),
            )
            if key not in seen:
                seen.add(key)
                deduped.append(f)
        all_facts = deduped
        # Subject normalization — SeqMatcher + 8082 LLM resolver
        subjects = list(dict.fromkeys(f.get("subject", "") for f in all_facts))
        subj_map = {}
        for i, s1 in enumerate(subjects):
            for s2 in subjects[i + 1 :]:
                if s1 in subj_map or s2 in subj_map:
                    continue
                ratio = SequenceMatcher(None, s1.lower().strip(), s2.lower().strip()).ratio()
                if ratio >= 0.95:
                    canonical = s1 if len(s1) <= len(s2) else s2
                    subj_map[s1] = canonical
                    subj_map[s2] = canonical
                elif ratio >= 0.70:
                    try:
                        meta = _call_with_8082_retry(
                            call_llm,
                            [
                                {"role": "system", "content": _ENTITY_RESOLVER_4B},
                                {
                                    "role": "user",
                                    "content": f'Entity A: "{s1}"\nEntity B: "{s2}"',
                                },
                            ],
                            model="day_extract",
                            max_tokens=16,
                            temperature=0.0,
                            timeout=15,
                            json_mode=True,
                            return_meta=True,
                        )
                        verdict = _parse_json(meta["content"], "entity_resolve")
                        if verdict and str(verdict.get("same", "")).lower() == "yes":
                            canonical = s1 if len(s1) <= len(s2) else s2
                            subj_map[s1] = canonical
                            subj_map[s2] = canonical
                    except Exception as e:
                        print(f"  [entity-resolve] LLM call failed: {e}", flush=True)
        if subj_map:
            for f in all_facts:
                old_subj = f.get("subject", "")
                canonical = subj_map.get(old_subj)
                if canonical:
                    f["subject"] = canonical
            seen2 = set()
            deduped2 = []
            for f in all_facts:
                key = (
                    f.get("subject", "").strip().lower(),
                    f.get("predicate", "").strip().lower(),
                    f.get("object", "").strip().lower(),
                )
                if key not in seen2:
                    seen2.add(key)
                    deduped2.append(f)
            all_facts = deduped2
        if len(all_facts) != before:
            print(
                f"    [{t['id'][:8]}] cross-section dedup: {before} -> {len(all_facts)}",
                flush=True,
            )
            turn_data[t["id"]]["extractions"] = all_facts

    # ── Stop-and-Swap: EDC normalization pass ──
    all_fact_groups = {}
    for t in dual_turns:
        all_facts = turn_data[t["id"]]["extractions"]
        if all_facts:
            all_fact_groups[t["id"]] = all_facts

    if all_fact_groups:
        print("\n  [swap] Starting embed on :8081...", flush=True)
        embed_ok = _ensure_embed_8081()
        time.sleep(1)

        if embed_ok:
            for tid, facts in all_fact_groups.items():
                before = len(facts)
                print(f"    -- Normalization ({tid[:8]}) --", flush=True)
                facts = _normalize_freeform_pipeline(facts)
                if tid in turn_data:
                    turn_data[tid]["extractions"] = facts

        print("  [swap] Stopping embed (:8081)...", flush=True)
        _stop_embed_8081()
    else:
        print("  [swap] No facts to normalize, skipping Stop-and-Swap", flush=True)

    # ── Final cleanup: free all LLM memory (keep 8082 for NLI verify) ──
    print("\n  [cleanup] Freeing LLM instances (keeping 8082 for NLI)...", flush=True)
    _cleanup_all_llms(keep_8082=True)

    for t in dual_turns:
        td = turn_data[t["id"]]
        if td.get("skip") and not td["extractions"]:
            yield t, None, "noise skip"
        elif not td["extractions"]:
            yield t, None, "all sections returned empty"
        else:
            yield (
                t,
                {
                    "extractions": td["extractions"],
                    "usage": td["total_usage"],
                    "timings": {},
                    "elapsed_ms": 0,
                },
                None,
            )
