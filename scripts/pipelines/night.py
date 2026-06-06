#!/usr/bin/env python3
# Status: production
# Path: nightly_batch.sh
"""
DevForge Night Pipeline v2.0 — 3-phase review pipeline.

Architecture (model-loaded-at-switch-time):
  Phase 2  Pod B review-qw       Qwen7B(:8080) — R(refuter) role + all model interaction
  Phase 3  Pod B review-j        NextCoder-14B(:8080) — J(judge) role
  Phase 4  Pod B verify          27B(:8081) — production final verify

Usage:
  # Run full pipeline:
  python3 scripts/pipelines/night.py --all

  # Single phase (after mode already switched):
  python3 scripts/pipelines/night.py --phase 2
"""

import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "eval")
os.makedirs(EVAL_DIR, exist_ok=True)

sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql_ok
from lib.llm_client import call_llm

# ── Ports ────────────────────────────────────────────────────────────────
QWEN7B_PORT = 8080
VERIFY_PORT = 8081
VERIFY_TEST_PORT = 8081
POD_A_REVIEW_PORT = 8083  # R1-8B night mode

# ── Mode files ───────────────────────────────────────────────────────────
MODE_FILE_A = "/opt/ai_data/scripts/current-mode-pod-a.env"
MODE_FILE_B = "/opt/ai_data/scripts/current-mode-pod-b.env"

# ── Model-specific config ────────────────────────────────────────────────
# Overrides per model: timeout, chunk sizes
#   first_chunk: items in first chunk (cold → KV cache warm-up)
#   chunk_size: items per subsequent chunk (0 = no chunking)
MODEL_CFG = {
    "Qwen7B":    {"timeout": 480,  "ctx": 8192, "first_chunk": 0, "chunk_size": 0},
    "NextCoder14B": {"timeout": 1800, "ctx": 6144, "first_chunk": 0, "chunk_size": 0},
    "Qwen27B":   {"timeout": 1200, "ctx": 6144, "first_chunk": 5, "chunk_size": 15},
}

TIMEOUT_SWAP = 600  # large models (22B+) need >5min to load on ARM


def chunk_findings(
    findings: list,
    *,
    first_chunk: int = 0,
    chunk_size: int = 0,
) -> list:
    """Split findings into fixed-size chunks.

    *first_chunk* items go in chunk 1 (cold start → KV cache warm-up).
    Remaining items are split into chunks of *chunk_size* each.
    A trailing chunk ≤3 items is merged into the previous chunk.

    When both params are 0, returns all findings as a single chunk.

    Returns: [{findings: [...], label: "chunk 1/N"}, ...]
    """
    if first_chunk <= 0 or chunk_size <= 0:
        return [{"findings": list(findings), "label": "chunk 1/1"}]

    groups = [list(findings[:first_chunk])]
    rest = findings[first_chunk:]

    for i in range(0, len(rest), chunk_size):
        groups.append(list(rest[i:i + chunk_size]))

    # Merge tiny trailing chunk into predecessor
    if len(groups) > 2 and len(groups[-1]) <= 3:
        groups[-2].extend(groups.pop())

    total = len(groups)
    return [{"findings": g, "label": f"chunk {i+1}/{total}"}
            for i, g in enumerate(groups)]

# ── System prompts ───────────────────────────────────────────────────────

SYSTEM_REFUTER = """You are a review reflector. Given a set of findings and the original data, decide for each finding:
- ACCEPT: the finding is real and correctly identified
- REJECT: the finding is false, irrelevant, or already handled

Output STRICT JSON:
{
  "verdicts": [
    {"id": "F01", "verdict": "accept", "reason": "1-sentence explanation"},
    {"id": "F02", "verdict": "reject", "reason": "1-sentence explanation"}
  ]
}"""

SYSTEM_JUDGE = """You evaluate a code review pipeline. Score Finder (P) on correctness(0-10), coverage(0-10), precision(0-10). Score Reflector (R) on accuracy(0-10), efficiency(0-10), completeness(0-10). P_score = correctness+coverage+precision, R_score = accuracy+efficiency+completeness. Output JSON with P_score, R_score, rubric_evaluation, decision(APPROVED/REJECT), consensus_score(0-100)."""

