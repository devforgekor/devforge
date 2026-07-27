#!/usr/bin/env python3
# Status: deprecated
# Path: migrated to text_clean.py (unified preprocessing) 2026-06-27
"""DEPRECATED — Merged into text_clean.py.

text_clean.py now handles everything: language detection → language-branched cleaning
→ hanja substitution → LLM verify → tiktoken token stats.

This file kept for reference but no longer called by day_cycle.sh.
Remove after 2026-07-27 if no issues.
"""

user_turn_clean already contains Kiwi typo correction (applied during text_clean.py).
This pipeline detects Kiwi-introduced changes and
optionally verifies diffs with the polisher LLM (--no-llm skips verify).

Phase 1 — Kiwi diff detection (user_turn vs user_turn_clean).
Phase 2 — Hanja substitution (text/thinking): deterministic 한자→한글.
Phase 3 — LLM verify diffs (skip with --no-llm).
DB write — Kiwi output as user_turn_clean_polished.

Usage:
  python3 scripts/pipelines/polish_batch.py                       # batch from NULL-checkpoint
  python3 scripts/pipelines/polish_batch.py --limit 20            # batch cap
  python3 scripts/pipelines/polish_batch.py --turn-id <uuid>      # single turn (debug)
  python3 scripts/pipelines/polish_batch.py --no-llm              # Kiwi+hanja only, no verify LLM
  python3 scripts/pipelines/polish_batch.py --dry-run             # simulate, no writes
"""

import atexit
import os
import re
import signal
import sys
import time
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql_json, psql_ok, esc_sql
from lib.llm.json_parser import parse_llm_json
from lib.llm_client import call_llm
from lib.watchdog.messenger import heartbeat, resolve_pulse

import hanja

BATCH_LIMIT = 50
SUBBATCH_SIZE = 10

# ── Code block protection (same pattern as text_cleaner.py) ──
RE_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
RE_INLINE_CODE = re.compile(r"`[^`]+`")
HANJA_RANGE = re.compile(r"[一-鿿]")


# ═══════════════════════════════════════════════
# Phase 2 — Hanja Substitution (thinking/text)
# ═══════════════════════════════════════════════

def _has_hanja(text: str) -> bool:
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

    changes = []
    for bl, al in zip(before.split('\n'), t.split('\n')):
        if bl.strip() != al.strip():
            changes.append({"from": bl.strip(), "to": al.strip()})

    for i, cb in enumerate(code_blocks):
        t = t.replace(f"\x00BLOCK{i}\x00", cb)
    for i, ic in enumerate(inline_codes):
        t = t.replace(f"\x00INLINE{i}\x00", ic)

    return t, changes


# ═══════════════════════════════════════════════
# Phase 3 — Reranker Prompts
# ═══════════════════════════════════════════════

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


def _extract_diffs(original: str, corrected: str, max_pairs: int = 5) -> str:
    """Extract changed spans and format for verify prompt."""
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


def _verify_diffs(diff_text: str) -> Optional[bool]:
    """Verify changed spans — uses polisher for accuracy. Single call, no retry."""
    if not diff_text.strip():
        return True
    prompt = VERIFY_DIFF_PROMPT.format(changes=diff_text)
    timeout = max(180, min(600, len(diff_text) * 0.3))
    try:
        meta = call_llm(
            [{"role": "user", "content": prompt}],
            model="cleaner", max_tokens=128, temperature=0.0,
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
        print(f"  [polish] verify diffs skipped (LLM failed): {e}", flush=True)
    return None


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


# ═══════════════════════════════════════════════
# Main Processing — per sub-batch
# ═══════════════════════════════════════════════

def _process_sub_batch(sub_batch: list, dry_run: bool, no_llm: bool = False) -> Tuple[int, int]:
    """Process one sub-batch.

    Phase 1 — Kiwi diff detection: user_turn vs user_turn_clean (already Kiwi-corrected).
    Phase 2 — Hanja substitution (text/thinking): deterministic 한자→한글.
    Phase 3 — LLM verify diffs with polisher (skip with --no-llm).
    """
    # ── Phase 1: Kiwi diff detection ──
    # user_turn_clean includes Kiwi typo correction (applied in text_clean.py).
    # Detect Kiwi-introduced changes by comparing raw vs clean.
    has_kiwi_diff = 0
    for row in sub_batch:
        raw = row.get("user_turn", "") or ""
        clean = row.get("user_turn_clean", "") or ""
        if raw and clean and raw != clean:
            has_kiwi_diff += 1
    print(f"    [kiwi] user_turn diff={has_kiwi_diff}/{len(sub_batch)}", flush=True)

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

    # ── Phase 3: LLM verify Kiwi diffs with polisher ──
    reverted: set = set()
    if not no_llm:
        for row in sub_batch:
            tid = row["id"]
            raw_ut = row.get("user_turn", "") or ""
            kiwied_ut = row.get("user_turn_clean", "") or ""
            if raw_ut and kiwied_ut and raw_ut != kiwied_ut:
                diff_text = _extract_diffs(raw_ut, kiwied_ut)
                ok = _verify_diffs(diff_text)
                if ok is False:
                    print(f"    [verify] {tid[:8]} — Kiwi diff REJECTED, reverting", flush=True)
                    reverted.add(tid)

    # ── DB write ──
    sub_ok = sub_fail = 0

    for row in sub_batch:
        tid = row["id"]
        raw_ut = row.get("user_turn", "") or ""
        kiwied_ut = row.get("user_turn_clean", "") or ""
        orig_tx = row.get("text_clean", "") or ""
        orig_th = row.get("thinking_clean", "") or ""

        # user_turn: Kiwi output (user_turn_clean), or raw if verify rejected
        final_ut = raw_ut if tid in reverted else kiwied_ut

        # text/thinking: hanja-substituted or original
        h_tx = hanja_results.get(tid, {}).get("text", (orig_tx, []))[0] if tid in hanja_results else orig_tx
        h_th = hanja_results.get(tid, {}).get("thinking", (orig_th, []))[0] if tid in hanja_results else orig_th
        final_tx = h_tx
        final_th = h_th

        if tid in reverted:
            sub_fail += 1

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

    if not no_llm:
        print(f"  Polisher available via inference router (:8083)", flush=True)

    # Register heartbeat pulse + SIGTERM cleanup
    if not no_llm:
        heartbeat("polish_batch", detail="text polish phase")
        _cleanup_polish = lambda: resolve_pulse("heartbeat_polish_batch")
        signal.signal(signal.SIGTERM, lambda s, f: (_cleanup_polish(), os._exit(1)))
        atexit.register(_cleanup_polish)

    print("=" * 60, flush=True)
    if no_llm:
        print("Polish Batch — Kiwi only (no verify LLM)", flush=True)
    else:
        print("Polish Batch — Kiwi + verify(polisher)", flush=True)
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
