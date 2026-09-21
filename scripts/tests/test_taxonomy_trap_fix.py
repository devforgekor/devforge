#!/usr/bin/env python3
# Status: experimental
# Path: none — prototype: Taxonomy Trap fix for 4B extraction
"""Test free-form extraction (Taxonomy Trap fix) + 3-stage predicate canonicalization.

Architecture:
  Phase 1: 4B dual free-form (no predicate constraints on either persona)
           Strict:8082 + Xplore:8083 (both predicate-constraint-free)
           → FactArbiter merge → LLM conflict resolve
  Phase 2: 3-stage hybrid canonicalization (EDC + OAK + CLUE+ inspired)
           2-a. snake_case conversion
           2-b. SequenceMatcher blocking (threshold=0.70)
           2-c. Embedding cosine similarity (4B Q4 on 8081) 3-tier:
                ≥0.85 confident merge / <0.65 confident split
                / 0.65-0.85 ambiguous → margin-based confidence
           2-d. LLM-as-judge (WorkStealer 8082+8083) for ambiguous pairs
           2-e. re-dedup after normalization
"""

import json
import re
import subprocess
import sys
import uuid as uuid_mod
from difflib import SequenceMatcher
from typing import Optional

sys.path.insert(0, "/opt/projects/server/scripts")

from lib.arbiter import FactStatus
from lib.llm.json_parser import parse_llm_json
from lib.llm_client import call_llm_with_retry
from lib.model_registry import MODEL_METADATA
from lib.pod_manager import ensure_dual, wait_health
from pipelines.extract_llm import (
    _CONFLICT_RESOLVER_4B,
    _calc_max_tokens,
    _calc_timeout,
)

# ════════════════════════════════════════════════════════════════════
# CHUNKING  — split turns into ~400-char chunks at sentence/paragraph
# boundaries before extraction. Max 4 facts per chunk.
# ════════════════════════════════════════════════════════════════════


def split_atomic(text, max_chars=400):
    """Split text into ~max_chars chunks at sentence/paragraph boundaries.

    Paragraphs separated by blank lines are kept intact when under max_chars.
    Long paragraphs are split at sentence boundaries near max_chars.
    Tiny trailing chunks (< 40 chars) merged into previous chunk.
    """
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


# ════════════════════════════════════════════════════════════════════
# FREE-FORM STRUCTURED FIELDS  (predicate constraints DELETED)
# ════════════════════════════════════════════════════════════════════

_STRUCTURED_FIELDS_FREE = """
Structured fields — every extraction MUST have subject, predicate, object:
  subject:   The concrete entity this fact is about (file, function, port, model, config).
  predicate: Free-form verb phrase in natural language describing the relation.
             Keep it concise. Snake_case or plain English — whichever feels natural.
  object:    The specific value, outcome, or target entity.
  qualifiers: Optional JSON for additional context (e.g., {"from": "8081"}). Omit if not needed.

Examples:
  ["the server"] ["configures port to"] ["8082"]
  ["the patch"]  ["replaces old implementation with"] ["new one"]
  ["model"]      ["runs on version"] ["3.2.1"]
"""


# ════════════════════════════════════════════════════════════════════
# TAXONOMY-TRAP-FREE PROMPTS  (ALL predicate constraints REMOVED)
# ════════════════════════════════════════════════════════════════════

SYSTEM_USER_FREE = """\
Extract factual triples (subject, predicate, object) from the USER MESSAGE.
Base each fact on information present or clearly implied in the text.

Output ONLY valid JSON. No extra text.

RULES:
1. Max 4 facts. If fewer clear facts exist, return only what exists.
2. Each evidence MUST be a sentence ending in period.
3. Self-contained: Resolve pronouns ("it runs on port 8082" -> "the server runs on port 8082").
4. No duplicates: Same fact extracted once only.
5. If uncertain, include "confidence": 0.5-0.9 in qualifiers rather than skipping.
6. Skip trivial conversation flow markers ("I see", "let me check", etc.).

Output format:
{
  "extractions": [
    {
      "evidence": "<sentence ending with .>",
      "category": "code|decision|explanation|requirement|other",
      "subject": "<entity name>",
      "predicate": "<descriptive verb phrase>",
      "object": "<value>",
      "qualifiers": {},
      "source_context": "<surrounding text>"
    }
  ]
}

If nothing extractable: {"extractions": []}.
Noise detection (gibberish, errors): {"skip_verdict": "skip"}."""

