#!/usr/bin/env python3
# Status: experimental
# Path: none — Full day_cycle pipeline end-to-end test (10 real turns)
"""Full Day Cycle Pipeline Test — day_cycle.sh 로직 전체 검증.

Stages (matching day_cycle.sh exactly):
  1. Text Preprocess (text_clean)
  2. Polish + Self-Verify (polish_batch.py, PARALLEL=2, UBATCH=512)
  3. FTS5 Refresh
  4. Embed (embed_batch.py, MAX_BATCH_SIZE=6)
  5. Extract (extract.py, PARALLEL=2)
  6. MCP Enrich
  7. Verify (3 prompt variants: OLD vs V2 vs V3_COT, VERIFY_PARALLEL=2)

Captures: timing per stage, parallel efficiency, batch metrics, grounding distribution.
Snapshots saved after MCP (pre-verify) for reproducible verify comparison.

Usage:
  python3 scripts/tests/day_cycle_pipeline_full.py [--limit 10]
  python3 scripts/tests/day_cycle_pipeline_full.py --snapshot <path>  # skip pipeline, only verify
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "eval")
PIPELINES_DIR = os.path.join(SCRIPTS_DIR, "pipelines")
os.makedirs(EVAL_DIR, exist_ok=True)
sys.path.insert(0, SCRIPTS_DIR)
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from lib.llm_client import call_llm, MODEL_REGISTRY
from lib.test_common import test_setup, test_heartbeat, test_complete
from lib.db import psql_json, psql_ok
from lib.text_cleaner import get_cleaner
from lib.db import esc_sql

# Global test start — used by save_snapshot to capture facts
# from non-original turn_ids that stages create during the run
_test_start_epoch: int = 0

# ── Snapshot helpers ──────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def _fmt_ids(turn_ids: list) -> str:
    return ",".join(f"'{tid}'" for tid in turn_ids)

def save_snapshot(stage: str, turn_ids: list, tag: str = "") -> str:
    ts = _ts()
    safe_tag = f"_{tag}" if tag else ""
    filename = f"day_cycle_full_{stage}{safe_tag}_{ts}.json"
    path = os.path.join(EVAL_DIR, filename)
    id_list = _fmt_ids(turn_ids)
    rows = psql_json(
        f"SELECT id, conversation_id, seq, user_turn, user_turn_clean, "
        f"user_turn_clean_polished, thinking, thinking_clean, "
        f"thinking_clean_polished, text, text_clean, text_clean_polished, "
        f"meta, created_at "
        f"FROM turns WHERE id IN ({id_list}) ORDER BY created_at"
    )
    # Capture facts from original turn_ids plus any created during the run
    # (stages do their own SELECT, may process different IDs)
    extra_fact_filter = ""
    if _test_start_epoch:
        extra_fact_filter = f" OR created_at >= to_timestamp({_test_start_epoch})"
    facts = psql_json(
        f"SELECT turn_id, fact_index, fact_type, evidence, verdict, reason, "
        f"fact_action, fact_confidence, nli_verdict, "
        f"extract_model, verify_model, source, phase "
        f"FROM review_facts "
        f"WHERE turn_id IN ({id_list}){extra_fact_filter} "
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


def run_stage(cmd: list, timeout: int = 3600, label: str = "",
              extract_metrics: list = None) -> dict:
    """Run a pipeline stage with real-time stdout passthrough, capture timing + metrics."""
    t0 = time.monotonic()
    stdout_chunks = []
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        # Read line by line for real-time output
        for line in proc.stdout or []:
            print(line, end="", flush=True)
            stdout_chunks.append(line)
        proc.wait(timeout=timeout)
        ok = proc.returncode == 0
        stdout = "".join(stdout_chunks)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        elapsed = time.monotonic() - t0
        print(f"  [{label}] TIMEOUT after {elapsed:.0f}s", flush=True)
        return {"ok": False, "elapsed_s": round(elapsed, 1), "exit": -1, "stdout": "", "stderr": "TIMEOUT"}
    except Exception as e:
        elapsed = time.monotonic() - t0
        print(f"  [{label}] ERROR: {e}", flush=True)
        return {"ok": False, "elapsed_s": round(elapsed, 1), "exit": -2, "stdout": "", "stderr": str(e)}

    elapsed = time.monotonic() - t0
    print(f"  [{label}] exit={proc.returncode}, {elapsed:.0f}s", flush=True)

    # Extract metrics from stdout
    metrics = {}
    if extract_metrics:
        for pattern, key in extract_metrics:
            m = re.search(pattern, stdout)
            if m:
                metrics[key] = m.group(1)

    return {"ok": ok, "elapsed_s": round(elapsed, 1), "exit": proc.returncode,
            "stdout": stdout, "stderr": "", "metrics": metrics}


# ── Pod B helpers ─────────────────────────────────────────────────

def switch_pod_b(model_key: str, port: int, skip_probe: bool = False,
                 timeout: int = 600) -> dict:
    """Switch Pod B via pod_manager.start_pod_b. Returns success + switch time."""
    from lib.pod_manager import start_pod_b
    t0 = time.monotonic()
    try:
        ok = start_pod_b(model_key, port, skip_probe=skip_probe)
    except Exception as e:
        print(f"  [pod_b] switch to {model_key}:{port} failed: {e}", flush=True)
        ok = False
    elapsed = time.monotonic() - t0
    return {"ok": ok, "elapsed_s": round(elapsed, 1)}


# ── Stage runners ─────────────────────────────────────────────────

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
    n_empty = sum(1 for r in rows if not r.get("text"))
    print(f"  [select] {len(rows)} turns (min={min(sizes)}, max={max(sizes)}, "
          f"avg={sum(sizes)//len(sizes)} chars, {n_empty} empty text)", flush=True)
    for r in rows:
        preview = (r.get("user_turn", "") or "")[:50]
        print(f"    {r['id'][:8]} ({r.get('text_len',0)}c) \"{preview}\"", flush=True)
    return rows


def stage_text_clean(turn_ids: list) -> dict:
    """Text Preprocess — matches day_cycle.sh text_clean.py stage."""
    print(f"\n{'='*50}", flush=True)
    print(f"  Stage 1/6: Text Preprocess (text_clean)", flush=True)
    print(f"{'='*50}", flush=True)
    test_heartbeat("text_clean")
    t0 = time.monotonic()

    cl = get_cleaner()
    id_list = _fmt_ids(turn_ids)
    needs = psql_json(
        f"SELECT id, user_turn, text, thinking FROM turns WHERE id IN ({id_list}) "
        f"AND (text_clean IS NULL OR text_clean = '')"
    )
    cleaned = 0
    for t in (needs or []):
        sql = (
            f"UPDATE turns SET "
            f"user_turn_clean = '{esc_sql(cl.clean((t.get('user_turn') or '')[:2000]))}', "
            f"text_clean = '{esc_sql(cl.clean((t.get('text') or '')[:8000]))}', "
            f"thinking_clean = '{esc_sql(cl.clean((t.get('thinking') or '')[:4000]))}' "
            f"WHERE id = '{t['id']}'"
        )
        if psql_ok(sql):
            cleaned += 1

    elapsed = time.monotonic() - t0
    print(f"  [done] {cleaned}/{len(turn_ids)} turns cleaned ({elapsed:.1f}s)", flush=True)
    return {"ok": True, "elapsed_s": round(elapsed, 1), "n_cleaned": cleaned}


def stage_polish(limit: int) -> dict:
    """Polish + Self-Verify — matches day_cycle.sh polish stage, with retry on Pod B failure."""
    print(f"\n{'='*50}", flush=True)
    print(f"  Stage 2/6: Polish + Self-Verify (4B Q8 :8082, PARALLEL=2)", flush=True)
    print(f"{'='*50}", flush=True)

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        test_heartbeat(f"polish_attempt_{attempt}")
        if attempt > 1:
            print(f"  [retry] Polish attempt {attempt}/{max_retries}...", flush=True)
            # Check Pod B — if down, switch to polish-4b
            try:
                urllib.request
                resp = urllib.request.urlopen("http://127.0.0.1:8082/health", timeout=5)
                if resp.status != 200:
                    raise OSError("health not ok")
            except Exception:
                print(f"  [retry] Pod B :8082 down — re-switching to polish-4b", flush=True)
                sw = switch_pod_b("polish-4b", 8082)
                if not sw["ok"]:
                    print(f"  [error] polish-4b re-switch failed — abort", flush=True)
                    return {"ok": False, "elapsed_s": 0, "exit": -1, "metrics": {}}
                print(f"  [retry] Pod B ready after {sw['elapsed_s']}s", flush=True)
                time.sleep(15)  # settling

        result = run_stage(
            [sys.executable, "-u", os.path.join(PIPELINES_DIR, "polish_batch.py"),
             "--limit", str(limit)],
            timeout=7200, label="polish",
            extract_metrics=[
                (r"Batch (\d+)/", "batches"),
                (r"(\d+)/\d+ turns", "turns_processed"),
            ],
        )

        # Success if any turns were processed
        stdout = result.get("stdout", "")
        if "0 ok" not in stdout and "0/" not in stdout:
            return result
        if "Connection refused" not in stdout and "returned None" not in stdout:
            return result  # non-connection errors are not retryable

        if attempt < max_retries:
            print(f"  [retry] Polish failed (attempt {attempt}) — Pod B may have restarted", flush=True)
            time.sleep(10)

    return result


def stage_fts5() -> dict:
    """FTS5 Refresh — always runs (idempotent)."""
    print(f"\n{'='*50}", flush=True)
    print(f"  Stage 3/6: FTS5 Refresh", flush=True)
    print(f"{'='*50}", flush=True)
    test_heartbeat("fts5")
    return run_stage(
        [sys.executable, "-u", os.path.join(PIPELINES_DIR, "fts5_refresh.py")],
        timeout=120, label="fts5",
    )


def stage_embed(limit: int) -> dict:
    """Embed — switch Pod B to embed mode, run embed_batch.py, switch back."""
    print(f"\n{'='*50}", flush=True)
    print(f"  Stage 4/6: Embed (8B f16 :8081, MAX_BATCH_SIZE=6)", flush=True)
    print(f"{'='*50}", flush=True)
    test_heartbeat("embed_switch")

    # Switch Pod B to embed mode (:8081)
    sw = switch_pod_b("embed", 8081, skip_probe=True)
    print(f"  [pod_b] embed mode: {'OK' if sw['ok'] else 'FAIL'} ({sw['elapsed_s']}s)", flush=True)

    # Get embedder port from MODEL_REGISTRY
    embed_port = MODEL_REGISTRY.get("embedder", {}).get("port", 8081)
    print(f"  [embed] Waiting for :{embed_port} health...", flush=True)
    for i in range(120):
        try:
            urllib.request
            resp = urllib.request.urlopen(f"http://127.0.0.1:{embed_port}/health", timeout=5)
            if resp.status == 200:
                print(f"  [embed] Ready after {(i+1)*5}s", flush=True)
                break
        except Exception:
            pass
        time.sleep(5)
    else:
        print(f"  [embed] Not ready after 600s — skip", flush=True)
        switch_pod_b("extractor", 8082, skip_probe=True)
        return {"ok": False, "elapsed_s": 600, "exit": -1}

    result = run_stage(
        [sys.executable, "-u", os.path.join(PIPELINES_DIR, "embed_batch.py"),
         "--limit", str(limit)],
        timeout=3600, label="embed",
        extract_metrics=[
            (r"Batch (\d+)/", "batches"),
            (r"(\d+)/\d+ embedded", "n_embedded"),
        ],
    )

    # Switch back to extractor mode (:8082)
    print(f"  [pod_b] Switching back to extractor mode (:8082)...", flush=True)
    switch_pod_b("extractor", 8082, skip_probe=True)
    return result


def stage_extract(limit: int) -> dict:
    """Extract — matches day_cycle.sh extract phase."""
    print(f"\n{'='*50}", flush=True)
    print(f"  Stage 5/6: Extract (7B Q8 :8082, PARALLEL=2)", flush=True)
    print(f"{'='*50}", flush=True)
    test_heartbeat("extract")

    # Check Pod A reranker (day_cycle.sh does ensure_pod_a "reranker")
    reranker_port = MODEL_REGISTRY.get("reranker", {}).get("port", 8080)
    try:
        urllib.request
        resp = urllib.request.urlopen(f"http://127.0.0.1:{reranker_port}/health", timeout=3)
        if resp.status == 200:
            print(f"  [reranker] Pod A :{reranker_port} healthy", flush=True)
        else:
            print(f"  [reranker] Pod A :{reranker_port} not healthy — no reranker verdicts", flush=True)
    except Exception:
        print(f"  [reranker] Pod A :{reranker_port} unreachable — no reranker verdicts", flush=True)

    # Wait for extractor mode (:8082) health
    for i in range(60):
        try:
            urllib.request
            resp = urllib.request.urlopen(f"http://127.0.0.1:8082/health", timeout=5)
            if resp.status == 200:
                print(f"  [extractor] :8082 ready after {(i+1)*3}s", flush=True)
                break
        except Exception:
            pass
        time.sleep(3)
    else:
        print(f"  [extractor] :8082 not ready after 180s — proceeding anyway", flush=True)

    return run_stage(
        [sys.executable, "-u", os.path.join(PIPELINES_DIR, "extract.py"),
         "--limit", str(limit)],
        timeout=7200, label="extract",
        extract_metrics=[
            (r"(\d+) of (\d+) done", "n_extracted"),
            (r"(\d+) concurrent LLM call", "parallel_calls"),
            (r"(\d+) facts", "n_facts"),
        ],
    )


def stage_enrich(limit: int) -> dict:
    """Enrich — stays on :8082."""
    print(f"\n{'='*50}", flush=True)
    print(f"  Stage 6/6: Enrich (7B Q8 :8082)", flush=True)
    print(f"{'='*50}", flush=True)
    test_heartbeat("enrich")
    return run_stage(
        [sys.executable, "-u", os.path.join(PIPELINES_DIR, "enrich.py"),
         "--limit", str(limit)],
        timeout=3600, label="enrich",
        extract_metrics=[
            (r"(\d+)/", "enriched"),
            (r"failed.*?(\d+)", "failed"),
        ],
    )


# ── Verify prompt variants ───────────────────────────────────────

# OLD: matches day_verify.py VERIFIER_SYSTEM_PROMPT (E-C-N order)
OLD_SYSTEM = """You are a code review verifier. Examine all findings and decide for each:
- approved: correct, can proceed
- rejected: incorrect or not actionable
- needs_review: requires deeper analysis

