#!/usr/bin/env python3
# Status: experimental
# Path: none — Full pipeline NEUTRAL test (10 real samples, 3 verify prompts)
"""Full Pipeline NEUTRAL Test — 10 real samples with 3 verify prompt variants.

Phase 1: Text Clean → Polish → FTS5 → Extract → MCP Enrich (10 oldest turns)
  - Saves snapshot at each stage, especially after MCP (pre-verify)
Phase 2: Verify with 3 prompt variants on identical snapshot data
  - OLD:   day_verify.py default prompt (E-C-N order, no few-shot)
  - V2:    N-C-E order + few-shot NEUTRAL examples
  - V3_COT: CoT + strict ENTAILMENT def + all 3 NEUTRAL subtypes as examples
  - Compares grounding distribution + accuracy per variant

Usage:
  python3 scripts/tests/pipeline_verify_compare.py [--limit 10] [--skip-phase1]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "eval")
PIPELINES_DIR = os.path.join(SCRIPTS_DIR, "pipelines")
os.makedirs(EVAL_DIR, exist_ok=True)
sys.path.insert(0, SCRIPTS_DIR)
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from lib.llm_client import call_llm
from lib.test_common import test_setup, test_heartbeat, test_complete
from lib.db import psql_json, psql_ok

# ── Snapshot helpers (from pipeline_preverify_test.py) ───────────

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def _fmt_ids(turn_ids: list) -> str:
    return ",".join(f"'{tid}'" for tid in turn_ids)

def save_snapshot(stage: str, turn_ids: list, tag: str = "") -> str:
    ts = _ts()
    safe_tag = f"_{tag}" if tag else ""
    filename = f"pipeline_verify_compare_{stage}{safe_tag}_{ts}.json"
    path = os.path.join(EVAL_DIR, filename)
    id_list = _fmt_ids(turn_ids)

    rows = psql_json(
        f"SELECT id, conversation_id, seq, user_turn, user_turn_clean, "
        f"user_turn_clean_polished, thinking, thinking_clean, "
        f"thinking_clean_polished, text, text_clean, text_clean_polished, "
        f"meta, created_at "
        f"FROM turns WHERE id IN ({id_list}) ORDER BY created_at"
    )
    facts = psql_json(
        f"SELECT turn_id, fact_index, fact_type, evidence, verdict, reason, "
        f"fact_action, fact_confidence, nli_verdict, "
        f"extract_model, verify_model, source, phase "
        f"FROM review_facts WHERE turn_id IN ({id_list}) "
        f"ORDER BY turn_id, fact_index"
    )

    snapshot = {
        "timestamp": ts, "stage": stage, "tag": tag or None,
        "n_turns": len(rows), "n_facts": len(facts or []),
        "turns": rows, "review_facts": facts or [],
    }
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False, default=str)
    print(f"  [snapshot] {stage}: {path} ({len(rows)} turns, {len(facts or [])} facts)", flush=True)
    return path


def run_subprocess(cmd: list, timeout: int = 3600, label: str = "") -> bool:
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        ok = r.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"  [{label}] TIMEOUT after {time.monotonic()-t0:.0f}s", flush=True)
        return False
    except Exception as e:
        print(f"  [{label}] ERROR: {e}", flush=True)
        return False
    elapsed = time.monotonic() - t0
    print(f"  [{label}] exit={r.returncode}, {elapsed:.0f}s", flush=True)
    for line in r.stdout.strip().splitlines()[-6:]:
        print(f"    {line}", flush=True)
    return ok


# ── Phase 1: Pre-verify pipeline stages ──────────────────────────

def select_turns(limit: int = 10) -> list:
    """Pick N oldest turns needing full processing (no polished text)."""
    rows = psql_json(
        f"SELECT id, user_turn, text, thinking, "
        f"LENGTH(COALESCE(text,'')) AS text_len, created_at "
        f"FROM turns "
        f"WHERE (text_clean_polished IS NULL OR text_clean IS NULL OR text_clean = '') "
        f"  AND user_turn NOT LIKE 'This session%%' "
        f"  AND user_turn NOT LIKE 'Run /opt%%' "
        f"  AND user_turn NOT LIKE '<command%%' "
        f"  AND user_turn NOT LIKE 'Check %%' "
        f"ORDER BY created_at ASC LIMIT {limit}"
    )
    if not rows:
        print("  [error] No turns need processing!", flush=True)
        return []
    sizes = [(r.get("text_len", 0) or 0) for r in rows]
    print(f"  [select] {len(rows)} turns (min={min(sizes)}, max={max(sizes)}, avg={sum(sizes)//len(sizes)} chars)", flush=True)
    for r in rows[:10]:
        preview = (r.get("user_turn", "") or "")[:50]
        print(f"    {r['id'][:8]} ({r.get('text_len',0)}c) \"{preview}\"", flush=True)
    return rows


def stage_text_clean(turn_ids: list) -> bool:
    """Create text_clean for turns that need it."""
    print(f"\n{'─'*50}\n  Stage: Text Preprocess\n{'─'*50}", flush=True)
    test_heartbeat("text_clean")
    from lib.text_cleaner import get_cleaner
    cl = get_cleaner()
    id_list = _fmt_ids(turn_ids)
    needs = psql_json(
        f"SELECT id, user_turn, text, thinking FROM turns WHERE id IN ({id_list}) "
        f"AND (text_clean IS NULL OR text_clean = '')"
    )
    if not needs:
        print(f"  [text_clean] All {len(turn_ids)} turns done", flush=True)
        return True
    print(f"  [text_clean] Cleaning {len(needs)}/{len(turn_ids)} turns...", flush=True)
    ok = 0
    for t in needs:
        from lib.db import esc_sql
        sql = (
            f"UPDATE turns SET "
            f"user_turn_clean = '{esc_sql(cl.clean((t.get('user_turn') or '')[:2000]))}', "
            f"text_clean = '{esc_sql(cl.clean((t.get('text') or '')[:8000]))}', "
            f"thinking_clean = '{esc_sql(cl.clean((t.get('thinking') or '')[:4000]))}' "
            f"WHERE id = '{t['id']}'"
        )
        if psql_ok(sql):
            ok += 1
    print(f"  [text_clean] {ok}/{len(needs)} done", flush=True)
    return ok == len(needs)


def stage_polish(limit: int) -> bool:
    print(f"\n{'─'*50}\n  Stage: Polish + Self-Verify (7B Q8 :8082)\n{'─'*50}", flush=True)
    test_heartbeat("polish")
    return run_subprocess(
        [sys.executable, "-u", os.path.join(PIPELINES_DIR, "polish_batch.py"), "--limit", str(limit)],
        timeout=7200, label="polish"
    )


def stage_fts5() -> bool:
    print(f"\n{'─'*50}\n  Stage: FTS5 Refresh\n{'─'*50}", flush=True)
    test_heartbeat("fts5")
    return run_subprocess(
        [sys.executable, "-u", os.path.join(PIPELINES_DIR, "fts5_refresh.py")],
        timeout=120, label="fts5"
    )


def stage_extract(limit: int) -> bool:
    print(f"\n{'─'*50}\n  Stage: Extract + Reranker\n{'─'*50}", flush=True)
    test_heartbeat("extract")
    return run_subprocess(
        [sys.executable, "-u", os.path.join(PIPELINES_DIR, "extract.py"), "--limit", str(limit)],
        timeout=7200, label="extract"
    )


def stage_enrich(limit: int) -> bool:
    print(f"\n{'─'*50}\n  Stage: Enrich (7B Q8 :8082)\n{'─'*50}", flush=True)
    test_heartbeat("enrich")
    return run_subprocess(
        [sys.executable, "-u", os.path.join(PIPELINES_DIR, "enrich.py"), "--limit", str(limit)],
        timeout=3600, label="enrich"
    )


# ── Phase 2: Snapshot-loaded verify with interchangeable prompts ──

# OLD prompt — matches day_verify.py VERIFIER_SYSTEM_PROMPT exactly
OLD_SYSTEM = """You are a code review verifier. Examine all findings and decide for each:
- approved: correct, can proceed
- rejected: incorrect or not actionable
- needs_review: requires deeper analysis