SYSTEM_TEXT_FREE = """\
Extract factual triples (subject, predicate, object) from the ASSISTANT RESPONSE (text).
Base each fact on information present or clearly implied in the text.

Output ONLY valid JSON. No extra text.

RULES:
1. Max 4 facts. If fewer clear facts exist, return only what exists.
2. Each evidence MUST be a sentence ending in period.
3. Self-contained: Resolve pronouns and implicit references.
4. No duplicates: Same fact extracted once only.
5. No fabrication: Only extract what is present or clearly implied.
6. If uncertain, include "confidence": 0.5-0.9 in qualifiers rather than skipping.
7. Skip trivial conversation flow markers.

EXAMPLES of extracting facts from narrative/analytical paragraphs:

Input paragraph (Korean analysis):
  "주요 원인을 분석하면 다음과 같습니다: 1. Watchdog 관련 문제 - 실험 시작 전 watchdog가
   자동으로 중지되지 않아서 프로세스 간 충돌 발생 - 해결 방법: runner.py에서 watchdog
   프로세스를 확인하고 자동으로 중지시키는 로직 추가 필요"
Extracted:
  {"evidence": "Watchdog가 실험 시작 전 자동으로 중지되지 않아서 프로세스 간 충돌이 발생했습니다.",
   "category": "explanation", "subject": "watchdog",
   "predicate": "did not auto-stop before experiment start", "object": "process crash"}
  {"evidence": "runner.py에 watchdog 자동 중지 로직이 필요합니다.",
   "category": "requirement", "subject": "runner.py",
   "predicate": "needs watchdog auto-stop before experiment start", "object": "watchdog process"}

Input paragraph (English explanation):
  "The extract pipeline processes sections sequentially causing excessive time.
   WorkStealer should be applied to enable parallel processing. The current
   day-extractor config uses 4 threads and CPU 0-2."
Extracted:
  {"evidence": "The extract pipeline processes sections sequentially causing excessive time.",
   "category": "code", "subject": "extract pipeline",
   "predicate": "processes sections sequentially causing", "object": "excessive time"}
  {"evidence": "WorkStealer should be applied to enable parallel processing.",
   "category": "requirement", "subject": "extract pipeline",
   "predicate": "should apply WorkStealer for", "object": "parallel processing"}

Output format:
{
  "extractions": [
    {
      "evidence": "<sentence ending with .>",
      "category": "code|decision|explanation|requirement|other",
      "subject": "<entity name>",
      "predicate": "<descriptive verb phrase>",
      "object": "<value>",
      "qualifiers": {},
      "source_context": "<surrounding text>"
    }
  ]
}

If nothing extractable: {"extractions": []}."""

SYSTEM_USER_XPLORE_FREE = """\
Extract factual triples (subject, predicate, object) from the USER MESSAGE.
Include explicit facts AND strongly implied relationships.

Output ONLY valid JSON. No extra text.

RULES:
1. HIGH RECALL: Extract explicit facts AND strongly implied relationships.
2. Each evidence MUST be a sentence ending in period.
3. Self-contained: Resolve pronouns.
4. No duplicates.
5. Max 4 facts per turn. Do NOT pad.
6. If ambiguous, include "confidence": 0.5-0.9 in qualifiers. Omit if certain.
7. Skip trivial conversation flow markers.

Output format:
{
  "extractions": [
    {
      "evidence": "<sentence ending with .>",
      "category": "code|decision|explanation|requirement|other",
      "subject": "<entity name>",
      "predicate": "<descriptive verb phrase>",
      "object": "<value>",
      "qualifiers": {},
      "source_context": "<surrounding text>"
    }
  ]
}

If nothing extractable: {"extractions": []}.
Noise detection (gibberish, errors): {"skip_verdict": "skip"}."""