Classify each finding into a category:
- bug / security / performance / quality / data_loss / hallucination

IMPORTANT — Faithfulness check: Each finding has an "evidence" field extracted from the
conversation. Verify that the evidence actually appears in or is directly supported by the
turn. If the evidence is fabricated, exaggerated, or contradicts the turn context, mark
it as "hallucination" category with result "fail".

IMPORTANT — Grounding (NLI): For each finding, determine the logical relationship between
the evidence and the conversation turn context. Classify as:
- ENTAILMENT: evidence is directly supported by the turn (same facts, correct numbers, logically follows)
- CONTRADICTION: evidence contradicts the turn (opposite claim, wrong numbers, negation mismatch)
- NEUTRAL: evidence is related but not directly entailed (reasonable inference, plausible but unstated)

Output STRICT JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "confidence": 0-100,
  "summary": "...",
  "reasoning": "...",
  "verification_items": [
    {"check": "...", "result": "pass|fail|partial", "detail": "...", "category": "...", "grounding": "ENTAILMENT|CONTRADICTION|NEUTRAL"}
  ]
}"""

# V2: N-C-E order + NEUTRAL few-shot
V2_SYSTEM = """You are a code review verifier. Examine all findings and decide for each:
- approved / rejected / needs_review

Categories: bug / security / performance / quality / data_loss / hallucination