Classify each finding into a category:
- bug: actual logic error or incorrect behavior
- security: vulnerability or unsafe pattern
- performance: efficiency or resource issue
- quality: maintainability, style, or documentation
- data_loss: missing or dropped information
- hallucination: extracted content NOT supported by the original conversation turn (made up, exaggerated, or contradictory)

IMPORTANT — Faithfulness check: Each finding has an "evidence" field extracted from the
conversation. Verify that the evidence actually appears in or is directly supported by the
turn. If the evidence is fabricated, exaggerated, or contradicts the turn context, mark
it as "hallucination" category with result "fail".

IMPORTANT — Grounding (NLI): For each finding, determine the logical relationship between
the evidence and the conversation turn context (shown as CTX entries). Classify as:
- ENTAILMENT: evidence is directly supported by the turn (same facts, correct numbers, logically follows)
- CONTRADICTION: evidence contradicts the turn (opposite claim, wrong numbers, negation mismatch)
- NEUTRAL: evidence is related but not directly entailed (reasonable inference, plausible but unstated)

Output STRICT JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "confidence": 0-100,
  "summary": "1-sentence overall assessment",
  "reasoning": "2-3 sentence analysis",
  "verification_items": [
    {"check": "...", "result": "pass|fail|partial", "detail": "...", "category": "bug|security|performance|quality|data_loss|hallucination", "grounding": "ENTAILMENT|CONTRADICTION|NEUTRAL"}
  ]
}"""

# V2 prompt — N-C-E order + few-shot NEUTRAL examples
V2_SYSTEM = """You are a code review verifier. Examine all findings and decide for each:
- approved: correct, can proceed
- rejected: incorrect or not actionable
- needs_review: requires deeper analysis