SYSTEM_TEXT_XPLORE_FREE = """\
Extract factual triples (subject, predicate, object) from the ASSISTANT RESPONSE (text).
Include explicit facts AND strongly implied relationships.

Output ONLY valid JSON. No extra text.

RULES:
1. HIGH RECALL: Extract explicit facts AND strongly implied relationships.
2. Each evidence MUST be a sentence ending in period.
3. Self-contained: Resolve pronouns and implicit references.
4. No duplicates.
5. Max 4 facts per turn. Do NOT pad.
6. Do NOT fabricate. Only extract what is present or strongly implied.
7. If ambiguous, include "confidence": 0.5-0.9 in qualifiers.

EXAMPLES of high-recall extraction from narrative/analytical paragraphs:

Input paragraph (Korean analysis):
  "Phase 0에서 3개의 attempt가 모두 실패했습니다. 주요 원인:
   1) watchdog가 자동으로 중지되지 않음
   2) SIGKILL fallback 없어서 강제 종료 불가
   3) preflight 검증 누락"
Extracted (4 facts):
  {"evidence": "Phase 0에서 3개의 attempt가 모두 실패했습니다.",
   "category": "explanation", "subject": "Phase 0",
   "predicate": "had 3 failed attempts", "object": "all attempts"}
  {"evidence": "watchdog가 자동으로 중지되지 않아서 충돌이 발생했습니다.",
   "category": "explanation", "subject": "watchdog",
   "predicate": "did not auto-stop before experiment start", "object": "process crash"}
  {"evidence": "SIGKILL fallback이 없어서 프로세스 강제 종료가 불가능했습니다.",
   "category": "code", "subject": "run_pipeline()",
   "predicate": "lacks SIGKILL fallback", "object": "forceful process termination"}
  {"evidence": "preflight 검증이 누락되었습니다.",
   "category": "code", "subject": "preflight.py",
   "predicate": "missing mode file validation", "object": "mode.yaml"}

Input paragraph (model configuration):
  "The model is configured with 4 threads and CPU 0-2. It has max 8192 tokens
   context and averages 3.27 tokens/second. The day-extractor model runs on port 8082."
Extracted (3 facts):
  {"evidence": "The model is configured with 4 threads and CPU 0-2.",
   "category": "code", "subject": "model",
   "predicate": "configured with", "object": "4 threads, CPU 0-2"}
  {"evidence": "The model has max 8192 tokens context and averages 3.27 tokens/second.",
   "category": "code", "subject": "model",
   "predicate": "has performance of", "object": "8192 ctx, 3.27 tok/s"}
  {"evidence": "The day-extractor model runs on port 8082.",
   "category": "code", "subject": "day-extractor model",
   "predicate": "runs on port", "object": "8082"}

Output format:
{
  "extractions": [
    {
      "evidence": "<sentence ending with .>",
      "category": "code|decision|explanation|requirement|other",
      "subject": "<entity name>",
      "predicate": "<descriptive verb phrase>",
      "object": "<value>",
      "qualifiers": {},
      "source_context": "<surrounding text>"
    }
  ]
}

If nothing extractable: {"extractions": []}."""


# ════════════════════════════════════════════════════════════════════
# EMBEDDING SERVER (4B Q4 on 8081 via podman exec)
# ════════════════════════════════════════════════════════════════════


def _stop_embed_server():
    """Kill embed llama-server on :8081 inside inference container."""
    subprocess.run(
        ["podman", "exec", "devforge-inference", "pkill", "-f", "llama-server.*8081"],
        timeout=10,
        capture_output=True,
    )


def start_embed_server() -> bool:
    """Start embeder (Qwen3-Embedding-8B) on :8081 via podman exec (does NOT restart inference container).

    Uses same pattern as ensure_dual_extraction() — starts a new llama-server
    inside the running inference container without disturbing existing models.
    Returns True if :8081 is healthy (already running or just started).
    """
    import urllib.request

    # Quick health check
    try:
        req = urllib.request.Request("http://127.0.0.1:8081/health")
        with urllib.request.urlopen(req, timeout=3) as r:
            if r.status == 200:
                print("  [embed] :8081 already healthy — skip")
                return True
    except Exception:
        pass

    meta = MODEL_METADATA.get("embeder")
    if not meta:
        print("  [embed] FATAL: embeder not in MODEL_METADATA")
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
    r = subprocess.run(cmd, capture_output=True, timeout=30, text=True)
    if r.returncode != 0:
        print(f"  [embed] launch failed (rc={r.returncode}): {r.stderr.strip()[:200]}")
        return False

    ok = wait_health(port, timeout=120)
    if ok:
        print(f"  [embed] :{port} healthy with {model_file}")
    else:
        print(f"  [embed] :{port} health timeout")
    return ok


def embed_text(text: str) -> Optional[list[float]]:
    """Embed a single text via 4B Q4 on :8081. Returns vector or None."""
    import urllib.request

    body = json.dumps(
        {
            "input": text,
            "model": "default",
        }
    ).encode()
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8081/v1/embeddings",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data["data"][0]["embedding"]
    except Exception as e:
        print(f"    [embed] error: {e}")
        return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na * nb > 0 else 0.0


# ════════════════════════════════════════════════════════════════════
# EXTRACTION
# ════════════════════════════════════════════════════════════════════


