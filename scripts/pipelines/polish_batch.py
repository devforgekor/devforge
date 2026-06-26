#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.sh — polish phase (before extract)
"""Polish Batch Pipeline — Kiwi(user) + Hanja substitution(thinking/text).

Phase 1 — Kiwi Detect (user_turn only): raw vs Kiwi-corrected clean.
Phase 2 — Hanja Substitution (thinking/text only): 중국어 한자→한글 (deterministic).
Phase 3 — LLM Correct (user_turn only, skip with --no-llm).
Phase 4 — Verify + DB write.

Usage:
  python3 scripts/pipelines/polish_batch.py                       # batch from NULL-checkpoint
  python3 scripts/pipelines/polish_batch.py --limit 20            # batch cap
  python3 scripts/pipelines/polish_batch.py --turn-id <uuid>      # single turn (debug)
  python3 scripts/pipelines/polish_batch.py --no-llm              # Kiwi+hanja only, no LLM calls
  python3 scripts/pipelines/polish_batch.py --dry-run             # simulate, no writes
"""

import atexit
import os
import re
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql_json, psql_ok, esc_sql
from lib.llm.json_parser import parse_llm_json
from lib.llm_client import call_llm, reranker_score, reranker_nli_verdict
from lib.text_cleaner import get_cleaner
from lib.watchdog.messenger import heartbeat, resolve_pulse
from lib.common import context_limit

import hanja

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

# ── Code block protection (same pattern as text_cleaner.py) ──
RE_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
RE_INLINE_CODE = re.compile(r"`[^`]+`")
HANJA_RANGE = re.compile(r"[一-鿿]")


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
    """True if Kiwi found spelling/grammar errors in raw text."""
    if not raw.strip():
        return False
    return _get_kiwi_cleaner().detect_kiwi_changes(raw)


# ═══════════════════════════════════════════════
# Phase 2 — Hanja Substitution (thinking/text)
# ═══════════════════════════════════════════════

def _has_hanja(text: str) -> bool:
    """True if text contains any Chinese character (CJK Unified Ideographs)."""
    return bool(HANJA_RANGE.search(text))


def _hanja_substitute(text: str) -> Tuple[str, List[Dict[str, str]]]:
    """Replace hanja (Chinese characters) with Korean hangul.

    Code blocks and inline code preserved verbatim (same pattern as TextCleaner).
    Returns (corrected_text, changes_list) where changes_list has:
      [{"from": "...original line...", "to": "...substituted line..."}]

    Deterministic — no LLM call.
    """
    if not text.strip() or not _has_hanja(text):
        return text, []

    # Code block protection
    code_blocks: List[str] = []
    inline_codes: List[str] = []

    def _save_code(m: re.Match) -> str:
        code_blocks.append(m.group())
        return f"\x00BLOCK{len(code_blocks) - 1}\x00"

    def _save_inline(m: re.Match) -> str:
        inline_codes.append(m.group())
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    t = RE_CODE_BLOCK.sub(_save_code, text)
    t = RE_INLINE_CODE.sub(_save_inline, t)

    before = t
    t = hanja.translate(t, 'substitution')

    # Track changed lines
    changes = []
    for bl, al in zip(before.split('\n'), t.split('\n')):
        if bl.strip() != al.strip():
            changes.append({"from": bl.strip(), "to": al.strip()})

    # Restore code blocks
    for i, cb in enumerate(code_blocks):
        t = t.replace(f"\x00BLOCK{i}\x00", cb)
    for i, ic in enumerate(inline_codes):
        t = t.replace(f"\x00INLINE{i}\x00", ic)

    return t, changes


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
    """Run NLI self-verify: does corrected mean the same as original?
    Returns ENTAILMENT, CONTRADICTION, or NEUTRAL.
    """
    if not corrected or not original:
        return "NEUTRAL"
    prompt = _NLI_VERIFY_PROMPT.format(
        source=context_limit(original), evidence=corrected[:500]
    )
    try:
        meta = call_llm(
            [{"role": "user", "content": prompt}],
            model="polisher",
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
      "type": "spelling|grammar|hanja|word_swap|content_added|proper_noun",
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
  - hanja: Chinese character → Korean hangul substitution (e.g., "全部" -> "전부") → ALWAYS valid, meaning preserved
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
            model="polisher", max_tokens=MAX_TOKENS_USER, temperature=TEMP,
            timeout=timeout, json_mode=True, return_meta=True,
        )
        parsed = parse_llm_json(_extract_json(meta["content"]))
        if isinstance(parsed, dict):
            return str(parsed.get("corrected", "") or "")
    except Exception as e:
        print(f"  [polish] polish user_turn failed: {e}", flush=True)
    return None