Classify each finding into a category:
- bug: actual logic error or incorrect behavior
- security: vulnerability or unsafe pattern
- performance: efficiency or resource issue
- quality: maintainability, style, or documentation
- data_loss: missing or dropped information
- hallucination: extracted content NOT supported by the original conversation turn

IMPORTANT — Faithfulness check: Verify evidence actually appears in or is directly supported by the turn.

IMPORTANT — Grounding (NLI):
- NEUTRAL: evidence is related but NOT directly entailed (reasonable inference, plausible but unstated, or different aspect of same topic)
- CONTRADICTION: evidence contradicts the turn (opposite claim, wrong numbers, negation mismatch)
- ENTAILMENT: evidence is directly supported by the turn (same facts, correct numbers, logically follows)

Examples of NEUTRAL:
  Source: "PostgreSQL 16을 데이터베이스로 사용한다"
  Evidence: "PostgreSQL 16은 빠르다"
  → NEUTRAL (속도는 source에 언급되지 않음)

  Source: "비타민C는 면역력에 좋다"
  Evidence: "비타민C는 피부 미백에 도움된다"
  → NEUTRAL (같은 주제지만 다른 측면)

Output STRICT JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "confidence": 0-100,
  "summary": "1-sentence overall assessment",
  "reasoning": "2-3 sentence analysis",
  "verification_items": [
    {"check": "...", "result": "pass|fail|partial", "detail": "...", "category": "bug|security|performance|quality|data_loss|hallucination", "grounding": "NEUTRAL|CONTRADICTION|ENTAILMENT"}
  ]
}"""

# V3_COT prompt — CoT reasoning + strict ENTAILMENT + all 3 NEUTRAL subtypes
V3_SYSTEM = """You are a code review verifier. Determine the relationship between each finding's evidence and the conversation context.

Definitions:
- ENTAILMENT: evidence is DIRECTLY STATED in the turn — verbatim match, clear synonym, or direct paraphrase. Must be explicitly present in the source text, not merely implied.
- CONTRADICTION: evidence directly contradicts the turn (opposite claim, wrong numbers, negation mismatch).
- NEUTRAL: evidence is related but NOT directly stated in the turn. This includes plausible inferences, different aspects of the same topic, and related facts not mentioned.

Examples:

NEUTRAL (plausible unstated):
  Source: "ARM 서버에서 동작한다"
  Evidence: "ARM Neoverse-N1 4코어"
  → Source says "ARM server" but never specifies processor details. → NEUTRAL

NEUTRAL (different aspect):
  Source: "비타민C는 면역력에 좋다"
  Evidence: "비타민C는 피부 미백에 도움된다"
  → Related but different aspect. NOT in source. → NEUTRAL