SYSTEM_VERIFY = """You are a final verification specialist. Review ALL findings across all evaluation
documents. Decide for each finding:
- approved: correct, can be committed
- rejected: incorrect or not actionable
- escalate: requires human review

Also evaluate the overall evaluation quality.

Output STRICT JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "action": "commit|revert|escalate",
  "confidence": 0-100,
  "summary": "1-sentence final decision",
  "reasoning": "step-by-step analysis (3-5 sentences)",
  "verification_items": [
    {"check": "...", "result": "pass|fail|partial", "detail": "..."}
  ],
  "feedback": {
    "proposer_improvement": "...",
    "refuter_improvement": "...",
    "judge_improvement": "..."
  }
}"""


# ── Helpers ──────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


def load_eval_data() -> Dict[str, Any]:
    """Load ALL eval_*.json files into a single consolidated dict."""
    consolidated = {"files": {}, "meta": {"total_files": 0, "total_findings": 0}}
    all_findings = []

    for fname in sorted(os.listdir(EVAL_DIR)):
        if not fname.endswith(".json") or not fname.startswith("eval_"):
            continue
        fpath = os.path.join(EVAL_DIR, fname)
        try:
            with open(fpath) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            continue

        # Normalize: extract findings from various structures
        findings = data.get("findings", [])
        if not findings and data.get("verification_items"):
            findings = data["verification_items"]
        if not findings and data.get("verification_result", {}).get("verification_items"):
            findings = data["verification_result"]["verification_items"]
        if not findings:
            findings = data.get("verdicts", [])
        if not findings and data.get("summary"):
            s = data["summary"]
            desc = s[:200] if isinstance(s, str) else str(s)[:200]
            findings = [{"id": "SUMMARY", "description": desc}]

        entry = {
            "filename": fname,
            "role": data.get("role", data.get("evaluation_type", "unknown")),
            "findings": findings,
            "summary": data.get("summary", "") if isinstance(data.get("summary"), str) else "",
            "verdict": data.get("verification_result", {}).get("final_verdict", ""),
            "confidence": data.get("verification_result", {}).get("confidence", 0),
        }
        consolidated["files"][fname] = entry
        consolidated["meta"]["total_files"] += 1
        consolidated["meta"]["total_findings"] += len(findings)
        all_findings.extend(findings)

    consolidated["all_findings"] = all_findings
    return consolidated


MAX_REPORT_CHARS = 4000  # max chars for consolidated LLM prompt


def build_consolidated_report(all_data: Dict[str, Any]) -> str:
    """Build a prompt containing all findings from all eval files."""
    parts = ["# Consolidated Evaluation Report — All Findings\n"]
    for fname, entry in all_data["files"].items():
        if len("\n".join(parts)) > MAX_REPORT_CHARS:
            parts.append("\n*(truncated — remaining files omitted)*")
            break
        parts.append(f"\n## {fname} ({entry['role']})")
        parts.append(f"Summary: {str(entry.get('summary', ''))[:200]}")
        for f in entry["findings"][:5]:  # max 5 per file
            if len("\n".join(parts)) > MAX_REPORT_CHARS:
                break
            fid = f.get("id", f.get("check", "?"))
            desc = f.get("description", f.get("detail", str(f)[:200]))
            sev = f.get("severity", f.get("result", ""))
            parts.append(f"  [{sev}] {fid}: {desc[:200]}")
    text = "\n".join(parts)
    return text[:MAX_REPORT_CHARS]


POD_A_SERVICE = "container-devforge-pod-a.service"
LARGE_MODES = {"review-j", "verify", "verify_test"}


def _ensure_pod_a(desired: bool) -> None:
    """Stop (False) or start (True) Pod A to free/restore RAM for large models."""
    action = "stop" if not desired else "start"
    r = subprocess.run(
        ["systemctl", "--user", action, POD_A_SERVICE],
        capture_output=True, timeout=30,
    )
    if r.returncode != 0:
        log(f"  [ram] pod-a {action} status={r.returncode}: {r.stderr.decode()[:100]}")
    else:
        log(f"  [ram] pod-a {action} OK")