def extract(prompt, source_text, model_key):
    """Single 4B extraction call with predicate-constraint-free fields. Retries on transient errors."""
    prompt_text = prompt + "\n\n" + _STRUCTURED_FIELDS_FREE
    total_chars = len(source_text) + len(prompt_text)
    effective_max_tokens = _calc_max_tokens(total_chars) or 512
    timeout = _calc_timeout(total_chars, effective_max_tokens)
    import time as _time

    for attempt in range(3):
        try:
            meta = call_llm_with_retry(
                [
                    {"role": "system", "content": prompt_text},
                    {"role": "user", "content": source_text},
                ],
                model=model_key,
                max_tokens=effective_max_tokens,
                temperature=0.0,
                timeout=timeout,
                json_mode=True,
                return_meta=True,
            )
            result = parse_llm_json(meta["content"])
            return result.get("extractions", []) if result else []
        except (ConnectionResetError, OSError) as e:
            if attempt < 2:
                print(f"    [retry] extract {model_key} attempt {attempt + 1}: {e}")
                _time.sleep(5 * (attempt + 1))
            else:
                raise


def resolve_conflict(fact_a, fact_b, model_key):
    """LLM conflict resolver via 4B."""
    b_ev = fact_b.get("evidence", "") or ""
    msg = (
        f"Fact A: [{fact_a.get('subject', '?')}] {fact_a.get('predicate', '?')} = {fact_a.get('object', '?')}\n"
        f"Fact B: [{fact_b.get('subject', '?')}] {fact_b.get('predicate', '?')} = {fact_b.get('object', '?')}"
    )
    if b_ev:
        msg += f"\nEvidence B: {b_ev[:300]}"
    meta = call_llm_with_retry(
        [{"role": "system", "content": _CONFLICT_RESOLVER_4B}, {"role": "user", "content": msg}],
        model=model_key,
        max_tokens=128,
        temperature=0.0,
        timeout=30,
        json_mode=True,
        return_meta=True,
    )
    return parse_llm_json(meta["content"])


# ════════════════════════════════════════════════════════════════════
# CONFLICT RESOLUTION HELPER  (shared by user/text sections)
# ════════════════════════════════════════════════════════════════════


def _resolve_refs(refs, b_facts, model_key):
    """Resolve CONFLICT refs via LLM. Returns merged fact list."""
    merged = []
    for ref in refs:
        if ref.status != FactStatus.CONFLICT:
            merged.append(dict(ref.fact))
            continue
        b_match = next(
            (
                fb
                for fb in b_facts
                if SequenceMatcher(
                    None,
                    _norm(ref.fact.get("subject", "") + " " + ref.fact.get("predicate", "")),
                    _norm(fb.get("subject", "") + " " + fb.get("predicate", "")),
                ).ratio()
                >= 0.80
            ),
            None,
        )
        if b_match:
            verdict = resolve_conflict(ref.fact, b_match, model_key)
            v = verdict.get("verdict") if verdict else None
            if v == "CONSENSUS":
                ref.status = FactStatus.CONSENSUS
                ref.fact = dict(b_match)
            elif v == "DIFFERENT_ASPECT":
                ref.status = FactStatus.UNIQUE_A
        if ref.status != FactStatus.CONFLICT:
            merged.append(dict(ref.fact))
    return merged


# ════════════════════════════════════════════════════════════════════
# PREDICATE NORMALIZATION
# ════════════════════════════════════════════════════════════════════


def _start_extract_b() -> bool:
    """Launch day-extract-b (:8083) inside inference container without restarting."""
    meta = MODEL_METADATA.get("day-extractor-b")
    if not meta:
        print("  [extract-b] FATAL: day-extractor-b not in MODEL_METADATA")
        return False
    port = meta["port"]
    model_file = meta["file"]
    ctx = meta.get("ctx", 8192)
    threads = meta.get("threads", 4)
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
            "--temp",
            "0.1",
            "--flash-attn",
            "on",
            "--timeout",
            "28800",
            "--batch-size",
            "512",
            "--ubatch-size",
            "256",
            "--cont-batching",
            "--no-mmap",
            "-lv",
            "1",
            "--metrics",
        ]
    )

    print(f"  [extract-b] launching {model_file} on :{port} via podman exec...")
    r = subprocess.run(cmd, capture_output=True, timeout=30, text=True)
    if r.returncode != 0:
        print(f"  [extract-b] launch failed (rc={r.returncode}): {r.stderr.strip()[:200]}")
        return False
    ok = wait_health(port, timeout=120)
    if ok:
        print(f"  [extract-b] :{port} healthy")
    else:
        print(f"  [extract-b] :{port} health timeout")
    return ok


def _snake_case(text: str) -> str:
    """Convert any free-form predicate to snake_case."""
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def normalize_predicate(fact: dict) -> dict:
    """Normalize predicate: store raw, write snake_case canonical form."""
    raw = fact.get("predicate", "")
    fact["predicate_raw"] = raw
    fact["predicate"] = _snake_case(raw)
    return fact


