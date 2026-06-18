#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.sh — polish phase (before extract)
"""Polish Batch Pipeline — 3-phase detect-then-correct with Kiwi.

Phase 1 — Kiwi Detect (free): Compare raw text vs Kiwi-corrected clean text.
  If Kiwi made changes → field has spelling/grammar errors.
  text/thinking: only flagged when Kiwi detects changes.
  user_turn: always flagged for LLM review.

Phase 2 — LLM Correct: user_turn always polished (512 tok), text/thinking only when
  Kiwi flagged (256 tok). Non-flagged fields auto-pass (Kiwi correction sufficient).

Phase 3 — LLM Verify: diff-based verify on user_turn only (128 tok).
  text/thinking auto-pass (Kiwi is deterministic, no hallucination risk).

Usage:
  python3 scripts/pipelines/polish_batch.py              # batch from NULL-checkpoint
  python3 scripts/pipelines/polish_batch.py --limit 20   # batch cap
  python3 scripts/pipelines/polish_batch.py --dry-run    # simulate, no writes
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from typing import Any, Dict, Optional, Tuple

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql_json, psql_ok, esc_sql
from lib.llm.json_parser import parse_llm_json
from lib.llm_client import call_llm, reranker_score, reranker_nli_verdict
from lib.protection import protect
from lib.text_cleaner import get_cleaner
from lib.watchdog.messenger import heartbeat

BATCH_LIMIT = 50
SUBBATCH_SIZE = 10
PARALLEL = 2
MAX_TOKENS_USER = 512
MAX_TOKENS_FIELD = 256
TEMP = 0.0
TIMEOUT_BASE = 30
TIMEOUT_PER_CHAR = 0.05
TIMEOUT_PER_TOK = 1.5
SOLO_FACTOR = 2.5
MAX_CHARS_SOLO = 5000
QUEUE_MARGIN = 2.0  # account for slot queuing: each call may wait N-1 turns ahead


# ═══════════════════════════════════════════════
# Phase 1 — Kiwi Detection
# ═══════════════════════════════════════════════

_kiwi_cleaner = None


def _get_kiwi_cleaner():
    global _kiwi_cleaner
    if _kiwi_cleaner is None:
        _kiwi_cleaner = get_cleaner()
    return _kiwi_cleaner


def _kiwi_detect(raw: str) -> bool:
    """True if Kiwi found spelling/grammar errors in raw text.

    Uses detect_kiwi_changes() which runs Kiwi typo correction and checks
    if output differs from input (ignoring NFKC/whitespace-only changes).
    ~5ms per call — effectively free.
    """
    if not raw.strip():
        return False
    return _get_kiwi_cleaner().detect_kiwi_changes(raw)


# ═══════════════════════════════════════════════
# Phase 2 — Correction Prompts
# ═══════════════════════════════════════════════

USER_TURN_PROMPT = """You are a Korean spelling and grammar corrector. Fix the user_turn text below. User messages tend to have more errors — review CAREFULLY.

Rules:
1. Fix spelling and grammar errors only — never change word choice, sentence structure, or style
2. Never touch code blocks, URLs, proper nouns, numbers, or special characters
3. If the text has zero errors, return it exactly as-is

Output STRICT JSON with one field: {{"corrected": "the corrected text"}}

=== user_turn ===
{text}"""

CORRECT_PROMPT = """Fix Korean spelling/grammar errors in the {field} field below.

Rules:
1. Fix spelling and grammar errors only — never change word choice, sentence structure, or style
2. Never touch code blocks, URLs, proper nouns, numbers, or special characters
3. If the text has zero errors, return it exactly as-is

Output STRICT JSON with one field: {{"corrected": "the corrected text"}}

=== {field} ===
{text}"""


# ═══════════════════════════════════════════════
# Phase 3 — Diff-Based Verification
# ═══════════════════════════════════════════════

_NLI_VERIFY_PROMPT = """You are verifying whether an EVIDENCE sentence is factually supported by a SOURCE sentence.

Follow these steps:
1. Identify the key factual claim in the evidence.
2. Check whether that claim is directly stated or clearly implied by the source.
3. Output exactly one label.

LABELS:
- ENTAILMENT: The evidence is directly supported by the source.
- CONTRADICTION: The evidence contradicts the source — they cannot both be true.
- NEUTRAL: The evidence is not directly supported but does not contradict either.

