#!/usr/bin/env python3
# Status: production
# Path: day_cycle.sh — Text Preprocessing step (formerly text_clean.py + polish_batch.py)
"""Unified Text Preprocessing — Clean + Polish in one stage.

Language-aware pipeline with branching:
  - Korean (ko): NFKC → emoji → Kiwi typo correction → hanja substitution → LLM verify
  - English (en): NFKC → emoji → whitespace normalization (no Kiwi)
  - Other: basic NFKC + whitespace only

Pipeline:
  Phase 1 — Clean: language detection → language-branched cleaning
  Phase 2 — Hanja substitution (Korean text/thinking only, deterministic)
  Phase 3 — LLM verify Kiwi diffs (skip with --no-llm)
  Phase 4 — Store + tiktoken token count → est_chars update

pipeline_state: batching → cleaned (polished stage merged into this one).

Usage:
  python3 scripts/pipelines/text_clean.py                       # batch from batching state
  python3 scripts/pipelines/text_clean.py --limit 20            # batch cap
  python3 scripts/pipelines/text_clean.py --turn-id <uuid>      # single turn (debug)
  python3 scripts/pipelines/text_clean.py --no-llm              # skip LLM verify
  python3 scripts/pipelines/text_clean.py --dry-run             # simulate, no writes
"""

import atexit
import os
import signal
import sys
import time
from difflib import SequenceMatcher
from typing import Dict, Optional, Tuple

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.common import context_limit
from lib.db import esc_sql, psql_json, psql_ok
from lib.llm.json_parser import parse_llm_json
from lib.llm_client import call_llm
from lib.text_cleaner import (
    detect_language,
    estimate_tokens,
    get_cleaner,
    hanja_substitute,
)
from lib.watchdog.messenger import heartbeat, resolve_pulse

BATCH_LIMIT = 50
SUBBATCH_SIZE = 10

# ── Phase 3: LLM Verify Prompts ──

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
  - hanja: Chinese character → Korean hangul substitution → ALWAYS valid, meaning preserved
  - word_swap: word replaced with different word → INVALID, meaning NOT preserved
  - content_added: new content inserted → INVALID, meaning NOT preserved
  - proper_noun: proper noun or technical term modified → INVALID, meaning NOT preserved