def _raw_pred(fact: dict) -> str:
    """Get raw predicate form (or fall back to snake_case)."""
    return fact.get("predicate_raw", fact.get("predicate", ""))


# ── LLM-as-judge (Stage 3) ──────────────────────────────────────────

_llm_stats: dict[str, int] = {"calls": 0, "merged": 0, "split": 0, "uncertain": 0}


def _llm_judge_similarity(pred_a: str, pred_b: str) -> Optional[float]:
    """WorkStealer LLM-as-judge: threading + queue, first response wins."""
    global _llm_stats
    prompt = (
        f"Do these two predicates mean the same thing?\n\n"
        f"A: '{pred_a}'\nB: '{pred_b}'\n\n"
        f"Answer ONLY: equivalent | different | uncertain"
    )
    import queue as _q
    import threading as _th

    q = _q.Queue()

    def _work(key):
        try:
            r = (
                call_llm_with_retry(
                    [{"role": "user", "content": prompt}],
                    model=key,
                    max_tokens=16,
                    temperature=0.0,
                    timeout=15,
                )
                .strip()
                .lower()
            )
            q.put(r)
        except Exception:
            pass

    for k in ("day_extract", "day_extract_b"):
        _th.Thread(target=_work, args=(k,), daemon=True).start()
    try:
        raw = q.get(timeout=20)
    except _q.Empty:
        _llm_stats["uncertain"] += 1
        return None
    _llm_stats["calls"] += 1
    if "equivalent" in raw:
        _llm_stats["merged"] += 1
        return 0.85
    if "different" in raw:
        _llm_stats["split"] += 1
        return 0.0
    _llm_stats["uncertain"] += 1
    return None


# ── 3-stage hybrid grouping ────────────────────────────────────────


def group_similar_predicates(
    facts: list[dict],
    seq_threshold: float = 0.85,
    use_embedding: bool = True,
) -> list[dict]:
    """3-stage hybrid predicate grouping (EDC + OAK + CLUE+ inspired).

    Stage 1: SequenceMatcher blocking (non-transitive, threshold=0.85)
             Each predicate only matches against group representatives,
             NOT through intermediate chains. This avoids false transitive
             merges on short snake_case texts.
    Stage 2: Embedding 3-tier decision:
             - >= 0.85 confident merge
             - < 0.65 confident split
             - 0.65-0.85 ambiguous → margin-based confidence check
    Stage 3: LLM-as-judge for residual ambiguous pairs
             (WorkStealer dual 8082+8083, first response wins)
    """
    global _llm_stats
    _llm_stats = {"calls": 0, "merged": 0, "split": 0, "uncertain": 0}

    if not facts:
        return facts

    # Stage 1: Non-transitive SequenceMatcher grouping
    # Each predicate directly matches against the first member of
    # each existing group only (no chaining through intermediates).
    groups = []
    for i, fa in enumerate(facts):
        pa = _snake_case(_raw_pred(fa))
        matched = False
        for g in groups:
            rep_i = min(g)
            pb = _snake_case(_raw_pred(facts[rep_i]))
            if SequenceMatcher(None, pa, pb).ratio() >= seq_threshold:
                g.add(i)
                matched = True
                break
        if not matched:
            groups.append({i})

    if not use_embedding:
        return _assign_canonical(facts, groups)

    # Stage 2 + 3: Embedding 3-tier → LLM-as-judge for ambiguous
    # Compare group representatives against each other (not "unassigned").
    # Every fact is already assigned after Stage 1 — we merge groups
    # whose representatives have semantically similar predicates.
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
    if len(rep_ids) >= 2:
        print(f"    [embed] checking {len(rep_ids)} group representatives (3-tier)...")
        merged_group_ids: set[int] = set()

        for i in range(len(rep_ids)):
            if rep_ids[i] in merged_group_ids:
                continue
            ri, gi = rep_map[rep_ids[i]]
            fi = facts[ri]
            ei = _cached_embed(_get_definition(_raw_pred(fi)))
            if ei is None:
                continue
            for j in range(i + 1, len(rep_ids)):
                if rep_ids[j] in merged_group_ids:
                    continue
                rj, gj = rep_map[rep_ids[j]]
                fj = facts[rj]
                ej = _cached_embed(_get_definition(_raw_pred(fj)))
                if ej is None:
                    continue

                sim = cosine_similarity(ei, ej)

                if sim >= 0.85:
                    # Tier 1: Confident merge
                    gi |= gj
                    merged_group_ids.add(rep_ids[j])
                    print(
                        f"    [def-embed] merged '{_raw_pred(fi)}' -> '{_raw_pred(fj)}' (cos={sim:.3f}, tier-1)"
                    )
                elif sim >= 0.65:
                    # Tier 2: Ambiguous → LLM-as-judge (all pairs in 0.65-0.85 range)
                    verdict = _llm_judge_similarity(_raw_pred(fi), _raw_pred(fj))
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
                            f"    [judge] uncertain '{_raw_pred(fi)}' vs '{_raw_pred(fj)}' (LLM: uncertain, keeping separate)"
                        )
                # else sim < 0.65: confident split, skip

    # Build final group list excluding merged ones
    final_groups = [g for g in groups if id(g) not in merged_group_ids]

    merged_count = sum(1 for g in final_groups if len(g) > 1)
    if merged_count:
        print(f"    [embed] {merged_count} groups formed (embed+judge)")
    if _llm_stats["calls"] > 0:
        print(
            f"    [judge] calls={_llm_stats['calls']} merged={_llm_stats['merged']} split={_llm_stats['split']} uncertain={_llm_stats['uncertain']}"
        )

    return _assign_canonical(facts, final_groups)