IMPORTANT — Grounding (NLI):
- NEUTRAL: evidence is related but NOT directly entailed (reasonable inference, plausible but unstated, or different aspect of same topic)
- CONTRADICTION: evidence contradicts the turn
- ENTAILMENT: evidence is directly supported by the turn

Examples of NEUTRAL:
  Source: "PostgreSQL 16을 데이터베이스로 사용한다"
  Evidence: "PostgreSQL 16은 빠르다"
  → NEUTRAL (속도는 source에 언급되지 않음)

  Source: "비타민C는 면역력에 좋다"
  Evidence: "비타민C는 피부 미백에 도움된다"
  → NEUTRAL (같은 주제지만 다른 측면)

Output STRICT JSON:
{
  "final_verdict": "...",
  "confidence": 0-100,
  "summary": "...",
  "reasoning": "...",
  "verification_items": [
    {"check": "...", "result": "pass|fail|partial", "detail": "...", "category": "...", "grounding": "NEUTRAL|CONTRADICTION|ENTAILMENT"}
  ]
}"""

# V3_COT: CoT + strict ENTAILMENT + all 3 NEUTRAL subtypes
V3_SYSTEM = """You are a code review verifier. Determine the relationship between each finding's evidence and the context.

Definitions:
- ENTAILMENT: evidence is DIRECTLY STATED in the turn — verbatim match, clear synonym, direct paraphrase. Must be explicitly present. NOT merely implied.
- CONTRADICTION: evidence directly contradicts the turn.
- NEUTRAL: evidence is related but NOT directly stated. Includes plausible inferences, different aspects, related facts not mentioned.