NEUTRAL (related inference):
  Source: "시스템은 PostgreSQL 16을 사용한다"
  Evidence: "PostgreSQL 16은 빠르다"
  → Names the DB but doesn't discuss speed. → NEUTRAL

ENTAILMENT (direct statement):
  Source: "PostgreSQL 16을 데이터베이스로 사용한다"
  Evidence: "PostgreSQL 16"
  → Directly stated. → ENTAILMENT

CONTRADICTION:
  Source: "오늘 날씨가 좋다"
  Evidence: "오늘 날씨가 나쁘다"
  → Directly contradicts. → CONTRADICTION

IMPORTANT — Reasoning steps for each finding:
1. Is the evidence text DIRECTLY present in the turn context? If yes → ENTAILMENT
2. Does the evidence DIRECTLY contradict the turn? If yes → CONTRADICTION
3. If neither explicitly stated nor contradicted → NEUTRAL (regardless of how plausible)

Include category:
- bug / security / performance / quality / data_loss / hallucination

Output STRICT JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "confidence": 0-100,
  "summary": "1-sentence assessment",
  "reasoning": "step-by-step analysis",
  "verification_items": [
    {"check": "...", "result": "pass|fail|partial", "detail": "...", "category": "bug|security|performance|quality|data_loss|hallucination", "grounding": "NEUTRAL|CONTRADICTION|ENTAILMENT"}
  ]
}"""


def load_snapshot(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def build_findings_from_snapshot(snapshot: dict) -> list:
    """Reconstruct findings list from snapshot review_facts (same as day_verify._build_findings_from_turn)."""
    facts = snapshot.get("review_facts", [])
    turns_map = {t["id"]: t for t in snapshot.get("turns", [])}
    findings = []

    for f in facts:
        ftype = f.get("fact_type", "")
        if ftype not in ("text", "user", "thinking", "enrich_meta"):
            continue
        evidence = f.get("evidence", "")[:500]
        if not evidence or evidence == "null":
            continue
        turn_id = f.get("turn_id", "")

        if ftype == "enrich_meta":
            try:
                enrich_data = json.loads(evidence) if isinstance(evidence, str) else evidence
            except json.JSONDecodeError:
                continue
            tldr = enrich_data.get("tldr", "")
            if tldr:
                findings.append({
                    "id": f"ENR-{turn_id[:8]}-tldr", "severity": "medium",
                    "category": "quality",
                    "description": "Enrich tldr summary",
                    "evidence": tldr[:300], "_turn_id": turn_id,
                })
            entities = enrich_data.get("entities", {}) or {}
            if entities.get("files"):
                findings.append({
                    "id": f"ENR-{turn_id[:8]}-files", "severity": "medium",
                    "category": "quality",
                    "description": "Files referenced",
                    "evidence": json.dumps(entities["files"], ensure_ascii=False)[:300],
                    "_turn_id": turn_id,
                })
            if entities.get("technologies"):
                findings.append({
                    "id": f"ENR-{turn_id[:8]}-tech", "severity": "medium",
                    "category": "quality",
                    "description": "Technologies mentioned",
                    "evidence": json.dumps(entities["technologies"], ensure_ascii=False)[:300],
                    "_turn_id": turn_id,
                })
            tags = enrich_data.get("tags", [])
            if tags:
                findings.append({
                    "id": f"ENR-{turn_id[:8]}-tags", "severity": "low",
                    "category": "quality",
                    "description": "Conversation tags",
                    "evidence": json.dumps(tags, ensure_ascii=False)[:200],
                    "_turn_id": turn_id,
                })
        else:
            turn = turns_map.get(turn_id, {})
            desc_text = turn.get("user_turn", "")[:80] or turn.get("text", "")[:80] or "extracted content"
            findings.append({
                "id": f"EX-{turn_id[:8]}-{f.get('fact_index','?')}",
                "severity": "medium",
                "category": "quality",
                "description": f"Extracted {ftype}: {desc_text}",
                "evidence": evidence,
                "source": f.get("fact_action", "extract"),
                "_turn_id": turn_id,
            })

    return findings


def findings_to_context(findings: list, max_evid_len: int = 200) -> str:
    """Build context text from findings (simpler than day_verify TokenBudget version)."""
    parts = [f"## Findings ({len(findings)} total)\n"]
    for fi, f in enumerate(findings):
        ev = f.get("evidence", "")[:max_evid_len]
        tid = f.get("_turn_id", "?")[:8]
        parts.append(f"  [{fi+1}] {f['id']} ({f['severity']}/{f['category']}): \"{ev}\"")
    return "\n".join(parts)


def verify_chunk(chunk: list, ci: int, total: int, system_prompt: str,
                 timeout: int = 480, max_tokens: int = 1024) -> dict:
    """Single chunk verify call. Returns grounding distribution."""
    ctx = findings_to_context(chunk)
    print(f"  [chunk {ci}/{total}] {len(chunk)} items", flush=True)

    try:
        resp = call_llm(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": ctx}],
            model="reviewer",
            max_tokens=max_tokens, temperature=0.1,
            timeout=timeout, json_mode=True, return_meta=True,
        )
    except Exception as e:
        print(f"    ERROR chunk {ci}: {e}", flush=True)
        return {"items": [], "verdict": None, "error": str(e)}

    r_content = resp.get("content", "") or ""
    if isinstance(r_content, str):
        m = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', r_content)
        if m:
            r_content = m.group(1)
        try:
            r = json.loads(r_content.strip()) if r_content.strip() else {}
        except json.JSONDecodeError:
            r = {}
    else:
        r = r_content

    items = r.get("verification_items", [])
    grounding_dist = {"ENTAILMENT": 0, "CONTRADICTION": 0, "NEUTRAL": 0}
    result_dist = {"pass": 0, "fail": 0, "partial": 0}
    for item in items:
        g = item.get("grounding", "")
        if g in grounding_dist:
            grounding_dist[g] += 1
        res = item.get("result", "")
        if res in result_dist:
            result_dist[res] += 1

    return {
        "items": items,
        "verdict": r.get("final_verdict"),
        "summary": r.get("summary", ""),
        "reasoning": r.get("reasoning", ""),
        "grounding_dist": grounding_dist,
        "result_dist": result_dist,
        "n_items": len(items),
        "usage": resp.get("usage", {}),
        "elapsed_ms": resp.get("elapsed_ms", 0),
    }


def run_verify_on_snapshot(snapshot_path: str, system_prompt: str,
                           label: str, chunk_size: int = 6) -> dict:
    """Load snapshot, build findings, chunk-verify, return metrics."""
    print(f"\n{'='*50}", flush=True)
    print(f"  [{label}] VERIFY on {os.path.basename(snapshot_path)}", flush=True)
    print(f"{'='*50}", flush=True)
    t0 = time.monotonic()

    snap = load_snapshot(snapshot_path)
    findings = build_findings_from_snapshot(snap)
    print(f"  [load] {len(findings)} findings from {snap['n_turns']} turns", flush=True)

    if not findings:
        return {"label": label, "findings": 0, "error": "no_findings"}

    chunks = [findings[i:i+chunk_size] for i in range(0, len(findings), chunk_size)]
    merged_items = []
    grounding_dist = {"ENTAILMENT": 0, "CONTRADICTION": 0, "NEUTRAL": 0}
    result_dist = {"pass": 0, "fail": 0, "partial": 0}
    total_ms = 0
    verdicts = []

    workers = min(2, len(chunks))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for ci, chunk in enumerate(chunks):
            f = executor.submit(verify_chunk, chunk, ci + 1, len(chunks), system_prompt)
            futures[f] = ci
        for f in as_completed(futures):
            r = f.result()
            merged_items.extend(r.get("items", []))
            for g, c in r.get("grounding_dist", {}).items():
                grounding_dist[g] = grounding_dist.get(g, 0) + c
            for res, c in r.get("result_dist", {}).items():
                result_dist[res] = result_dist.get(res, 0) + c
            total_ms += r.get("elapsed_ms", 0)
            if r.get("verdict"):
                verdicts.append(r["verdict"])

    elapsed = time.monotonic() - t0
    final_verdict = max(set(verdicts), key=verdicts.count) if verdicts else "unknown"

    result = {
        "label": label,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "turns": snap["n_turns"],
        "findings": len(findings),
        "chunks": len(chunks),
        "items_verified": len(merged_items),
        "elapsed_s": round(elapsed, 1),
        "final_verdict": final_verdict,
        "verdict_counts": {v: verdicts.count(v) for v in set(verdicts)},
        "grounding_dist": {k: v for k, v in sorted(grounding_dist.items()) if v > 0},
        "result_dist": {k: v for k, v in sorted(result_dist.items()) if v > 0},
        "verification_items": merged_items,
    }

    # Save result
    ts = _ts()
    result_path = os.path.join(EVAL_DIR, f"verify_compare_{label}_{ts}.json")
    with open(result_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"  [{label}] {result['items_verified']} items, G={dict(result['grounding_dist'])}, "
          f"R={dict(result['result_dist'])} ({elapsed:.0f}s)", flush=True)
    print(f"  [save] {result_path}", flush=True)

    return result


def print_comparison(results: list):
    """Side-by-side comparison of verify results across prompt variants."""
    print(f"\n{'='*70}", flush=True)
    print(f"  COMPARISON: Verify Prompt Variants", flush=True)
    print(f"{'='*70}", flush=True)

    labels = [r["label"] for r in results if "error" not in r]
    if not labels:
        print("  No valid results to compare", flush=True)
        return

    # Grounding distribution table
    header = f"{'Metric':<25s}" + "".join(f"{l:>15s}" for l in labels)
    print(f"\n  {header}", flush=True)
    print(f"  {'─'* (25 + 15*len(labels))}", flush=True)

    # Items verified
    vals = [str(r.get("items_verified", 0)) for r in results if "error" not in r]
    print(f"  {'Items verified':<25s}" + "".join(f"{v:>15s}" for v in vals), flush=True)

    # Entailment count
    for g in ("ENTAILMENT", "CONTRADICTION", "NEUTRAL"):
        vals = [str(r.get("grounding_dist", {}).get(g, 0)) for r in results if "error" not in r]
        print(f"  {f'{g} (count)':<25s}" + "".join(f"{v:>15s}" for v in vals), flush=True)

    # NEUTRAL ratio
    def neutral_pct(r):
        gd = r.get("grounding_dist", {})
        total = sum(gd.values())
        return f"{gd.get('NEUTRAL', 0)/total*100:.0f}%" if total > 0 else "-"
    vals = [neutral_pct(r) for r in results if "error" not in r]
    print(f"  {f'NEUTRAL %':<25s}" + "".join(f"{v:>15s}" for v in vals), flush=True)

    # Pass/fail distribution
    for res in ("pass", "fail", "partial"):
        vals = [str(r.get("result_dist", {}).get(res, 0)) for r in results if "error" not in r]
        print(f"  {f'{res} (count)':<25s}" + "".join(f"{v:>15s}" for v in vals), flush=True)

    # Elapsed time
    vals = [f"{r.get('elapsed_s', 0):.0f}s" for r in results if "error" not in r]
    print(f"  {'Elapsed':<25s}" + "".join(f"{v:>15s}" for v in vals), flush=True)

    # Final verdict
    vals = [r.get("final_verdict", "?") for r in results if "error" not in r]
    print(f"  {'Final verdict':<25s}" + "".join(f"{v:>15s}" for v in vals), flush=True)

    # Save comparison report
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "n_turns": results[0].get("turns", 0) if results else 0,
        "variants": [{
            "label": r["label"],
            "items_verified": r.get("items_verified", 0),
            "grounding_dist": r.get("grounding_dist", {}),
            "result_dist": r.get("result_dist", {}),
            "elapsed_s": r.get("elapsed_s", 0),
            "final_verdict": r.get("final_verdict", ""),
        } for r in results if "error" not in r],
    }
    ts = _ts()
    report_path = os.path.join(EVAL_DIR, f"verify_compare_summary_{ts}.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"  [report] {report_path}", flush=True)


def switch_pod_b_to_14b() -> bool:
    """Switch Pod B to verify mode (14B Q6 :8083)."""
    env_file = "/opt/ai_data/scripts/current-mode-pod-b.env"
    env = {
        "MODE": "test-q8", "MODEL_NAME": "test-nextcoder-q8",
        "PORT": "8083", "MODEL_FILE": "NextCoder-14B-q6_k_m.gguf",
        "CTX_SIZE": "8192", "THREADS": "4", "THREADS_BATCH": "4", "CACHE_RAM": "512",
    }
    lines = [f"{k}={v}" for k, v in env.items()]
    with open(env_file, "w") as f:
        f.write("\n".join(lines) + "\n")
    subprocess.run(["systemctl", "--user", "restart", "container-devforge-pod-b.service"],
                   capture_output=True, timeout=60)
    for i in range(120):
        h = subprocess.run(["curl", "-sf", "--max-time", "5", "http://127.0.0.1:8083/health"],
                           capture_output=True, text=True, timeout=10)
        if h.returncode == 0 and "ok" in h.stdout:
            print(f"  [switch] 14B ready after {i*3}s", flush=True)
            return True
        time.sleep(3)
    return False


# ── Main ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Full Pipeline NEUTRAL Test")
    parser.add_argument("--limit", type=int, default=10, help="Number of turns")
    parser.add_argument("--skip-phase1", action="store_true", help="Skip pre-verify pipeline stages")
    parser.add_argument("--snapshot", type=str, default="", help="Load existing snapshot path (skip Phase 1 & 2)")
    args = parser.parse_args()

    TEST = test_setup("pipeline_verify_compare",
                      f"Full pipeline verify comparison on {args.limit} real turns")

    # Stop day_cycle
    subprocess.run(["systemctl", "--user", "stop", "devforge-day-cycle.service"], capture_output=True, timeout=30)
    subprocess.run(["pkill", "-9", "-f", "day_cycle.sh"], capture_output=True, timeout=5)

    # ── Phase 1: Pre-verify pipeline ──
    snapshot_path = args.snapshot
    if not snapshot_path and not args.skip_phase1:
        print("\n" + "="*60, flush=True)
        print("  PHASE 1: Pre-Verify Pipeline", flush=True)
        print("  Text Clean → Polish → FTS5 → Extract → MCP Enrich", flush=True)
        print("="*60, flush=True)
        test_heartbeat("phase1_pipeline")

        turns = select_turns(args.limit)
        if not turns:
            test_complete("error: no turns")
            return
        turn_ids = [t["id"] for t in turns]

        if not stage_text_clean(turn_ids):
            test_complete("error: text_clean")
            return
        stage_polish(args.limit)
        stage_fts5()
        stage_extract(args.limit)
        stage_enrich(args.limit)
        snapshot_path = save_snapshot("pre_verify", turn_ids, f"n{args.limit}")
        print(f"\n  [phase1] Snapshot ready: {snapshot_path}", flush=True)

    elif args.skip_phase1 and not snapshot_path:
        # Still need a snapshot — find the latest one
        import glob
        snaps = sorted(glob.glob(os.path.join(EVAL_DIR, "pipeline_verify_compare_pre_verify_*.json")))
        if snaps:
            snapshot_path = snaps[-1]
            print(f"  [phase1] Skipped. Using existing snapshot: {snapshot_path}", flush=True)
        else:
            print("  [error] --skip-phase1 but no existing snapshot found. Run without --skip-phase1 first.", flush=True)
            test_complete("error: no snapshot")
            return

    # ── Phase 2: Verify with 3 prompt variants on snapshot ──
    print("\n" + "="*60, flush=True)
    print("  PHASE 2: Switch to 14B + Verify with 3 prompt variants", flush=True)
    print("="*60, flush=True)
    test_heartbeat("phase2_switch_14b")

    if not switch_pod_b_to_14b():
        print("  [error] 14B not ready", flush=True)
        test_complete("error: 14b_start")
        return

    # Warm-up
    print("  Warming up 14B...", flush=True)
    call_llm([{"role": "user", "content": "Reply OK"}], model="reviewer", max_tokens=5, temperature=0.0, timeout=120)
    print("  Warm-up OK", flush=True)

    # Run 3 verify variants
    variants = [("OLD", OLD_SYSTEM), ("V2", V2_SYSTEM), ("V3_COT", V3_SYSTEM)]
    results = []

    for label, sys_prompt in variants:
        test_heartbeat(f"verify_{label}")
        result = run_verify_on_snapshot(snapshot_path, sys_prompt, label)
        results.append(result)

    # Comparison table
    print_comparison(results)

    test_complete("done")


if __name__ == "__main__":
    main()