_definition_cache: dict[str, str] = {}
_DEFINITION_PROMPT = """Define this predicate briefly — what relation does it express?

Predicate: {}"""


def _get_definition(raw_predicate: str) -> str:
    """Generate a natural-language definition for a raw predicate via 4B.

    EDC-style: definition captures the semantic relation, enabling more accurate
    embedding-based comparison than raw predicate string alone.
    Falls back to the predicate itself on failure.
    """
    if not raw_predicate:
        return ""
    if raw_predicate in _definition_cache:
        return _definition_cache[raw_predicate]

    prompt = _DEFINITION_PROMPT.format(raw_predicate)
    try:
        result = call_llm_with_retry(
            [{"role": "user", "content": prompt}],
            model="day_extract",
            max_tokens=48,
            temperature=0.0,
            timeout=15,
        )
        definition = result.strip().strip("\"'")
        if definition and len(definition) > 5:
            _definition_cache[raw_predicate] = definition
            return definition
    except Exception as e:
        print(f"    [def-gen] error '{raw_predicate[:40]}': {e}")

    return raw_predicate  # fallback


_embed_cache: dict[str, list[float]] = {}


def _cached_embed(text: str) -> Optional[list[float]]:
    if not text:
        return None
    if text in _embed_cache:
        return _embed_cache[text]
    vec = embed_text(text)
    if vec:
        _embed_cache[text] = vec
    return vec


def _assign_canonical(facts: list[dict], groups: list[set]) -> list[dict]:
    """Assign canonical predicate to each group member."""
    for group in groups:
        members = [facts[i] for i in group]
        raw_counts = {}
        for m in members:
            raw = _raw_pred(m)
            raw_counts[raw] = raw_counts.get(raw, 0) + 1
        canonical = max(raw_counts, key=raw_counts.get)
        canonical_snake = _snake_case(canonical)
        for i in group:
            facts[i]["predicate"] = canonical_snake
            facts[i]["predicate_group"] = canonical
    return facts


def dedup_after_normalization(facts: list[dict]) -> list[dict]:
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


def normalize_pipeline(facts: list[dict], verbose: bool = True) -> list[dict]:
    """Full normalization pipeline: snake_case -> group (seq+embed) -> dedup."""
    if not facts:
        return facts

    for f in facts:
        normalize_predicate(f)

    raw_preds = sorted({_raw_pred(f) for f in facts})
    if verbose:
        print(f"    Unique raw predicates ({len(raw_preds)}): {raw_preds}")

    before = len(facts)
    facts = group_similar_predicates(facts, use_embedding=True)
    if verbose:
        norm_preds = sorted({f.get("predicate", "") for f in facts})
        print(f"    After grouping: {len(norm_preds)} unique predicates")
        for f in facts:
            r = _raw_pred(f)
            n = f.get("predicate", "")
            if _snake_case(r) != n:
                print(f"      NORM: '{r}' -> '{n}'")

    after = len(facts)
    facts = dedup_after_normalization(facts)
    if verbose:
        print(f"    Dedup: {before} -> {len(facts)} (group+dedup removed {before - len(facts)})")

    return facts


# ════════════════════════════════════════════════════════════════════
# DATA LOADING
# ════════════════════════════════════════════════════════════════════