def _polish_field(text: str, field: str, timeout: int = 300) -> Optional[str]:
    """Polish text or thinking — only when Kiwi flagged errors."""
    prompt = CORRECT_PROMPT.format(field=field, text=text[:3000] or "(empty)")
    try:
        meta = call_llm(
            [{"role": "user", "content": prompt}],
            model="polisher", max_tokens=MAX_TOKENS_FIELD, temperature=TEMP,
            timeout=timeout, json_mode=True, return_meta=True,
        )
        parsed = parse_llm_json(_extract_json(meta["content"]))
        if isinstance(parsed, dict):
            return str(parsed.get("corrected", "") or "")
    except Exception as e:
        print(f"  [polish] polish {field} failed: {e}", flush=True)
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
    # Long turns can produce large diffs; polish model needs extra decode time
    timeout = max(120, min(600, len(diff_text) * 0.5))
    try:
        meta = call_llm(
            [{"role": "user", "content": prompt}],
            model="polisher", max_tokens=512, temperature=TEMP,
            timeout=int(timeout), json_mode=True, return_meta=True,
        )
        parsed = parse_llm_json(_extract_json(meta["content"]))
        if isinstance(parsed, dict):
            ok = bool(parsed.get("pass", False))
            if not ok:
                reason = str(parsed.get("reason", "no reason"))[:120]
                print(f"    [verify] FAIL — {reason}", flush=True)
            return ok
    except Exception as e:
        print(f"  [polish] verify diffs failed: {e}", flush=True)
    return False


# ═══════════════════════════════════════════════
# Main Processing — 3-phase per sub-batch
# ═══════════════════════════════════════════════

def _process_sub_batch(sub_batch: list, dry_run: bool, no_llm: bool = False) -> Tuple[int, int]:
    """Process one sub-batch.

    Phase 1 — Kiwi detect (user_turn only): raw vs Kiwi-corrected clean.
      user_turn: always flagged for LLM review.
      text/thinking: SKIP Kiwi (destructive to English/structured text).
    Phase 2 — Hanja substitution (text/thinking): deterministic 한자→한글.
    Phase 3 — LLM correction (user_turn only, skip with --no-llm).
    Phase 4 — Verify + DB write.
    """
    # ── Phase 1: Kiwi detection for user_turn only ──
    ut_has_kiwi = {row["id"]: _kiwi_detect(row.get("user_turn_clean", "") or "")
                   for row in sub_batch}
    k_ut = sum(1 for v in ut_has_kiwi.values() if v)
    print(f"    [kiwi] user_turn={k_ut}/{len(sub_batch)} flagged", flush=True)

    # ── Phase 2: Hanja substitution (text/thinking, deterministic) ──
    hanja_results: Dict[str, Dict[str, Tuple[str, List]]] = {}
    for row in sub_batch:
        tid = row["id"]
        tx = row.get("text_clean", "") or ""
        th = row.get("thinking_clean", "") or ""
        tx_sub, tx_changes = _hanja_substitute(tx)
        th_sub, th_changes = _hanja_substitute(th)
        if tx_changes or th_changes:
            hanja_results[tid] = {}
            if tx_changes:
                hanja_results[tid]["text"] = (tx_sub, tx_changes)
            if th_changes:
                hanja_results[tid]["thinking"] = (th_sub, th_changes)

    h_text = sum(1 for v in hanja_results.values() if "text" in v)
    h_think = sum(1 for v in hanja_results.values() if "thinking" in v)
    if h_text or h_think:
        print(f"    [hanja] text={h_text}/{len(sub_batch)}, thinking={h_think}/{len(sub_batch)} substituted",
              flush=True)

    # ── Phase 3: LLM correction (user_turn only, parallel) — skip if --no-llm ──
    corrected: Dict[str, Dict[str, Any]] = {}

    if no_llm:
        print(f"    [no-llm] Kiwi+hanja only — no LLM calls", flush=True)
    else:
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            fmap = {}
            for row in sub_batch:
                tid = row["id"]
                ut = row.get("user_turn_clean", "") or ""
                ut_timeout = _calc_timeout(len(ut), MAX_TOKENS_USER)
                if len(ut) > MAX_CHARS_SOLO:
                    ut_timeout = _calc_timeout(len(ut), MAX_TOKENS_USER, solo=True)
                fmap[pool.submit(_polish_user_turn, ut, ut_timeout)] = (tid, "user_turn")
            for f in as_completed(fmap):
                tid, field = fmap[f]
                corrected.setdefault(tid, {})[field] = f.result()

    # ── Phase 4: Verify (user_turn only) + reranker + DB ──
    sub_ok = sub_fail = 0

    for row in sub_batch:
        tid = row["id"]
        orig_ut = row.get("user_turn_clean", "") or ""
        orig_tx = row.get("text_clean", "") or ""
        orig_th = row.get("thinking_clean", "") or ""
        r = corrected.get(tid, {})

        # user_turn: LLM result or original fallback
        final_ut = r.get("user_turn") if r.get("user_turn") is not None else orig_ut

        # text/thinking: hanja-substituted or original
        h_tx = hanja_results.get(tid, {}).get("text", (orig_tx, []))[0] if tid in hanja_results else orig_tx
        h_th = hanja_results.get(tid, {}).get("thinking", (orig_th, []))[0] if tid in hanja_results else orig_th

        if r.get("text") is not None:
            final_tx = r.get("text")
        else:
            final_tx = h_tx
        if r.get("thinking") is not None:
            final_th = r.get("thinking")
        else:
            final_th = h_th

        # Verify ALL diffs in one call (user_turn + text hanja + thinking hanja)
        passed = True
        if not no_llm:
            all_diffs = []
            if orig_ut != final_ut:
                d = _extract_diffs(orig_ut, final_ut)
                if d:
                    all_diffs.append(f"=== User Turn ===\n{d}")
            for field, h_val, o_val in [("Text", h_tx, orig_tx), ("Thinking", h_th, orig_th)]:
                if h_val != o_val:
                    d = _extract_diffs(o_val, h_val)
                    if d:
                        all_diffs.append(f"=== {field} (Hanja) ===\n{d}")
            if all_diffs:
                combined = "\n\n".join(all_diffs)
                passed = _verify_diffs(combined)
                if not passed:
                    print(f"    [verify] {tid[:8]} — diffs REJECTED, reverting all fields", flush=True)

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

        # ── Reranker + NLI grounding for user_turn only ──
        if orig_ut and final_ut and orig_ut != final_ut:
            cos = reranker_score(context_limit(final_ut), context_limit(orig_ut))
            nli_v = reranker_nli_verdict(cos)
            score = round(cos * 100, 1)
            if nli_v == "UNGROUNDED":
                print(f"    [rerank] WARN {tid[:8]} - user_turn diverged (score={score}, {nli_v})", flush=True)
            elif nli_v == "AMBIGUOUS":
                print(f"    [rerank] {tid[:8]} - user_turn ambiguous (score={score}, {nli_v})", flush=True)
            else:
                print(f"    [rerank] {tid[:8]} - user_turn grounded (score={score}, {nli_v})", flush=True)
            # NLI second opinion for uncertain reranker results
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
        psql_ok(
            f"UPDATE turns SET pipeline_state = 'polished' "
            f"WHERE id = '{esc_sql(tid)}'::uuid",
            timeout=30,
        )
        sub_ok += 1

    return sub_ok, sub_fail