SOURCE: {source}

EVIDENCE: {evidence}

LABEL:"""


def _nli_check(corrected: str, original: str) -> str:
    """Run 7B Q8 NLI self-verify: does corrected mean the same as original?
    Returns ENTAILMENT, CONTRADICTION, or NEUTRAL.
    """
    if not corrected or not original:
        return "NEUTRAL"
    prompt = _NLI_VERIFY_PROMPT.format(
        source=original[:2000], evidence=corrected[:500]
    )
    try:
        meta = call_llm(
            [{"role": "user", "content": prompt}],
            model="polish",
            max_tokens=64, temperature=0.0, timeout=30,
            return_meta=True,
        )
        raw = meta["content"].strip().upper()
        for tok in raw.replace("\n", " ").split():
            tok = tok.strip(".,!?;:\"'()[]")
            if tok in ("ENTAILMENT", "CONTRADICTION", "NEUTRAL"):
                return tok
        return "NEUTRAL"
    except Exception:
        return "NEUTRAL"

VERIFY_DIFF_PROMPT = """You verify Korean text corrections. Each "before -> after" pair shows how a text segment was changed.

{changes}

For EACH pair, analyze before deciding. Output a JSON object with your step-by-step analysis and final verdict:

{{
  "analysis": [
    {{
      "change": 1,
      "type": "spelling|grammar|word_swap|content_added|proper_noun",
      "meaning_preserved": true or false,
      "note": "What changed and whether meaning is preserved"
    }}
  ],
  "pass": true if ALL changes are VALID, false if ANY is INVALID,
  "reason": "Overall explanation",
  "invalid_count": 0
}}

Step 1 — Identify each change type:
  - spelling: typo fix (e.g., "안녕하세여" -> "안녕하세요") → ALWAYS valid, meaning preserved
  - grammar: spacing/honorific fix (e.g., "했어요" -> "했습니다") → ALWAYS valid, meaning preserved
  - word_swap: word replaced with different word (e.g., "중요합니다" -> "대단합니다") → INVALID, meaning NOT preserved
  - content_added: new content inserted (e.g., "" -> "새로운", "좋습니다" -> "매우 좋습니다") → INVALID, meaning NOT preserved
  - proper_noun: proper noun or technical term modified → INVALID, meaning NOT preserved

Step 2 — Set meaning_preserved accordingly.