def swap_pod_b(mode: str, timeout: int = TIMEOUT_SWAP) -> bool:
    """Switch Pod B to the given mode and wait for health.

    For large models (review-j / verify), stops Pod A first to free ~4Gi RAM.
    When switching back to day, restarts Pod A.
    """
    log(f"  [swap] Pod B → {mode}")

    # Free/restore RAM for large model swaps
    will_be_large = mode in LARGE_MODES

    # Stop Pod A for large models (start it back when switching to day)
    if will_be_large:
        _ensure_pod_a(False)  # idempotent — no-op if already stopped
    # Skipping large→large transition; restart Pod A only when leaving large
    if not will_be_large:
        # Check if we're leaving a large mode
        try:
            with open(MODE_FILE_B) as f:
                if f.read().strip().split("=")[-1] in LARGE_MODES:
                    _ensure_pod_a(True)
        except Exception:
            pass
    # Skip restart if already in target mode
    try:
        with open(MODE_FILE_B) as f:
            current = f.read().strip().split("=")[-1]
        if current == mode:
            port = 8081 if mode in ("verify", "verify_test") else 8080
            url = f"http://127.0.0.1:{port}/health"
            try:
                import urllib.request
                with urllib.request.urlopen(url, timeout=3) as resp:
                    if resp.status == 200:
                        log(f"  [swap] already in {mode} mode, health OK")
                        return True
            except Exception:
                pass
            log(f"  [swap] already in {mode} mode but health check failed, restarting")
    except Exception:
        pass
    with open(MODE_FILE_B, "w") as f:
        f.write(f"MODE={mode}")
    r = subprocess.run(
        ["systemctl", "--user", "restart", "container-devforge-swap.service"],
        capture_output=True, timeout=60,
    )
    if r.returncode != 0:
        log(f"  [swap] restart failed: {r.stderr.decode()[:200]}")
        return False
    # Wait for health
    port = 8081 if mode in ("verify", "verify_test") else 8080
    url = f"http://127.0.0.1:{port}/health"
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    log(f"  [swap] :{port} ready after {time.monotonic()-t0:.0f}s")
                    return True
        except Exception:
            pass
        time.sleep(5)
    log(f"  [swap] :{port} TIMEOUT after {timeout}s")
    return False


def swap_pod_a(mode: str, timeout: int = TIMEOUT_SWAP) -> bool:
    """Switch Pod A to the given mode."""
    log(f"  [swap] Pod A → {mode}")
    with open(MODE_FILE_A, "w") as f:
        f.write(f"MODE={mode}")
    r = subprocess.run(
        ["systemctl", "--user", "restart", "container-devforge-pod-a.service"],
        capture_output=True, timeout=60,
    )
    if r.returncode != 0:
        log(f"  [swap] restart failed: {r.stderr.decode()[:200]}")
        return False
    url = f"http://127.0.0.1:{POD_A_REVIEW_PORT}/health"
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    log(f"  [swap] Pod A :{POD_A_REVIEW_PORT} ready after {time.monotonic()-t0:.0f}s")
                    return True
        except Exception:
            pass
        time.sleep(5)
    log(f"  [swap] Pod A TIMEOUT after {timeout}s")
    return False


def call_model(
    messages: List[Dict],
    model: str,
    max_tokens: int = 1024,
    timeout: Optional[int] = None,
    json_mode: bool = True,
    repr: str = "model",
) -> Optional[Dict]:
    """Call an LLM and return JSON result with metadata.

    Uses model-specific timeout from MODEL_CFG when *timeout* is None.
    """
    import re

    # Resolve model-specific timeout
    if timeout is None:
        resolved_model = model
        # Check if model has role alias → physical mapping in MODEl_REGISTRY
        if model in ("night_judge", "night_verify", "night_proposer", "night_reflector"):
            from lib.llm_client import MODEL_REGISTRY, resolve_model
            resolved_model = resolve_model(model)
        timeout = MODEL_CFG.get(resolved_model, {}).get("timeout", 600)

    try:
        result = call_llm(
            messages,
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
            json_mode=json_mode,
            return_meta=True,
        )
        content = result["content"]
        if isinstance(content, str):
            if not content.strip():
                raise ValueError("empty response")
            # Strip markdown JSON code blocks
            m = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', content)
            if m:
                content = m.group(1)
            # Handle trailing text beyond the JSON object
            content = content.strip()
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as e:
                if "Extra data" in str(e):
                    # Try parsing only up to the last closing brace
                    end = content.rfind("}")
                    if end > 0:
                        parsed = json.loads(content[:end+1])
                    else:
                        raise
                else:
                    raise
        else:
            parsed = content
        return {
            "result": parsed,
            "usage": result.get("usage", {}),
            "timings": result.get("timings", {}),
            "elapsed_ms": result.get("elapsed_ms", 0),
            "model": result.get("model", model),
        }
    except Exception as e:
        log(f"  [error] {repr} call failed: {e}")
        return None