def main():
    dry_run = "--dry-run" in sys.argv
    no_llm = "--no-llm" in sys.argv
    limit = BATCH_LIMIT
    turn_ids = []
    for i, a in enumerate(sys.argv):
        if a == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])
        if a == "--turn-id" and i + 1 < len(sys.argv):
            turn_ids.append(sys.argv[i + 1])

    # Polisher runs on Pod A router (:8080) — no Pod B model switch needed
    if not no_llm:
        from lib.pod_manager import ensure_model, model_info
        print(f"  Polisher available via Pod A router (:8080)", flush=True)

    # Register heartbeat pulse + SIGTERM cleanup
    if not no_llm:
        heartbeat("polish_batch", detail="text polish phase")
        _cleanup_polish = lambda: resolve_pulse("heartbeat_polish_batch")
        signal.signal(signal.SIGTERM, lambda s, f: (_cleanup_polish(), os._exit(1)))
        atexit.register(_cleanup_polish)

    print("=" * 60, flush=True)
    if no_llm:
        print("Polish Batch v4 — Kiwi-only (no LLM)", flush=True)
    else:
        print("Polish Batch v3 — Kiwi + 2-pass LLM", flush=True)
    print("=" * 60, flush=True)

    # Advance already-polished turns: cleaned → polished
    if not turn_ids:
        psql_ok(
            "UPDATE turns SET pipeline_state = 'polished' "
            "WHERE pipeline_state = 'cleaned' "
            "AND text_clean_polished IS NOT NULL AND text_clean_polished != ''"
        )

    t_start = time.monotonic()

    if turn_ids:
        ids_list = ", ".join(f"'{esc_sql(t)}'::uuid" for t in set(turn_ids))
        rows = psql_json(
            f"SELECT id, user_turn, text, thinking, "
            f"  user_turn_clean, text_clean, thinking_clean "
            f"FROM turns "
            f"WHERE id IN ({ids_list}) AND text_clean IS NOT NULL "
            f"ORDER BY created_at ASC"
        )
    else:
        rows = psql_json(
            f"SELECT id, user_turn, text, thinking, "
            f"  user_turn_clean, text_clean, thinking_clean "
            f"FROM turns "
            f"WHERE text_clean IS NOT NULL AND text_clean_polished IS NULL AND pipeline_state = 'cleaned' "
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
        sub_ok, sub_fail = _process_sub_batch(sub_batch, dry_run, no_llm=no_llm)
        ok_count += sub_ok
        fail_count += sub_fail
        elapsed = time.monotonic() - t_start
        print(f"  Sub-batch {sb_num} done: {sub_ok} ok, {sub_fail} fail, {elapsed:.0f}s elapsed", flush=True)

    elapsed = time.monotonic() - t_start
    print(f"Polish batch done: {ok_count} ok, {fail_count} failed, {elapsed:.0f}s", flush=True)
    if not no_llm:
        from lib.llm_client import recall_tiny
        recall_tiny()
    if fail_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