def load_turns():
    raw = (
        subprocess.check_output(
            [
                "podman",
                "exec",
                "postgres",
                "psql",
                "-U",
                "devforge",
                "-d",
                "devforge_app",
                "-t",
                "-A",
                "-F",
                "|",
                "-c",
                """SELECT id, user_turn_clean, text_clean
FROM turns WHERE id IN (
  'abe91c0c-f311-48ea-a0a6-893b2ce663b9',
  'd5ce280d-ed12-4b7e-a490-a6f9a5a28607',
  'deb06ca2-9010-48cf-b964-6ae26ce3863b'
) ORDER BY created_at;""",
            ]
        )
        .decode()
        .strip()
    )
    turns = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) >= 3:
            turns.append(
                {
                    "id": uuid_mod.UUID(parts[0].strip()),
                    "user": parts[1].strip() or "",
                    "text": parts[2].strip() or "",
                }
            )
    return turns


def _triple_key(f):
    return (f.get("subject", ""), f.get("predicate", ""), f.get("object", ""))


def _norm(s):
    return re.sub(r"\s+", " ", s.lower().strip())


# ════════════════════════════════════════════════════════════════════
# TEST
# ════════════════════════════════════════════════════════════════════


def test():
    print("=" * 72)
    print("  TAXONOMY TRAP FIX: Free-form extraction + predicate normalization")
    print("  Goal: Eliminate predicate-verb constraints -> fix Strict=0")
    print("  Pipeline: free-form Strict(8082) single-port sequential")
    print("          -> norm pipeline (seq + embedding 4B Q4 on 8081)")
    print("=" * 72)

    turns = load_turns()
    print(f"\n  {len(turns)} turns loaded\n")

    print("[Phase 0] Ensure extractor on :8082...")
    ensure_dual("day-extractor", skip_if_healthy=False)

    _embed_started = False
    total_extracted = 0
    total_normalized = 0
    strict_zero_turns = []

    for t in turns:
        eid = str(t["id"])[:8]
        print(f"\n  -- [{eid}] --")

        # Sequential extraction, single port 8082 — with 400-char chunking
        results = {}
        for label, prompt, source in [
            ("user", SYSTEM_USER_FREE, t["user"]),
            ("text", SYSTEM_TEXT_FREE, t["text"]),
        ]:
            try:
                chunks = split_atomic(source)
                chunk_facts = []
                for ci, chunk in enumerate(chunks):
                    facts = extract(prompt, chunk, "day_extract")
                    chunk_facts.extend(facts)
                    if len(chunks) > 1:
                        print(f"      {label} ch{ci}/{len(chunks)}: {len(facts)} facts")
                results[label] = chunk_facts
            except Exception as e:
                print(f"    [{label}] FAILED: {e}")
                results[label] = []

        extracted = results.get("user", []) + results.get("text", [])
        total_extracted += len(extracted)

        user_count = len(results.get("user", []))
        text_count = len(results.get("text", []))
        user_chunks = len(split_atomic(t["user"]))
        text_chunks = len(split_atomic(t["text"]))
        print(
            f"    Extracted: user={user_count} ({user_chunks} chunks) text={text_count} ({text_chunks} chunks)"
        )
        if not extracted:
            strict_zero_turns.append(eid)
            print("    [NO FACTS]")
            continue

        # Phase 2: Predicate normalization
        print("    -- Normalization --")
        if not _embed_started:
            print("  [Phase 1b] Start embedding server on :8081...")
            start_embed_server()
            _embed_started = True

        normalized = normalize_pipeline(extracted, verbose=True)
        total_normalized += len(normalized)

        if normalized:
            print(f"    Final facts ({len(normalized)}):")
            for f in normalized:
                raw = f.get("predicate_raw", "?")
                norm_ = f.get("predicate", "?")
                print(f"      [{f.get('subject', '?')}] {norm_} = {f.get('object', '?')}")
                if raw != norm_:
                    print(f"        (raw: '{raw}')")
        else:
            print("    [NO FACTS]")

    print(f"\n{'=' * 72}")
    print("  SUMMARY")
    print(f"{'=' * 72}")
    print(f"  Extracted: {total_extracted} facts")
    print(f"  Normalized: {total_normalized} facts")
    if strict_zero_turns:
        print(f"  ! Strict=0 turns: {strict_zero_turns}")
    else:
        print("  [OK] Strict=0 FIXED - all turns produced facts")
    print(f"  Embed cache: {len(_embed_cache)} entries")
    if _llm_stats["calls"] > 0:
        print(f"  LLM-as-judge: {_llm_stats['calls']} calls")
        print(
            f"    merged={_llm_stats['merged']} "
            f"split={_llm_stats['split']} "
            f"uncertain={_llm_stats['uncertain']}"
        )
    print(f"{'=' * 72}")
    print("\nDone.")