Step 2 — Set meaning_preserved accordingly.
Step 3 — If ANY change is INVALID, pass MUST be false."""


# ═══════════════════════════════════════════════
# Phase 3 Helper — LLM Verify Diffs
# ═══════════════════════════════════════════════


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
    return "\n\n".join(f"=== Change {i + 1} ===\n{c}" for i, c in enumerate(changes))


def _extract_json(text: str) -> str:
    """Strip markdown code block markers from LLM response."""
    s = text.strip()
    for prefix in ("```json", "```", "'''json", "'''"):
        if s.startswith(prefix):
            s = s[len(prefix) :].strip()
            break
    for suffix in ("```", "'''"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
            break
    return s


def _verify_diffs(diff_text: str) -> Optional[bool]:
    """Verify changed spans using polisher. Single call, no retry."""
    if not diff_text.strip():
        return True
    prompt = VERIFY_DIFF_PROMPT.format(changes=diff_text)
    timeout = max(180, min(600, len(diff_text) * 0.3))
    try:
        meta = call_llm(
            [{"role": "user", "content": prompt}],
            model="cleaner",
            max_tokens=128,
            temperature=0.0,
            timeout=int(timeout),
            json_mode=True,
            return_meta=True,
        )
        parsed = parse_llm_json(_extract_json(meta["content"]))
        if isinstance(parsed, dict):
            ok = bool(parsed.get("pass", False))
            if not ok:
                reason = str(parsed.get("reason", "no reason"))[:120]
                print(f"    [verify] FAIL — {reason}", flush=True)
            return ok
    except Exception as e:
        print(f"  [text_clean] verify diffs skipped (LLM failed): {e}", flush=True)
    return None


# ═══════════════════════════════════════════════
# Main Processing — per sub-batch
# ═══════════════════════════════════════════════


def _process_sub_batch(sub_batch: list, dry_run: bool, no_llm: bool = False) -> Tuple[int, Dict]:
    """Process one sub-batch of turns.

    Phase 1 — Clean: language detection → language-branched cleaning.
    Phase 2 — Hanja substitution (Korean text/thinking only).
    Phase 3 — LLM verify Kiwi diffs (Korean only, skip with --no-llm).
    Phase 4 — Store + token count → est_chars update.

    Returns (ok_count, stats) where stats tracks language breakdown.
    """
    cl = get_cleaner()
    stats = {"ko": 0, "en": 0, "other": 0}
    reverted: set = set()

    # ── Phase 1: Clean + Language Detection ──
    for row in sub_batch:
        tid = row["id"]
        raw_ut = row.get("user_turn", "") or ""
        raw_tx = row.get("text", "") or ""
        raw_th = row.get("thinking", "") or ""

        # Detect language from user_turn (highest signal)
        combined = (raw_ut + " " + (raw_tx[:500] if raw_tx else "")).strip()
        lang, _ = detect_language(combined) if combined.strip() else ("unknown", 0.0)
        if lang not in ("ko", "en"):
            lang = "unknown"
        row["_lang"] = lang
        stats[lang if lang in stats else "other"] += 1

        # Clean (language-aware)
        row["_clean_ut"] = cl.clean(context_limit(raw_ut, 2000, ratio_front=1.0), lang=lang)
        row["_clean_tx"] = cl.clean(context_limit(raw_tx, 8000, ratio_front=1.0), lang=lang)
        row["_clean_th"] = cl.clean(context_limit(raw_th, 4000, ratio_front=1.0), lang=lang)

    ko_count = stats["ko"]
    en_count = stats["en"]
    print(
        f"    [lang] ko={ko_count}, en={en_count}, other={stats['other']}, total={len(sub_batch)}",
        flush=True,
    )

    # ── Phase 2: Hanja substitution (Korean text/thinking only) ──
    hanja_text = 0
    hanja_think = 0
    for row in sub_batch:
        if row.get("_lang") != "ko":
            row["_hanja_tx"] = None
            row["_hanja_th"] = None
            continue
        tid = row["id"]
        clean_tx = row["_clean_tx"]
        clean_th = row["_clean_th"]
        tx_sub, tx_changes = hanja_substitute(clean_tx)
        th_sub, th_changes = hanja_substitute(clean_th)
        row["_hanja_tx"] = (tx_sub, tx_changes) if tx_changes else None
        row["_hanja_th"] = (th_sub, th_changes) if th_changes else None
        if tx_changes:
            hanja_text += 1
        if th_changes:
            hanja_think += 1

    if hanja_text or hanja_think:
        print(
            f"    [hanja] text={hanja_text}/{ko_count}, thinking={hanja_think}/{ko_count} substituted",
            flush=True,
        )

    # ── Phase 3: LLM verify Kiwi diffs (Korean only) ──
    if not no_llm:
        for row in sub_batch:
            if row.get("_lang") != "ko":
                continue
            tid = row["id"]
            raw_ut = row.get("user_turn", "") or ""
            clean_ut = row["_clean_ut"]
            if raw_ut and clean_ut and raw_ut != clean_ut:
                diff_text = _extract_diffs(raw_ut, clean_ut)
                # diff_text already formatted by _extract_diffs; wrap in prompt format
                ok = _verify_diffs(diff_text)
                if ok is False:
                    print(
                        f"    [verify] {tid[:8]} — Kiwi diff REJECTED, using raw text", flush=True
                    )
                    reverted.add(tid)

    # ── Phase 4: Store ──
    sub_ok = 0
    for row in sub_batch:
        tid = row["id"]
        lang = row.get("_lang", "unknown")
        raw_ut = row.get("user_turn", "") or ""
        raw_tx = row.get("text", "") or ""
        raw_th = row.get("thinking", "") or ""
        clean_ut = row["_clean_ut"]
        clean_tx = row["_clean_tx"]
        clean_th = row["_clean_th"]

        # For Korean: apply hanja substitutions (Phase 2 results)
        if lang == "ko":
            if row.get("_hanja_tx"):
                clean_tx = row["_hanja_tx"][0]
            if row.get("_hanja_th"):
                clean_th = row["_hanja_th"][0]

        # For English: skip Kiwi — clean_ut is already the raw NFKC/emoji cleaned version
        # If verify rejected, use raw user_turn (reverted only applies to Korean)
        # For English: no Kiwi was applied, so no reversion needed
        final_ut = raw_ut if tid in reverted else clean_ut
        final_tx = clean_tx
        final_th = clean_th

        # Token estimation via tiktoken
        tok_count = estimate_tokens(final_ut)

        if dry_run:
            print(
                f"    DRY-RUN {tid[:8]} [{lang}] ut={len(final_ut)} tx={len(final_tx)} tok={tok_count}",
                flush=True,
            )
            sub_ok += 1
            continue

        def _sv(v: str) -> str:
            return "NULL" if not v.strip() else f"'{esc_sql(v)}'"

        psql_ok(
            f"UPDATE turns SET "
            f"  user_turn_clean = {_sv(final_ut)}, "
            f"  text_clean = {_sv(final_tx)}, "
            f"  thinking_clean = {_sv(final_th)}, "
            f"  est_chars = {tok_count} "
            f"WHERE id = '{esc_sql(tid)}'::uuid",
            timeout=30,
        )
        psql_ok(
            f"UPDATE turns SET pipeline_state = 'cleaned' WHERE id = '{esc_sql(tid)}'::uuid",
            timeout=30,
        )
        sub_ok += 1

    return sub_ok, stats


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
        heartbeat("text_clean", detail="unified text preprocessing phase")

    def _cleanup():
        if not no_llm:
            resolve_pulse("heartbeat_text_clean")

    signal.signal(signal.SIGTERM, lambda s, f: (_cleanup(), os._exit(1)))
    atexit.register(_cleanup)

    print("=" * 60, flush=True)
    mode = "Language-aware (Korean + English)"
    if no_llm:
        mode += " [no-llm]"
    print(f"Text Clean — {mode}", flush=True)
    print("=" * 60, flush=True)

    # Advance already-clean turns: batching → cleaned
    if not turn_ids:
        psql_ok(
            "UPDATE turns SET pipeline_state = 'cleaned' "
            "WHERE pipeline_state = 'batching' AND text_clean IS NOT NULL AND text_clean != ''"
        )

    t_start = time.monotonic()

    if turn_ids:
        ids_list = ", ".join(f"'{esc_sql(t)}'::uuid" for t in set(turn_ids))
        rows = psql_json(
            f"SELECT id, user_turn, text, thinking "
            f"FROM turns "
            f"WHERE id IN ({ids_list}) AND (text_clean IS NULL OR text_clean = '') "
            f"ORDER BY created_at ASC"
        )
    else:
        rows = psql_json(
            f"SELECT id, user_turn, text, thinking FROM turns "
            f"WHERE (text_clean IS NULL OR text_clean = '') AND pipeline_state = 'batching' "
            f"ORDER BY created_at ASC "
            f"LIMIT {limit}"
        )
    if not rows:
        print("  [text_clean] 0 turns need preprocessing", flush=True)
        return

    total = len(rows)
    print(f"  Found {total} turns to process (sub-batch={SUBBATCH_SIZE})", flush=True)

    ok_count = 0
    total_stats = {"ko": 0, "en": 0, "other": 0}
    sub_total = (total + SUBBATCH_SIZE - 1) // SUBBATCH_SIZE

    for sb_idx in range(0, total, SUBBATCH_SIZE):
        sub_batch = rows[sb_idx : sb_idx + SUBBATCH_SIZE]
        sb_num = sb_idx // SUBBATCH_SIZE + 1
        print(f"\n  ── Sub-batch {sb_num}/{sub_total} ({len(sub_batch)} turns) ──", flush=True)
        sub_ok, stats = _process_sub_batch(sub_batch, dry_run, no_llm=no_llm)
        ok_count += sub_ok
        for k in total_stats:
            total_stats[k] += stats.get(k, 0)
        elapsed = time.monotonic() - t_start
        print(f"  Sub-batch {sb_num} done: {sub_ok} ok, {elapsed:.0f}s elapsed", flush=True)

    elapsed = time.monotonic() - t_start
    ks = total_stats
    print(f"\nText clean done: {ok_count}/{total} ok, {elapsed:.0f}s", flush=True)
    print(f"  Languages: ko={ks['ko']}, en={ks['en']}, other={ks['other']}", flush=True)

    if not no_llm:
        from lib.llm_client import recall_tiny

        recall_tiny()

    if ok_count != total:
        sys.exit(1)


if __name__ == "__main__":
    main()