def save_output(phase: str, data: Any) -> str:
    """Save phase result to eval directory."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = f"pipeline_{phase}_{timestamp}.json"
    fpath = os.path.join(EVAL_DIR, fname)
    with open(fpath, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"  [save] {fpath}")
    return fpath


# ── Phase implementations ────────────────────────────────────────────────

def phase_1_python_verify(all_data: Dict) -> Dict:
    """Phase 1: Python structural audit. No LLM needed."""
    log("\n=== Phase 1: Python Verify ===")
    result = {
        "phase": 1,
        "role": "python_verify",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "files_loaded": all_data["meta"]["total_files"],
        "total_findings": all_data["meta"]["total_findings"],
        "findings": [],
    }

    for fname, entry in all_data["files"].items():
        result["findings"].append({
            "file": fname,
            "role": entry["role"],
            "finding_count": len(entry["findings"]),
            "has_verdict": bool(entry["verdict"]),
        })
        if entry.get("confidence"):
            result["findings"][-1]["confidence"] = entry["confidence"]

    save_output("01_python_verify", result)
    return result


def _load_latest_phase(phase_num: int, role: str) -> Dict:
    """Load the latest saved output for a given phase."""
    import re
    prefix = f"pipeline_{phase_num:02d}_"
    candidates = [f for f in os.listdir(EVAL_DIR) if f.startswith(prefix) and f.endswith(".json")]
    if not candidates:
        log(f"  [warn] no saved phase {phase_num} output found")
        return {}
    latest = sorted(candidates)[-1]
    fpath = os.path.join(EVAL_DIR, latest)
    log(f"  [load] {fpath}")
    with open(fpath) as f:
        return json.load(f)


def phase_2_refuter(all_data: Dict) -> Dict:
    """Phase 2: Pod B day — Qwen7B(:8080) plays R role. Chunked by ctx-size."""
    log("\n=== Phase 2: Refuter (Qwen7B :8080) ===")
    log("  [mode] Pod B → day (Qwen7B :8080)")

    if not swap_pod_b("day"):
        log("  [error] Pod B day mode swap failed")
        return {"phase": 2, "error": "Pod B swap failed"}

    all_findings = all_data.get("all_findings", [])
    cfg = MODEL_CFG["Qwen7B"]
    chunks = chunk_findings(all_findings, first_chunk=cfg["first_chunk"], chunk_size=cfg["chunk_size"])
    log(f"  [chunk] {len(all_findings)} findings → {len(chunks)} chunks")

    all_verdicts = []
    total_usage = {}
    total_elapsed = 0

    for i, chunk in enumerate(chunks):
        label = chunk["label"]
        log(f"  [llm] Chunk {label} ({len(chunk['findings'])} findings)...")
        chunk_text = json.dumps(chunk["findings"], ensure_ascii=False, indent=2)
        messages = [
            {"role": "system", "content": SYSTEM_REFUTER},
            {"role": "user", "content": f"## Findings (chunk {label})\n{chunk_text}"},
        ]
        response = call_model(messages, "Qwen7B", repr=f"Qwen7B(refuter {label})")
        if not response:
            log(f"  [error] chunk {label} failed, skipping")
            continue

        chunk_verdicts = response.get("result", {}).get("verdicts", [])
        all_verdicts.extend(chunk_verdicts)
        total_elapsed += response.get("elapsed_ms", 0)
        for k, v in response.get("usage", {}).items():
            total_usage[k] = total_usage.get(k, 0) + (v if isinstance(v, int) else 0)

    log(f"  [merge] {len(all_verdicts)} total verdicts from {len(chunks)} chunks")

    result = {
        "phase": 2,
        "role": "refuter_qwen7b",
        "model": "Qwen2.5-Coder-7B-Instruct",
        "port": QWEN7B_PORT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "usage": total_usage,
        "elapsed_ms": total_elapsed,
        "result": {"verdicts": all_verdicts},
        "chunks": len(chunks),
    }

    save_output("02_refuter", result)
    return result


def _merge_judge_chunks(chunk_results: list) -> dict:
    """Merge partial judge scores from multiple chunks into a single verdict."""
    if not chunk_results:
        return {"decision": "REJECT", "action": "escalate", "consensus_score": 0}

    scores = {"P": [], "R": [], "consensus": []}
    decisions = []

    for cr in chunk_results:
        r = cr.get("result", {})
        decisions.append(r.get("decision", "REJECT"))
        scores["consensus"].append(r.get("consensus_score", 0) if isinstance(r.get("consensus_score"), (int, float)) else 0)

        rubric = r.get("rubric_evaluation", {})
        finder = rubric.get("finder", {})
        reflector = rubric.get("reflector", {})
        p = sum(finder.get(k, 0) for k in ("correctness", "coverage", "precision"))
        r_score = sum(reflector.get(k, 0) for k in ("accuracy", "efficiency", "completeness"))
        scores["P"].append(p)
        scores["R"].append(r_score)

    # Average
    n = len(chunk_results)
    final = {
        "P_score": round(statistics.mean(scores["P"]), 1) if scores["P"] else 0,
        "R_score": round(statistics.mean(scores["R"]), 1) if scores["R"] else 0,
        "consensus_score": round(statistics.mean(scores["consensus"]), 1) if scores["consensus"] else 0,
        "decision": "APPROVED" if decisions.count("APPROVED") > n / 2 else "REJECT",
        "action": "commit",
        "disagreement_analysis": f"Merged from {n} chunks ({decisions.count('APPROVED')}/{n} approved)",
    }
    return final


def phase_3_judge(all_data: Dict, refuter_result: Dict = None) -> Dict:
    """Phase 3: Pod B review-j — NextCoder-14B(:8080) plays J role. Chunked."""
    log("\n=== Phase 3: Judge (NextCoder-14B :8080) ===")
    log("  [mode] Pod B → review-j")

    # Auto-load Phase 2 result if not provided (standalone mode)
    if not refuter_result or "result" not in refuter_result:
        refuter_result = _load_latest_phase(2, "refuter_qwen7b")
    if not refuter_result or "result" not in refuter_result:
        log("  [error] no refuter result available (run --phase 2 first)")
        return {"phase": 3, "error": "no refuter result"}

    if not swap_pod_b("review-j"):
        log("  [error] Pod B review-j failed to load")
        return {"phase": 3, "error": "Pod B swap failed"}

    all_findings = all_data.get("all_findings", [])
    refuter_verdicts = refuter_result.get("result", {}).get("verdicts", [])

    # Chunk: pair findings with their refuter verdicts
    verdict_map = {v.get("id", ""): v for v in refuter_verdicts}
    pairs = []
    for f in all_findings:
        fid = f.get("id", f.get("check", "?"))
        pairs.append({"finding": f, "verdict": verdict_map.get(fid, {"verdict": "unknown"})})

    cfg = MODEL_CFG["NextCoder14B"]
    chunks = chunk_findings(pairs, first_chunk=cfg["first_chunk"], chunk_size=cfg["chunk_size"])
    log(f"  [chunk] {len(pairs)} items → {len(chunks)} chunks (first={cfg['first_chunk']}, rest={cfg['chunk_size']})")

    chunk_results = []
    total_usage = {}
    total_elapsed = 0

    for i, chunk in enumerate(chunks):
        label = chunk["label"]
        log(f"  [llm] Chunk {label} ({len(chunk['findings'])} items)...")
        findings_text = json.dumps(chunk["findings"], ensure_ascii=False, indent=2)
        messages = [
            {"role": "system", "content": SYSTEM_JUDGE},
            {"role": "user", "content":
                f"## Findings + Verdicts (chunk {label})\n{findings_text}"},
        ]
        response = call_model(messages, "NextCoder14B", max_tokens=1024,
                              repr=f"NextCoder14B(judge {label})")
        if not response:
            log(f"  [error] chunk {label} failed, skipping")
            continue

        chunk_results.append(response)
        total_elapsed += response.get("elapsed_ms", 0)
        for k, v in response.get("usage", {}).items():
            total_usage[k] = total_usage.get(k, 0) + (v if isinstance(v, int) else 0)

    merged = _merge_judge_chunks(chunk_results)
    log(f"  [merge] P={merged['P_score']} R={merged['R_score']} → {merged['decision']}")

    result = {
        "phase": 3,
        "role": "judge_nextcoder14b",
        "model": "NextCoder-14B-Q8_0",
        "port": QWEN7B_PORT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "usage": total_usage,
        "elapsed_ms": total_elapsed,
        "result": merged,
        "chunks": len(chunks),
        "chunks_completed": len(chunk_results),
    }

    save_output("03_judge", result)
    return result


def _merge_verify_chunks(chunk_results: list) -> dict:
    """Merge partial verify results from multiple chunks."""
    if not chunk_results:
        return {"final_verdict": "rejected", "action": "escalate", "confidence": 0}

    verdicts = []
    all_items = []
    avg_conf = 0
    for cr in chunk_results:
        r = cr.get("result", {})
        verdicts.append(r.get("final_verdict", "rejected"))
        conf = r.get("confidence", 0)
        avg_conf += conf if isinstance(conf, (int, float)) else 0
        for item in r.get("verification_items", []):
            all_items.append(item)

    n = len(chunk_results)
    final_verdict = statistics.mode(verdicts) if n > 1 else verdicts[0]

    return {
        "final_verdict": final_verdict,
        "action": "commit" if final_verdict == "approved" else "escalate",
        "confidence": round(avg_conf / n, 1),
        "summary": f"Merged from {n} chunks",
        "verification_items": all_items[:20],  # cap at 20 items
    }


def phase_4_verify(all_data: Dict, judge_result: Dict = None) -> Dict:
    """Phase 4: Pod B verify — 27B(:8081) production final verify. Chunked."""
    log("\n=== Phase 4: 27B Verify (:8081) ===")
    log("  [mode] Pod B → verify")

    if not judge_result or "result" not in judge_result:
        judge_result = _load_latest_phase(3, "judge_codestral22b")
    if not judge_result or "result" not in judge_result:
        log("  [error] no judge result available (run --phase 3 first)")
        return {"phase": 4, "error": "no judge result"}

    if not swap_pod_b("verify"):
        log("  [error] Pod B verify failed to load")
        return {"phase": 4, "error": "Pod B swap failed"}

    all_findings = all_data.get("all_findings", [])
    judge_verdict = judge_result.get("result", {})

    cfg = MODEL_CFG["Qwen27B"]
    chunks = chunk_findings(all_findings, first_chunk=cfg["first_chunk"], chunk_size=cfg["chunk_size"])
    log(f"  [chunk] {len(all_findings)} findings → {len(chunks)} chunks (first={cfg['first_chunk']}, rest={cfg['chunk_size']})")

    chunk_results = []
    total_usage = {}
    total_elapsed = 0

    for i, chunk in enumerate(chunks):
        label = chunk["label"]
        log(f"  [llm] Chunk {label} ({len(chunk['findings'])} findings)...")
        findings_text = json.dumps(chunk["findings"], ensure_ascii=False, indent=2)
        messages = [
            {"role": "system", "content": SYSTEM_VERIFY},
            {"role": "user", "content":
                f"## Judge Verdict\n{json.dumps(judge_verdict, ensure_ascii=False, indent=2)[:1500]}\n\n"
                f"## Findings (chunk {label})\n{findings_text}"},
        ]
        response = call_model(messages, "Qwen27B", max_tokens=1024,
                              repr=f"27B(verify {label})")
        if not response:
            log(f"  [error] chunk {label} failed, skipping")
            continue

        chunk_results.append(response)
        total_elapsed += response.get("elapsed_ms", 0)
        for k, v in response.get("usage", {}).items():
            total_usage[k] = total_usage.get(k, 0) + (v if isinstance(v, int) else 0)

    merged = _merge_verify_chunks(chunk_results)
    log(f"  [merge] verdict={merged['final_verdict']} conf={merged['confidence']}")

    result = {
        "phase": 4,
        "role": "verify_27b",
        "model": "Qwen3.6-27B",
        "port": VERIFY_PORT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "usage": total_usage,
        "elapsed_ms": total_elapsed,
        "result": merged,
        "chunks": len(chunks),
        "chunks_completed": len(chunk_results),
    }

    save_output("04_verify_27b", result)
    return result



def phase_6_feedback(all_data: Dict, results: Dict) -> Dict:
    """Phase 6: Consolidate all results into final feedback."""
    log("\n=== Phase 6: Consolidated Feedback ===")

    total_phases = sum(1 for v in results.values() if v and "error" not in v)
    total_errors = sum(1 for v in results.values() if v and "error" in v)
    total_elapsed = sum(v.get("elapsed_ms", 0) for v in results.values() if v and "elapsed_ms" in v)
    total_prompt_tok = sum(v.get("usage", {}).get("prompt_tokens", 0) for v in results.values() if v)
    total_gen_tok = sum(v.get("usage", {}).get("completion_tokens", 0) for v in results.values() if v)

    result = {
        "phase": 6,
        "role": "consolidated_feedback",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "execution_summary": {
            "total_phases_completed": total_phases,
            "total_errors": total_errors,
            "total_elapsed_ms": total_elapsed,
            "total_prompt_tokens": total_prompt_tok,
            "total_gen_tokens": total_gen_tok,
            "total_tokens": total_prompt_tok + total_gen_tok,
        },
        "phase_results": {},
    }
    for phase_key, data in results.items():
        if data and "error" not in data:
            result["phase_results"][phase_key] = {
                "role": data.get("role", ""),
                "model": data.get("model", ""),
                "elapsed_ms": data.get("elapsed_ms", 0),
                "usage": data.get("usage", {}),
                "result_preview": str(data.get("result", {}))[:300],
            }

    result["rubric_v3_recommendation"] = {
        "note": "Based on pipeline execution, update rubric with operational_impact criterion",
        "criteria": [
            {"id": "faithfulness", "weight": 0.25},
            {"id": "coverage", "weight": 0.20},
            {"id": "schema_compliance", "weight": 0.15},
            {"id": "conciseness", "weight": 0.15},
            {"id": "cost_efficiency", "weight": 0.15},
            {"id": "operational_impact", "weight": 0.10},
        ],
    }

    save_output("06_feedback", result)
    return result


# ── Orchestrator ──────────────────────────────────────────────────────────

def run_pipeline(phases: Optional[List[int]] = None) -> Dict[str, Any]:
    """Run the full pipeline or selected phases."""
    t_start = time.monotonic()
    log("=" * 60)
    log("DevForge Night Pipeline v2.0")
    log("=" * 60)

    # Phase 0: Load all eval data
    log("Loading eval data files...")
    all_data = load_eval_data()
    log(f"  Loaded {all_data['meta']['total_files']} files, "
        f"{all_data['meta']['total_findings']} total findings")

    if phases is None:
        phases = [1, 2, 3, 4, 6]

    results: Dict[str, Any] = {}

    if 1 in phases:
        results["phase1"] = phase_1_python_verify(all_data)

    if 2 in phases:
        results["phase2"] = phase_2_refuter(all_data)

    if 3 in phases:
        refuter = results.get("phase2", {})
        results["phase3"] = phase_3_judge(all_data, refuter)

    if 4 in phases:
        judge = results.get("phase3", {})
        results["phase4"] = phase_4_verify(all_data, judge)

    if 6 in phases:
        results["phase6"] = phase_6_feedback(all_data, results)

    total = round(time.monotonic() - t_start, 1)
    log("\n" + "=" * 60)
    log(f"Pipeline complete in {total}s")
    phases_run = [p for p in phases if p <= len(results) or p == 6]
    log(f"  Phases: {len(results)} completed in {total}s")
    save_output("complete", {"total_elapsed_s": total, "phases_run": phases, "results": results})

    return results


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="DevForge Night Pipeline v2.0")
    ap.add_argument("--all", action="store_true", help="Run full pipeline")
    ap.add_argument("--phase", type=int, choices=[1, 2, 3, 4, 5, 6],
                    help="Run a single phase (mode must already be set)")
    args = ap.parse_args()

    if args.phase:
        run_pipeline(phases=[args.phase])
    else:
        run_pipeline()

    sys.exit(0)


if __name__ == "__main__":
    main()