def test_dual():
    """Dual-port free-form extraction: user→8082, text→8083 parallel + Stop-and-Swap lazy embed."""
    import queue as _q
    import threading as _th
    import time as _time

    print("=" * 72)
    print("  TAXONOMY TRAP FIX: Dual-port free-form + lazy embed (Stop-and-Swap)")
    print("  Goal: user→8082 + text→8083 parallel → kill 8083 → embed on 8081 → normalize")
    print("=" * 72)

    turns = load_turns()
    print(f"\n  {len(turns)} turns loaded\n")

    print("[Phase 0] Ensure dual extractors on :8082 + :8083...")
    ensure_dual("day-extractor", skip_if_healthy=False)

    total_extracted = 0
    total_normalized = 0
    strict_zero_turns = []

    for ti, t in enumerate(turns):
        eid = str(t["id"])[:8]
        print(f"\n  -- [{eid}] --")

        # Phase 1: Dual parallel chunked extraction (user→8082, text→8083)
        results = {}
        q = _q.Queue()

        def _worker(label, prompt, source, key):
            try:
                chunks = split_atomic(source)
                all_facts = []
                for ci, chunk in enumerate(chunks):
                    r = extract(prompt, chunk, key)
                    all_facts.extend(r)
                    if len(chunks) > 1:
                        print(f"      {label} ch{ci}/{len(chunks)}: {len(r)} facts")
                q.put((label, all_facts))
            except Exception as e:
                q.put((label, e))

        for label, prompt, source, key in [
            ("user", SYSTEM_USER_FREE, t["user"], "day_extract"),
            ("text", SYSTEM_TEXT_FREE, t["text"], "day_extract_b"),
        ]:
            _th.Thread(target=_worker, args=(label, prompt, source, key), daemon=True).start()

        for _ in range(2):
            try:
                label, result = q.get(timeout=3600)
                if isinstance(result, Exception):
                    print(f"    [{label}] FAILED: {result}")
                    results[label] = []
                else:
                    results[label] = result
            except _q.Empty:
                print("    [worker] TIMEOUT - one worker did not complete")

        extracted = results.get("user", []) + results.get("text", [])
        total_extracted += len(extracted)

        user_count = len(results.get("user", []))
        text_count = len(results.get("text", []))
        user_chunks = len(split_atomic(t["user"]))
        text_chunks = len(split_atomic(t["text"]))
        print(
            f"    Extracted: user={user_count} ({user_chunks} chunks) text={text_count} ({text_chunks} chunks)"
        )
        if not extracted:
            strict_zero_turns.append(eid)
            print("    [NO FACTS]")
            continue

        # [Swap] Kill 8083 → start embed 8081 (max 2 processes)
        print("    [swap] Stopping extract-b (:8083)...")
        subprocess.run(
            ["podman", "exec", "devforge-inference", "pkill", "-f", "llama-server.*8083[^0-9]"],
            timeout=10,
            capture_output=True,
        )
        _time.sleep(2)

        print("    [swap] Starting embed on :8081...")
        start_embed_server()

        # Phase 2: Normalization (8082 + 8081 only)
        print("    -- Normalization --")
        normalized = normalize_pipeline(extracted, verbose=True)
        total_normalized += len(normalized)

        if normalized:
            print(f"    Final facts ({len(normalized)}):")
            for f in normalized:
                raw = f.get("predicate_raw", "?")
                norm_ = f.get("predicate", "?")
                print(f"      [{f.get('subject', '?')}] {norm_} = {f.get('object', '?')}")
                if raw != norm_:
                    print(f"        (raw: '{raw}')")
        else:
            print("    [NO FACTS]")

        # [Restore] Kill embed → restart 8083 for next turn
        print("    [swap] Stopping embed (:8081)...")
        _stop_embed_server()

        if ti < len(turns) - 1:
            print("    [swap] Restarting extract-b (:8083)...")
            _start_extract_b()

    print(f"\n{'=' * 72}")
    print("  SUMMARY")
    print(f"{'=' * 72}")
    print(f"  Extracted: {total_extracted} facts")
    print(f"  Normalized: {total_normalized} facts")
    if strict_zero_turns:
        print(f"  ! Strict=0 turns: {strict_zero_turns}")
    else:
        print("  [OK] Strict=0 FIXED - all turns produced facts")
    print(f"  Embed cache: {len(_embed_cache)} entries")
    if _llm_stats["calls"] > 0:
        print(
            f"  LLM-as-judge: {_llm_stats['calls']} calls "
            f"merged={_llm_stats['merged']} split={_llm_stats['split']} uncertain={_llm_stats['uncertain']}"
        )
    print(f"{'=' * 72}")
    print("\nDone.")


if __name__ == "__main__":
    test_dual()