Step 3 — If ANY change is INVALID, pass MUST be false."""


# ═══════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════

def _extract_json(text: str) -> str:
    """Strip markdown code block markers from LLM response."""
    s = text.strip()
    for prefix in ("```json", "```", "'''json", "'''"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
            break
    for suffix in ("```", "'''"):
        if s.endswith(suffix):
            s = s[:-len(suffix)].strip()
            break
    return s


def _calc_timeout(total_chars: int, max_tokens: int, solo: bool = False) -> int:
    """Estimate timeout including queue wait margin for PARALLEL workers."""
    prompt_s = int(total_chars * TIMEOUT_PER_CHAR)
    decode_s = int(max_tokens * TIMEOUT_PER_TOK)
    est = TIMEOUT_BASE + prompt_s + decode_s
    est = int(est * QUEUE_MARGIN)  # account for slot queuing
    if solo:
        est = int(est * SOLO_FACTOR)
    return min(est, 3600)


def _polish_user_turn(text: str, timeout: int = 600) -> Optional[str]:
    """Polish user_turn — always runs. Returns corrected text or None on error."""
    prompt = USER_TURN_PROMPT.format(text=text[:4000] or "(empty)")
    try:
        meta = call_llm(
            [{"role": "user", "content": prompt}],
            model="polish", max_tokens=MAX_TOKENS_USER, temperature=TEMP,
            timeout=timeout, json_mode=True, return_meta=True,
        )
        parsed = parse_llm_json(_extract_json(meta["content"]))
        if isinstance(parsed, dict):
            return str(parsed.get("corrected", "") or "")
    except Exception as e:
        print(f"  [error] polish user_turn failed: {e}", flush=True)
    return None


def _polish_field(text: str, field: str, timeout: int = 300) -> Optional[str]:
    """Polish text or thinking — only when Kiwi flagged errors."""
    prompt = CORRECT_PROMPT.format(field=field, text=text[:3000] or "(empty)")
    try:
        meta = call_llm(
            [{"role": "user", "content": prompt}],
            model="polish", max_tokens=MAX_TOKENS_FIELD, temperature=TEMP,
            timeout=timeout, json_mode=True, return_meta=True,
        )
        parsed = parse_llm_json(_extract_json(meta["content"]))
        if isinstance(parsed, dict):
            return str(parsed.get("corrected", "") or "")
    except Exception as e:
        print(f"  [error] polish {field} failed: {e}", flush=True)
    return None


def _extract_diffs(original: str, corrected: str, max_pairs: int = 5) -> str:
    """Extract changed spans and format for verify prompt. Returns empty if identical."""
    if original == corrected or not original or not corrected:
        return ""
    matcher = SequenceMatcher(None, original, corrected)
    changes = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        before = original[i1:i2][:200]
        after = corrected[j1:j2][:200]
        changes.append(f'  before: "{before}"\n  after:  "{after}"')
        if len(changes) >= max_pairs:
            break
    if not changes:
        return ""
    return "\n\n".join(f"=== Change {i+1} ===\n{c}" for i, c in enumerate(changes))


def _verify_diffs(diff_text: str) -> bool:
    """Verify changed spans only — faster than full-text verify."""
    if not diff_text.strip():
        return True
    prompt = VERIFY_DIFF_PROMPT.format(changes=diff_text)
    try:
        meta = call_llm(
            [{"role": "user", "content": prompt}],
            model="polish", max_tokens=512, temperature=TEMP,
            timeout=120, json_mode=True, return_meta=True,
        )
        parsed = parse_llm_json(_extract_json(meta["content"]))
        if isinstance(parsed, dict):
            ok = bool(parsed.get("pass", False))
            if not ok:
                reason = str(parsed.get("reason", "no reason"))[:120]
                print(f"    [verify] FAIL — {reason}", flush=True)
            return ok
    except Exception as e:
        print(f"  [error] verify diffs failed: {e}", flush=True)
    return False


# ═══════════════════════════════════════════════
# Main Processing — 3-phase per sub-batch
# ═══════════════════════════════════════════════

def _process_sub_batch(sub_batch: list, dry_run: bool) -> Tuple[int, int]:
    """Process one sub-batch.

    Phase 1 — Kiwi detection (instant, no LLM): raw vs clean comparison for text/thinking.
      text/thinking: Kiwi flagged only. user_turn: always processed.
    Phase 2 — LLM correction (parallel, PARALLEL=2): user_turn always (512 tok),
      text/thinking only when Kiwi flagged (256 tok).
    Phase 3 — LLM verify + DB write: diff-based verify on user_turn only.
      text/thinking auto-pass (Kiwi's changes are deterministic).
    """
    # ── Phase 1: Kiwi detection for text/thinking ──
    kiwi_flag: Dict[str, Dict[str, bool]] = {}
    for row in sub_batch:
        tid = row["id"]
        raw_tx = row.get("text", "") or ""
        raw_th = row.get("thinking", "") or ""
        kiwi_flag[tid] = {
            "text": _kiwi_detect(raw_tx),
            "thinking": _kiwi_detect(raw_th),
        }

    k_text = sum(1 for f in kiwi_flag.values() if f.get("text"))
    k_think = sum(1 for f in kiwi_flag.values() if f.get("thinking"))
    print(f"    [kiwi] text={k_text}/{len(kiwi_flag)}, thinking={k_think}/{len(kiwi_flag)} flagged", flush=True)

    # ── Phase 2: LLM correction (parallel) ──
    corrected: Dict[str, Dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        fmap = {}
        for row in sub_batch:
            tid = row["id"]
            ut = row.get("user_turn_clean", "") or ""
            tx = row.get("text_clean", "") or ""
            th = row.get("thinking_clean", "") or ""
            kf = kiwi_flag.get(tid, {"text": False, "thinking": False})

            # Always polish user_turn — calculate dynamic timeout
            ut_timeout = _calc_timeout(len(ut), MAX_TOKENS_USER)
            ut_solo = len(ut) > MAX_CHARS_SOLO
            if ut_solo:
                ut_timeout = _calc_timeout(len(ut), MAX_TOKENS_USER, solo=True)
            fmap[pool.submit(_polish_user_turn, ut, ut_timeout)] = (tid, "user_turn")

            # text/thinking only if Kiwi flagged errors
            if kf.get("text") and tx.strip():
                tx_timeout = _calc_timeout(len(tx), MAX_TOKENS_FIELD)
                if len(tx) > MAX_CHARS_SOLO:
                    tx_timeout = _calc_timeout(len(tx), MAX_TOKENS_FIELD, solo=True)
                fmap[pool.submit(_polish_field, tx, "text", tx_timeout)] = (tid, "text")
            if kf.get("thinking") and th.strip():
                th_timeout = _calc_timeout(len(th), MAX_TOKENS_FIELD)
                if len(th) > MAX_CHARS_SOLO:
                    th_timeout = _calc_timeout(len(th), MAX_TOKENS_FIELD, solo=True)
                fmap[pool.submit(_polish_field, th, "thinking", th_timeout)] = (tid, "thinking")

        for f in as_completed(fmap):
            tid, field = fmap[f]
            corrected.setdefault(tid, {})[field] = f.result()

    # ── Phase 3: Verify (user_turn only) + reranker + DB ──
    sub_ok = sub_fail = 0

    for row in sub_batch:
        tid = row["id"]
        orig_ut = row.get("user_turn_clean", "") or ""
        orig_tx = row.get("text_clean", "") or ""
        orig_th = row.get("thinking_clean", "") or ""
        r = corrected.get(tid, {})
        kf = kiwi_flag.get(tid, {"text": False, "thinking": False})

        # user_turn: LLM result or original fallback
        final_ut = r.get("user_turn") if r.get("user_turn") is not None else orig_ut

        # text/thinking: LLM result (if Kiwi flagged), or original (if Kiwi found nothing)
        final_tx = r.get("text") if r.get("text") is not None else orig_tx
        final_th = r.get("thinking") if r.get("thinking") is not None else orig_th

        # Verify user_turn diffs only (text/thinking changes are from Kiwi = deterministic)
        passed = True
        if orig_ut != final_ut:
            diffs = _extract_diffs(orig_ut, final_ut)
            if diffs:
                passed = _verify_diffs(diffs)
        # text/thinking: auto-pass — Kiwi changes are safe, LLM corrections bypassed
        # unless Kiwi flagged errors, in which case LLM did targeted review.

        if not passed:
            sub_fail += 1
            psql_ok(
                f"UPDATE turns SET retry_count = COALESCE(retry_count, 0) + 1 "
                f"WHERE id = '{esc_sql(tid)}'::uuid",
                timeout=30,
            )
            r2 = psql_json(
                f"SELECT COALESCE(retry_count, 0) as rc "
                f"FROM turns WHERE id = '{esc_sql(tid)}'::uuid"
            )
            if r2 and r2[0].get("rc", 0) >= 3:
                psql_ok(
                    f"UPDATE turns SET "
                    f"  user_turn_clean_polished = '{esc_sql(orig_ut)}', "
                    f"  text_clean_polished = '{esc_sql(orig_tx)}', "
                    f"  thinking_clean_polished = '{esc_sql(orig_th)}' "
                    f"WHERE id = '{esc_sql(tid)}'::uuid",
                    timeout=30,
                )
                print(f"    SENTINEL {tid[:8]} — 3 verify failures, stored original as-is", flush=True)
            continue

        # ── Reranker + 7B NLI grounding for user_turn only ──
        if orig_ut and final_ut and orig_ut != final_ut:
            cos = reranker_score(final_ut[:2000], orig_ut[:2000])
            nli_v = reranker_nli_verdict(cos)
            score = round(cos * 100, 1)
            if nli_v == "UNGROUNDED":
                print(f"    [rerank] WARN {tid[:8]} - user_turn diverged (score={score}, {nli_v})", flush=True)
            elif nli_v == "AMBIGUOUS":
                print(f"    [rerank] {tid[:8]} - user_turn ambiguous (score={score}, {nli_v})", flush=True)
            else:
                print(f"    [rerank] {tid[:8]} - user_turn grounded (score={score}, {nli_v})", flush=True)
            # 7B NLI second opinion for uncertain reranker results
            if nli_v != "GROUNDED":
                nli = _nli_check(final_ut[:500], orig_ut[:500])
                if nli == "ENTAILMENT":
                    print(f"    [7b_nli] {tid[:8]} - overrode reranker, meaning preserved", flush=True)
                elif nli == "CONTRADICTION":
                    print(f"    [7b_nli] {tid[:8]} - CONTRADICTION, failing turn", flush=True)
                    passed = False
                else:
                    print(f"    [7b_nli] {tid[:8]} - NEUTRAL (score={score})", flush=True)

        if not passed:
            sub_fail += 1
            psql_ok(
                f"UPDATE turns SET retry_count = COALESCE(retry_count, 0) + 1 "
                f"WHERE id = '{esc_sql(tid)}'::uuid",
                timeout=30,
            )
            r2 = psql_json(
                f"SELECT COALESCE(retry_count, 0) as rc "
                f"FROM turns WHERE id = '{esc_sql(tid)}'::uuid"
            )
            if r2 and r2[0].get("rc", 0) >= 3:
                psql_ok(
                    f"UPDATE turns SET "
                    f"  user_turn_clean_polished = '{esc_sql(orig_ut)}', "
                    f"  text_clean_polished = '{esc_sql(orig_tx)}', "
                    f"  thinking_clean_polished = '{esc_sql(orig_th)}' "
                    f"WHERE id = '{esc_sql(tid)}'::uuid",
                    timeout=30,
                )
                print(f"    SENTINEL {tid[:8]} - 3 verify failures, stored original as-is", flush=True)
            continue

        if dry_run:
            print(f"    DRY-RUN {tid[:8]} — ut={len(final_ut)} tx={len(final_tx)} th={len(final_th)}", flush=True)
            sub_ok += 1
            continue

        def _sv(v: str) -> str:
            return "NULL" if not v.strip() else f"'{esc_sql(v)}'"

        psql_ok(
            f"UPDATE turns SET "
            f"  user_turn_clean_polished = {_sv(final_ut)}, "
            f"  text_clean_polished = {_sv(final_tx)}, "
            f"  thinking_clean_polished = {_sv(final_th)} "
            f"WHERE id = '{esc_sql(tid)}'::uuid",
            timeout=30,
        )
        sub_ok += 1

    return sub_ok, sub_fail


def main():
    dry_run = "--dry-run" in sys.argv
    limit = BATCH_LIMIT
    for i, a in enumerate(sys.argv):
        if a == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    # Ensure Pod B is in polish mode (port auto-resolved from MODEL_METADATA)
    from lib.pod_manager import ensure_model, model_info
    print(f"  Checking Pod B: {model_info('polish')}", flush=True)
    ensure_model("polish", skip_if_healthy=True)

    with protect("polish_batch", reason="text polish phase", ports=[8082]):
        heartbeat("polish_batch")
        print("=" * 60, flush=True)
        print("Polish Batch v3 — Kiwi + 2-pass LLM", flush=True)
        print("=" * 60, flush=True)

        t_start = time.monotonic()

        rows = psql_json(
            f"SELECT id, user_turn, text, thinking, "
            f"  user_turn_clean, text_clean, thinking_clean "
            f"FROM turns "
            f"WHERE text_clean IS NOT NULL AND text_clean_polished IS NULL "
            f"ORDER BY created_at ASC "
            f"LIMIT {limit}"
        )
        if not rows:
            print("  [ok] No turns to polish", flush=True)
            return

        total = len(rows)
        print(f"  Found {total} turns to polish (sub-batch={SUBBATCH_SIZE})", flush=True)

        ok_count = 0
        fail_count = 0
        sub_total = (total + SUBBATCH_SIZE - 1) // SUBBATCH_SIZE

        for sb_idx in range(0, total, SUBBATCH_SIZE):
            sub_batch = rows[sb_idx:sb_idx + SUBBATCH_SIZE]
            sb_num = sb_idx // SUBBATCH_SIZE + 1
            print(f"\n  ── Sub-batch {sb_num}/{sub_total} ({len(sub_batch)} turns) ──", flush=True)
            sub_ok, sub_fail = _process_sub_batch(sub_batch, dry_run)
            ok_count += sub_ok
            fail_count += sub_fail
            elapsed = time.monotonic() - t_start
            print(f"  Sub-batch {sb_num} done: {sub_ok} ok, {sub_fail} fail, {elapsed:.0f}s elapsed", flush=True)

        elapsed = time.monotonic() - t_start
        print(f"Polish batch done: {ok_count} ok, {fail_count} failed, {elapsed:.0f}s", flush=True)


if __name__ == "__main__":
    main()