Examples:
  Source: "ARM 서버에서 동작한다"  Evidence: "ARM Neoverse-N1 4코어" → NEUTRAL (processor not specified)
  Source: "비타민C는 면역력에 좋다"  Evidence: "비타민C는 피부 미백에 도움된다" → NEUTRAL (different aspect)
  Source: "PostgreSQL 16을 사용한다"  Evidence: "PostgreSQL 16은 빠르다" → NEUTRAL (speed not mentioned)
  Source: "PostgreSQL 16을 사용한다"  Evidence: "PostgreSQL 16" → ENTAILMENT (directly stated)
  Source: "날씨가 좋다"  Evidence: "날씨가 나쁘다" → CONTRADICTION

Reasoning:
1. Is evidence DIRECTLY stated in source? → ENTAILMENT
2. Does it DIRECTLY contradict? → CONTRADICTION
3. Otherwise → NEUTRAL (regardless of plausibility)

Output STRICT JSON:
{
  "final_verdict": "...",
  "confidence": 0-100,
  "summary": "...",
  "reasoning": "...",
  "verification_items": [
    {"check": "...", "result": "pass|fail|partial", "detail": "...", "category": "...", "grounding": "NEUTRAL|CONTRADICTION|ENTAILMENT"}
  ]
}"""


# ── Verify helpers ───────────────────────────────────────────────

def load_snapshot(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def build_findings_from_snapshot(snapshot: dict) -> list:
    """Reconstruct findings from snapshot facts (matches day_verify._build_findings_from_turn)."""
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
            for ek in ("files", "technologies"):
                ev = entities.get(ek)
                if ev:
                    findings.append({
                        "id": f"ENR-{turn_id[:8]}-{ek[:3]}", "severity": "medium",
                        "category": "quality",
                        "description": f"Enrich {ek}",
                        "evidence": json.dumps(ev, ensure_ascii=False)[:300],
                        "_turn_id": turn_id,
                    })
            tags = enrich_data.get("tags", [])
            if tags:
                findings.append({
                    "id": f"ENR-{turn_id[:8]}-tag", "severity": "low",
                    "category": "quality",
                    "description": "Tags",
                    "evidence": json.dumps(tags, ensure_ascii=False)[:200],
                    "_turn_id": turn_id,
                })
        else:
            turn = turns_map.get(turn_id, {})
            desc = (turn.get("user_turn", "") or "")[:80] or (turn.get("text", "") or "")[:80]
            findings.append({
                "id": f"EX-{turn_id[:8]}-{f.get('fact_index','?')}",
                "severity": "medium", "category": "quality",
                "description": f"Extracted {ftype}: {desc}",
                "evidence": evidence,
                "_turn_id": turn_id,
            })

    return findings


def findings_to_context(findings: list) -> str:
    parts = [f"## Findings ({len(findings)} total)\n"]
    for fi, f in enumerate(findings):
        ev = (f.get("evidence", "") or "")[:200]
        parts.append(f"  [{fi+1}] {f['id']} ({f['severity']}/{f['category']}): \"{ev}\"")
    return "\n".join(parts)


def verify_chunk(chunk: list, ci: int, total: int, system_prompt: str) -> dict:
    ctx = findings_to_context(chunk)
    print(f"  [chunk {ci}/{total}] {len(chunk)} items", flush=True)
    try:
        resp = call_llm(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": ctx}],
            model="reviewer", max_tokens=1024, temperature=0.1,
            timeout=480, json_mode=True, return_meta=True,
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
    gdist = {"ENTAILMENT": 0, "CONTRADICTION": 0, "NEUTRAL": 0}
    rdist = {"pass": 0, "fail": 0, "partial": 0}
    for item in items:
        g = item.get("grounding", "")
        if g in gdist: gdist[g] += 1
        res = item.get("result", "")
        if res in rdist: rdist[res] += 1

    return {
        "items": items, "verdict": r.get("final_verdict"),
        "summary": r.get("summary", ""), "reasoning": r.get("reasoning", ""),
        "grounding_dist": gdist, "result_dist": rdist,
        "n_items": len(items),
        "usage": resp.get("usage", {}), "elapsed_ms": resp.get("elapsed_ms", 0),
    }


def run_verify(snapshot_path: str, system_prompt: str, label: str,
               chunk_size: int = 6) -> dict:
    """Run verify on snapshot data with given prompt. chunk_size=6 matches day_verify.py."""
    print(f"\n{'='*50}", flush=True)
    print(f"  [VERIFY:{label}] on {os.path.basename(snapshot_path)} (chunk={chunk_size})", flush=True)
    print(f"{'='*50}", flush=True)
    t0 = time.monotonic()

    snap = load_snapshot(snapshot_path)
    findings = build_findings_from_snapshot(snap)
    if not findings:
        return {"label": label, "findings": 0, "error": "no_findings"}

    chunks = [findings[i:i+chunk_size] for i in range(0, len(findings), chunk_size)]
    print(f"  [load] {len(findings)} findings → {len(chunks)} chunks (chunk={chunk_size})", flush=True)

    merged_items = []
    gdist = {"ENTAILMENT": 0, "CONTRADICTION": 0, "NEUTRAL": 0}
    rdist = {"pass": 0, "fail": 0, "partial": 0}
    total_ms = 0
    verdicts = []

    # VERIFY_PARALLEL=2 (matches day_verify.py)
    with ThreadPoolExecutor(max_workers=min(2, len(chunks))) as executor:
        futures = {executor.submit(verify_chunk, ch, ci+1, len(chunks), system_prompt): ci
                   for ci, ch in enumerate(chunks)}
        for f in as_completed(futures):
            r = f.result()
            merged_items.extend(r.get("items", []))
            for k, v in r.get("grounding_dist", {}).items(): gdist[k] = gdist.get(k, 0) + v
            for k, v in r.get("result_dist", {}).items(): rdist[k] = rdist.get(k, 0) + v
            total_ms += r.get("elapsed_ms", 0)
            if r.get("verdict"): verdicts.append(r["verdict"])

    elapsed = time.monotonic() - t0
    final_v = max(set(verdicts), key=verdicts.count) if verdicts else "unknown"

    result = {
        "label": label, "chunk_size": chunk_size,
        "turns": snap["n_turns"], "findings": len(findings),
        "chunks": len(chunks), "items_verified": len(merged_items),
        "elapsed_s": round(elapsed, 1), "final_verdict": final_v,
        "verdict_counts": {v: verdicts.count(v) for v in set(verdicts)},
        "grounding_dist": {k: v for k, v in sorted(gdist.items()) if v > 0},
        "result_dist": {k: v for k, v in sorted(rdist.items()) if v > 0},
    }

    ts = _ts()
    rpath = os.path.join(EVAL_DIR, f"day_cycle_verify_{label}_ch{chunk_size}_{ts}.json")
    with open(rpath, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"  [{label}] {result['items_verified']} items, G={result['grounding_dist']}, "
          f"R={result['result_dist']} ({elapsed:.0f}s)", flush=True)
    print(f"  [save] {rpath}", flush=True)
    return result


# ── Reporting ────────────────────────────────────────────────────

def print_timing(stages: list):
    """Print stage timing table."""
    print(f"\n{'='*60}", flush=True)
    print(f"  PIPELINE TIMING", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"  {'Stage':<30s} {'Result':>8s} {'Time':>8s}", flush=True)
    print(f"  {'─'*46}", flush=True)
    total = 0
    for s in stages:
        t = s.get("elapsed_s", 0)
        total += t
        r = "OK" if s.get("ok", False) else "FAIL"
        print(f"  {s['name']:<30s} {r:>8s} {t:>7.0f}s", flush=True)
    print(f"  {'─'*46}", flush=True)
    print(f"  {'TOTAL':<30s} {'':>8s} {total:>7.0f}s", flush=True)

    # Metrics
    print(f"\n  {'Metric':<30s} {'Value':>20s}", flush=True)
    print(f"  {'─'*50}", flush=True)
    for s in stages:
        for k, v in s.get("metrics", {}).items():
            print(f"  {s['name']}.{k:<26s} {v:>20s}", flush=True)


def print_verify_comparison(results: list):
    """Side-by-side verify prompt comparison."""
    valid = [r for r in results if "error" not in r]
    if not valid:
        print("  No valid verify results", flush=True)
        return

    labels = [r["label"] for r in valid]
    print(f"\n{'='*70}", flush=True)
    print(f"  VERIFY PROMPT COMPARISON", flush=True)
    print(f"{'='*70}", flush=True)
    print(f"  {'Metric':<25s}" + "".join(f"{l:>15s}" for l in labels), flush=True)
    print(f"  {'─'* (25 + 15*len(labels))}", flush=True)

    for metric in ("items_verified", "chunks", "chunk_size"):
        vals = [str(r.get(metric, 0)) for r in valid]
        print(f"  {metric:<25s}" + "".join(f"{v:>15s}" for v in vals), flush=True)

    for g in ("ENTAILMENT", "CONTRADICTION", "NEUTRAL"):
        vals = [str(r.get("grounding_dist", {}).get(g, 0)) for r in valid]
        print(f"  {f'{g} (count)':<25s}" + "".join(f"{v:>15s}" for v in vals), flush=True)

    def pct(r, key):
        gd = r.get("grounding_dist", {})
        total = sum(gd.values())
        if total == 0: return "-"
        v = gd.get(key, 0)
        return f"{v} ({v/total*100:.0f}%)"
    for g in ("ENTAILMENT", "CONTRADICTION", "NEUTRAL"):
        vals = [pct(r, g) for r in valid]
        print(f"  {f'  {g} %':<25s}" + "".join(f"{v:>15s}" for v in vals), flush=True)

    for res in ("pass", "fail", "partial"):
        vals = [str(r.get("result_dist", {}).get(res, 0)) for r in valid]
        print(f"  {res:<25s}" + "".join(f"{v:>15s}" for v in vals), flush=True)

    vals = [f"{r.get('elapsed_s', 0):.0f}s" for r in valid]
    print(f"  {'Elapsed':<25s}" + "".join(f"{v:>15s}" for v in vals), flush=True)

    # Save
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "n_turns": valid[0].get("turns", 0) if valid else 0,
        "variants": [{
            "label": r["label"],
            "items_verified": r.get("items_verified", 0),
            "grounding_dist": r.get("grounding_dist", {}),
            "result_dist": r.get("result_dist", {}),
            "elapsed_s": r.get("elapsed_s", 0),
            "final_verdict": r.get("final_verdict", ""),
            "chunk_size": r.get("chunk_size", 6),
        } for r in valid],
    }
    ts = _ts()
    rpath = os.path.join(EVAL_DIR, f"day_cycle_verify_summary_{ts}.json")
    with open(rpath, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"  [report] {rpath}", flush=True)


# ── Dynamic batching verification ────────────────────────────────

def verify_parallel_batching(stage_name: str, stdout: str, expected_parallel: int) -> dict:
    """Check that parallel=2 / batch features are actually used."""
    checks = {}
    if "concurrent" in stdout.lower() and str(expected_parallel) in stdout:
        checks["parallel_calls"] = True
    else:
        checks["parallel_calls"] = False

    batch_count = len(re.findall(r"(?:Batch|batch|chunk)\s+\d+", stdout))
    checks["batch_count"] = batch_count
    checks["has_batching"] = batch_count > 0
    return checks


# ── Main ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Full day_cycle pipeline test")
    parser.add_argument("--limit", type=int, default=10, help="Number of turns")
    parser.add_argument("--snapshot", type=str, default="", help="Skip Phase 1-2, load existing snapshot")
    args = parser.parse_args()

    global _test_start_epoch
    _test_start_epoch = int(datetime.now(timezone.utc).timestamp())

    TEST = test_setup("day_cycle_full",
                      f"전체 day_cycle 파이프라인 검증 ({args.limit} turns)")

    subprocess.run(["systemctl", "--user", "stop", "devforge-day-cycle.service"],
                   capture_output=True, timeout=30)
    subprocess.run(["pkill", "-9", "-f", "day_cycle.sh"], capture_output=True, timeout=5)

    # Ensure Pod A reranker is up and mode file is set — prevents kill_all()
    # from stopping Pod A when Pod B switches modes
    from lib.pod_manager import start_pod_a_only
    print("  [pod_a] Ensuring reranker mode...", flush=True)
    start_pod_a_only("reranker", 8080)

    snapshot_path = args.snapshot
    pipeline_stages = []

    # ── Phase 1: Pipeline stages ──
    if not snapshot_path:
        print("\n" + "="*60, flush=True)
        print("  PHASE 1: Full day_cycle Pipeline", flush=True)
        print("  Text Clean → Polish (PARALLEL=2) → FTS5 → Embed (BATCH=6) → Extract (PARALLEL=2) → MCP", flush=True)
        print("="*60, flush=True)
        test_heartbeat("phase1_pipeline")

        turns = select_turns(args.limit)
        if not turns:
            test_complete("error: no turns")
            return
        turn_ids = [t["id"] for t in turns]

        # Stage 1: Text Preprocess
        r1 = stage_text_clean(turn_ids)
        pipeline_stages.append({"name": "1_text_clean", "ok": r1["ok"], "elapsed_s": r1["elapsed_s"], "metrics": {"n_cleaned": str(r1.get("n_cleaned", 0))}})
        snapshot_path = save_snapshot("01_after_clean", turn_ids)

        # Stage 2: Polish — ensure fresh Pod B
        print("  [pod_b] Switching to polish-4b mode (:8082)...", flush=True)
        sw = switch_pod_b("polish-4b", 8082)
        if not sw["ok"]:
            print("  [error] polish-4b mode failed — abort", flush=True)
            test_complete("error: polish-4b not ready")
            return
        print(f"  [pod_b] polish-4b ready ({sw['elapsed_s']}s)", flush=True)
        print("  [pod_b] settling 15s for full model init...", flush=True)
        time.sleep(15)

        r2 = stage_polish(args.limit)
        pipeline_stages.append({"name": "2_polish", "ok": r2["ok"], "elapsed_s": r2["elapsed_s"], "metrics": r2.get("metrics", {})})
        snap2 = save_snapshot("02_after_polish", turn_ids)

        # Stage 3: FTS5
        r3 = stage_fts5()
        pipeline_stages.append({"name": "3_fts5", "ok": r3["ok"], "elapsed_s": r3["elapsed_s"], "metrics": {}})

        # Stage 4: Embed
        r4 = stage_embed(args.limit)
        pipeline_stages.append({"name": "4_embed", "ok": r4["ok"], "elapsed_s": r4["elapsed_s"], "metrics": r4.get("metrics", {})})
        snap4 = save_snapshot("04_after_embed", turn_ids)

        # Stage 5: Extract
        r5 = stage_extract(args.limit)
        pipeline_stages.append({"name": "5_extract", "ok": r5["ok"], "elapsed_s": r5["elapsed_s"], "metrics": r5.get("metrics", {})})
        snap5 = save_snapshot("05_after_extract", turn_ids)

        # Stage 6: MCP
        r6 = stage_enrich(args.limit)
        pipeline_stages.append({"name": "6_enrich", "ok": r6["ok"], "elapsed_s": r6["elapsed_s"], "metrics": r6.get("metrics", {})})
        snapshot_path = save_snapshot("06_after_enrich", turn_ids, f"n{args.limit}")
        print(f"\n  [phase1] Snapshot ready: {snapshot_path}", flush=True)

    # ── Parallel/Batching verification ──
    print(f"\n{'='*60}", flush=True)
    print(f"  PARALLEL & BATCHING VERIFICATION", flush=True)
    print(f"{'='*60}", flush=True)
    for s in pipeline_stages:
        stdout = ""
        if s["name"] == "2_polish" and "polish" in s["name"]:
            # Check from log output (not stored in JSON)
            pass
        metrics = s.get("metrics", {})
        n_batches = metrics.get("batches", "?")
        n_par = metrics.get("parallel_calls", "?")
        print(f"  {s['name']:<25s} batches={n_batches:>5s} concurrent={n_par:>5s}", flush=True)

    # ── Phase 2: Timing report ──
    print_timing(pipeline_stages)

    # ── Phase 3: Verify with 3 prompt variants ──
    print("\n" + "="*60, flush=True)
    print("  PHASE 3: Verify (14B :8083) — 3 prompt variants × 2 chunk sizes", flush=True)
    print("="*60, flush=True)
    test_heartbeat("phase3_verify")

    # Switch Pod B to 14B verify mode
    print("  Switching Pod B to verify-14b mode (:8083)...", flush=True)
    sw = switch_pod_b("verify-14b", 8083)
    if not sw["ok"]:
        print("  [error] 14B not ready — verify comparison skipped", flush=True)
    else:
        # Warm-up
        print("  Warming up 14B...", flush=True)
        try:
            call_llm([{"role": "user", "content": "Reply OK"}], model="reviewer",
                     max_tokens=5, temperature=0.0, timeout=120)
            print("  Warm-up OK", flush=True)
        except Exception as e:
            print(f"  Warm-up failed: {e}", flush=True)

        # 3 variants with chunk_size=6 (day_verify.py default)
        variants = [("OLD", OLD_SYSTEM), ("V2", V2_SYSTEM), ("V3_COT", V3_SYSTEM)]
        verify_results = []
        for label, sys_prompt in variants:
            test_heartbeat(f"verify_{label}")
            r = run_verify(snapshot_path, sys_prompt, label, chunk_size=6)
            verify_results.append(r)

        # Comparison
        print_verify_comparison(verify_results)

    # ── Cleanup ──
    print(f"\n  Restoring Pod B to day mode...", flush=True)
    switch_pod_b("extractor", 8082, skip_probe=True)

    test_complete("done")


if __name__ == "__main__":
    main()
