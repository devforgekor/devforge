#!/usr/bin/env python3
# Status: experimental
# Path: none — pipeline redesign v3.0
"""DevForge Night Pipeline v3.0 — Full 6-stage pipeline.

Architecture:
  Phase 0  Boot            Pod A 3B(:8082) + Pod B 7B(:8080) day mode
  Phase 1  Python Verify   No LLM — structural audit
  Phase 2  7B Verify       Pod B reviewer(:8080) — single-pass day_verify
  Phase 3  7B-3B-7B DayPRJ Pod B 7B:P + Pod A 3B:R + Pod B 7B:J — per-turn debate
  Phase 4  Night P-R-J     proposer -> reflector -> judge (model swap per role)
  Phase 5  27B Verify      Pod B verify mode — 27B IQ4_XS(:8081) final gate
  Phase 6  Feedback        Consolidated report
  Phase 7  Restore Day     Pod B day + Pod A 3B(:8082)
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "eval")
os.makedirs(EVAL_DIR, exist_ok=True)

sys.path.insert(0, SCRIPTS_DIR)

from lib.infra.preflight import preflight_checks
from lib.db import psql_ok
from lib.llm_client import call_llm
from lib.token_budget import TokenBudget

REFLECTOR_PORT = 8080
VERIFY_PORT = 8081
POD_A_PORT = 8082

MODE_FILE_B = "/opt/ai_data/scripts/current-mode-pod-b.env"

MODEL_CFG = {
    "reviewer":       {"timeout": 480,  "ctx": 8192, "first_chunk": 0, "chunk_size": 0},
    "extractor":       {"timeout": 180,  "ctx": 8192, "first_chunk": 0, "chunk_size": 0},
    "reflector":      {"timeout": 900,  "ctx": 8192, "first_chunk": 3, "chunk_size": 6},
    "proposer":       {"timeout": 600,  "ctx": 8192, "first_chunk": 5, "chunk_size": 6},
    "judge":          {"timeout": 1800, "ctx": 6144, "first_chunk": 5, "chunk_size": 6},
    "verifier":       {"timeout": 1200, "ctx": 6144, "first_chunk": 5, "chunk_size": 6},
}

TIMEOUT_SWAP = 600
POD_A_DAY_TIMEOUT = 180


def log(msg: str) -> None:
    log_ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{log_ts}] {msg}", flush=True)
    try:
        import os
        os.fsync(sys.stdout.fileno())
    except (OSError, AttributeError):
        pass


# ── System Prompts ────────────────────────────────────────────────────────

VERIFIER_SYSTEM_PROMPT = """You are a code review verifier. Examine all findings and decide for each:
- approved: correct, can proceed
- rejected: incorrect or not actionable
- needs_review: requires deeper analysis

Classify each finding into a category:
- bug: actual logic error or incorrect behavior
- security: vulnerability or unsafe pattern
- performance: efficiency or resource issue
- quality: maintainability, style, or documentation
- data_loss: missing or dropped information

Output STRICT JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "confidence": 0-100,
  "summary": "1-sentence overall assessment",
  "reasoning": "2-3 sentence analysis",
  "verification_items": [
    {"check": "...", "result": "pass|fail|partial", "detail": "...", "category": "bug|security|performance|quality|data_loss"}
  ]
}"""

DAY_PROPOSER_SYSTEM_PROMPT = """You are a code review assistant. Examine the turn and extracted facts below.
Generate findings about potential issues, bugs, or improvements.

CRITICAL RULES:
- Each finding MUST cite specific evidence from the extracted facts.
- If no clear issue exists, return an empty findings list.
- Do NOT fabricate code, file paths, or function names.
- Maximum 20 findings per turn.

Return JSON:
{
  "findings": [
    {
      "id": "D001",
      "severity": "critical|high|medium|low",
      "category": "bug|security|data_loss|performance|quality",
      "description": "1 sentence, under 150 chars",
      "evidence": "quote from extracted facts"
    }
  ]
}"""

DAY_REFLECTOR_SYSTEM_PROMPT = """You are a verdict reviewer. For each finding proposed by the reviewer,
decide ACCEPT or REJECT based ONLY on whether the evidence supports the finding.

CRITICAL RULES:
- ACCEPT: evidence clearly supports the finding.
- REJECT: evidence is weak, missing, or contradicts the finding.
- Do NOT add new findings or modify existing ones.
- One sentence per verdict.

Return JSON:
{
  "verdicts": [
    {"id": "D001", "verdict": "accept", "reason": "evidence supports"},
    {"id": "D002", "verdict": "reject", "reason": "evidence does not support"}
  ]
}"""

DAY_JUDGE_SYSTEM_PROMPT = """You are a scoring judge. Review the findings and verdicts.
Assign a score and make a decision.

- P_score (0-30) = quality of findings
- R_score (0-30) = quality of verdicts
- decision = APPROVED if majority accepted and no critical findings rejected
- decision = REJECT otherwise

Return JSON:
{
  "P_score": 0-30,
  "R_score": 0-30,
  "decision": "APPROVED|REJECT",
  "consensus_score": 0-100,
  "approved": ["D001", "D002"],
  "rejected": ["D003"],
  "report": {"summary": "...", "top_issues": ["..."]}
}"""

SYSTEM_PROPOSER = """You are a code review proposer. Given a structural audit summary, propose findings for deeper investigation.
Each finding must be:
- Specific: reference exact code pattern, file, or logic
- Actionable: clear what should change
- In priority order: critical before minor

Output STRICT JSON:
{
  "findings": [
    {
      "id": "F01",
      "type": "bug|quality|performance|security|data_loss",
      "severity": "critical|major|minor",
      "description": "1-2 sentence description",
      "rationale": "why this matters",
      "proposed_action": "what to do about it"
    }
  ],
  "summary": "1-sentence overall assessment"
}"""

SYSTEM_REFUTER = """You are a review reflector. Given a set of findings, decide for each finding:
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

RUBRIC_SYSTEM_PROMPT = """You are a rubric evaluation specialist. Assess each finding below against the standard criteria.

## Criteria (weighted)
- Correctness (0.35): Is this a real, verifiable issue?
- Actionability (0.30): Is there a clear fix or mitigation?
- Evidence (0.25): Is it backed by specific data or code?
- Novelty (0.10): Does it add new insight?

For each finding assign 0-10 per criterion with brief justification.
weighted_score = correctness*0.35 + actionability*0.30 + evidence*0.25 + novelty*0.10

Return ONLY valid JSON — no markdown, no commentary.
Schema:
{
  "rubric_evaluations": [
    {"id": "finding_id", "correctness": 0-10, "correctness_justification": "...",
     "actionability": 0-10, "actionability_justification": "...",
     "evidence": 0-10, "evidence_justification": "...",
     "novelty": 0-10, "novelty_justification": "...",
     "weighted_score": 0.00}
  ]
}"""

FEEDBACK_VERIFIER_SYSTEM_PROMPT = """You are a final verifier. Review all findings and P-R-J results.

You will receive all findings across evaluation documents. Compare and verify.

After your final verdict, write detailed, actionable feedback per model+role:
e.g., P=proposer, R=reflector, J=judge — separate feedback for each.

=== EVALUATION RUBRIC (self-assessment) ===
Rate your OWN verification on these criteria:
- Thoroughness (0-10): Are all findings compared and cross-checked?
- Evidence Check (0-10): Are verification items backed by specific data?
- Feedback Quality (0-10): Is per-role feedback actionable and constructive?

Return JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "action": "commit|revert|escalate",
  "confidence": 0-100,
  "summary": "1 sentence",
  "reasoning": "3-5 sentences",
  "verification_items": [{"check":"...","result":"pass|fail|partial","detail":"..."}],
  "rubric_evaluation": {
    "thoroughness": "0-10",
    "thoroughness_justification": "...",
    "evidence_check": "0-10",
    "evidence_check_justification": "...",
    "feedback_quality": "0-10",
    "feedback_quality_justification": "..."
  },
  "feedback": {
    "P_proposer": {"model":"proposer","role":"proposer","score":0,"strengths":[],"weaknesses":[],"improvements":[]},
    "R_reflector": {"model":"reflector","role":"reflector","score":0,"strengths":[],"weaknesses":[],"improvements":[]},
    "J_judge": {"model":"judge","role":"judge","score":0,"strengths":[],"weaknesses":[],"improvements":[]}
  }
}"""


# ── Data Loading ──────────────────────────────────────────────────────────

def load_eval_data() -> Dict[str, Any]:
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
        findings = data.get("findings", [])
        if not findings and data.get("verification_items"):
            findings = data["verification_items"]
        if not findings and data.get("verification_result", {}).get("verification_items"):
            findings = data["verification_result"]["verification_items"]
        if not findings:
            findings = data.get("verdicts", [])
        if not findings and data.get("summary"):
            s = data["summary"]
            findings = [{"id": "SUMMARY", "description": str(s)[:200]}]
        entry = {
            "filename": fname, "role": data.get("role", data.get("evaluation_type", "unknown")),
            "findings": findings, "summary": str(data.get("summary", ""))[:200],
            "verdict": data.get("verification_result", {}).get("final_verdict", ""),
            "confidence": data.get("verification_result", {}).get("confidence", 0),
        }
        consolidated["files"][fname] = entry
        consolidated["meta"]["total_files"] += 1
        consolidated["meta"]["total_findings"] += len(findings)
        all_findings.extend(findings)
    consolidated["all_findings"] = all_findings
    return consolidated


# ── Helpers ───────────────────────────────────────────────────────────────

MAX_REPORT_CHARS = 4000


def build_consolidated_report(all_data: Dict[str, Any]) -> str:
    parts = ["# Consolidated Evaluation Report — All Findings\n"]
    for fname, entry in all_data["files"].items():
        if len("\n".join(parts)) > MAX_REPORT_CHARS:
            parts.append("\n*(truncated)*")
            break
        parts.append(f"\n## {fname} ({entry['role']})")
        parts.append(f"Summary: {str(entry.get('summary', ''))[:200]}")
        for f in entry["findings"][:5]:
            if len("\n".join(parts)) > MAX_REPORT_CHARS:
                break
            fid = f.get("id", f.get("check", "?"))
            desc = f.get("description", f.get("detail", str(f)[:200]))
            sev = f.get("severity", f.get("result", ""))
            parts.append(f"  [{sev}] {fid}: {desc[:200]}")
    return "\n".join(parts)[:MAX_REPORT_CHARS]


POD_A_SERVICE = "container-devforge-pod-a.service"


def _build_findings_context(items: List[Dict], phase: str, header: str = "Findings",
                            extra: Optional[List] = None,
                            sev_key=lambda x: x.get("severity", x.get("result", "medium")).lower()
                            ) -> str:
    """TokenBudget-constrained context. Critical first, low last, overflow truncated."""
    budget = TokenBudget(phase)
    parts = []

    def _add(text: str, priority: int = 5) -> bool:
        ok = budget.add_section(text.split("\n")[0][:60], text, priority)
        if ok:
            parts.append(text)
        return ok

    _add(f"## {header} ({len(items)} total)\n", priority=10)

    for sev_name, pri in (("critical", 9), ("high", 7), ("medium", 5), ("low", 3)):
        subset = [it for it in items if sev_key(it) == sev_name]
        if not subset:
            continue
        txt = f"\n[{sev_name.upper()}] ({len(subset)}):"
        for it in subset:
            txt += f"\n  {it.get('id', '?')}: {json.dumps(it, ensure_ascii=False)[:200]}"
        if not _add(txt, priority=pri):
            _add(f"\n[{sev_name.upper()}] ({len(subset)} total — omitted, budget)", priority=pri - 1)

    if extra:
        for label, text, priority in extra:
            _add(f"\n[{label}]\n{text}", priority=priority)

    return "\n".join(parts)


def rubric_evaluate_findings(items: List[Dict], tag: str) -> List[Dict]:
    """Evaluate each finding against rubric criteria using reviewer.

    Returns list of rubric evaluations with weighted_score per finding.
    Stores rubric data in-place on each item as item['rubric'].
    """
    log(f"\n--- Rubric Evaluation ({tag}) ---")
    if not items:
        log("  No findings to evaluate — skipping rubric evaluation")
        return []

    finding_lines = []
    for f in items[:30]:
        fid = f.get("fid", f.get("id", "?"))
        desc = f.get("description", "").replace("\n", " ")[:200]
        sev = f.get("severity", "?")
        cat = f.get("category", "?")
        finding_lines.append(f"  [{sev}/{cat}] {fid}: {desc}")

    user_text = "Evaluate these findings against the rubric:\n\n" + "\n".join(finding_lines)
    msg = [
        {"role": "system", "content": RUBRIC_SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]
    resp = call_model(msg, "reviewer", max_tokens=4096, label=f"rubric_{tag}")
    rubrics = (resp or {}).get("result", {}).get("rubric_evaluations", [])

    rubric_by_id = {r["id"]: r for r in rubrics if "id" in r}
    for f in items:
        fid = f.get("fid", f.get("id", ""))
        if fid in rubric_by_id:
            f["rubric"] = rubric_by_id[fid]

    avg_score = 0.0
    if rubrics:
        scores = [r.get("weighted_score", 0) for r in rubrics if r.get("weighted_score") is not None]
        avg_score = sum(scores) / len(scores) if scores else 0.0

    log(f"  Evaluated {len(rubrics)} findings, avg weighted_score={avg_score:.2f}")
    return rubrics


def _build_category_summary(verification_items: List[Dict], total_findings: int) -> str:
    """Aggregate Phase 2 categories into a one-line global context summary.

    Includes severity breakdown, per-category result distribution, and overall verdict.
    Example: "Category Distribution: bug=8(crit=2,high=4,med=2) [fail=5,partial=2,pass=1];
               security=4(crit=1,high=3) [fail=1,partial=3]... (39 total) | Verdict: fail=9, partial=26, pass=1"
    """
    if not verification_items:
        return ""
    cats: Dict[str, Dict[str, int]] = {}
    cats_result: Dict[str, Dict[str, int]] = {}
    verdict_counts: Dict[str, int] = {}
    for item in verification_items:
        cat = item.get("category", "other")
        sev = item.get("severity", item.get("result", "medium"))
        if cat not in cats:
            cats[cat] = {}
        cats[cat][sev] = cats[cat].get(sev, 0) + 1
        v = item.get("result", "unknown")
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
        # Per-category result (pass/fail/partial) distribution
        if cat not in cats_result:
            cats_result[cat] = {}
        cats_result[cat][v] = cats_result[cat].get(v, 0) + 1

    cat_parts = []
    for cat, sevs in sorted(cats.items(), key=lambda x: -sum(x[1].values())):
        sev_parts = [f"{s}={c}" for s, c in sorted(sevs.items())]
        total = sum(sevs.values())
        res_parts = cats_result.get(cat, {})
        res_str = f" [{','.join(f'{r}={c}' for r,c in sorted(res_parts.items()))}]" if res_parts else ""
        cat_parts.append(f"{cat}={total}({','.join(sev_parts)}){res_str}")
    verdict_str = ", ".join(f"{k}={v}" for k, v in sorted(verdict_counts.items()))
    return f"Category Distribution: {'; '.join(cat_parts)} ({total_findings} total) | Verdict: {verdict_str}"


def _load_historical_verify_context() -> str:
    """Summarise previous verify evaluations for Phase 4 context (rubric mode).

    Reads eval_verify_*.json files from EVAL_DIR and extracts verdict/summary.
    """
    parts = []
    for fname in sorted(os.listdir(EVAL_DIR)):
        if not fname.startswith("eval_verify_") or not fname.endswith(".json"):
            continue
        fpath = os.path.join(EVAL_DIR, fname)
        try:
            with open(fpath) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            continue
        vr = data.get("verification_result", {})
        verdict = vr.get("final_verdict", data.get("final_verdict", "?"))
        action = vr.get("action", "?")
        confidence = vr.get("confidence", data.get("confidence", "?"))
        summary = str(vr.get("summary", data.get("summary", "")))[:150]
        if verdict != "?":
            label = data.get("role", fname.replace(".json", ""))
            parts.append(f"  {label}: verdict={verdict} action={action} confidence={confidence}")
            if summary:
                parts.append(f"    summary: {summary}")
    if not parts:
        return ""
    return "\n" + "\n".join(parts)


LARGE_MODES = {"review-p", "review-r", "review-j", "verify"}


def _ensure_pod_a(desired: bool) -> None:
    action = "stop" if not desired else "start"
    r = subprocess.run(["systemctl", "--user", action, POD_A_SERVICE], capture_output=True, timeout=30)
    if r.returncode != 0:
        log(f"  [ram] pod-a {action} status={r.returncode}: {r.stderr.decode()[:100]}")
    else:
        log(f"  [ram] pod-a {action} OK")


def swap_pod_b(mode: str, timeout: int = TIMEOUT_SWAP) -> bool:
    log(f"  [swap] Pod B -> {mode}")
    will_be_large = mode in LARGE_MODES
    if will_be_large:
        _ensure_pod_a(False)
    if not will_be_large:
        try:
            with open(MODE_FILE_B) as f:
                if f.read().strip().split("=")[-1] in LARGE_MODES:
                    _ensure_pod_a(True)
        except Exception:
            pass
    try:
        with open(MODE_FILE_B) as f:
            current = f.read().strip().split("=")[-1]
        if current == mode:
            port = 8081 if mode == "verify" else 8080
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
    r = subprocess.run(["systemctl", "--user", "restart", "container-devforge-pod-b.service"],
                       capture_output=True, timeout=60)
    if r.returncode != 0:
        log(f"  [swap] restart failed: {r.stderr.decode()[:200]}")
        return False
    port = 8081 if mode == "verify" else 8080
    url = f"http://127.0.0.1:{port}/health"
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    log(f"  [swap] :{port} ready after {time.monotonic() - t0:.0f}s")
                    return True
        except Exception:
            pass
        time.sleep(5)
    log(f"  [swap] :{port} TIMEOUT after {timeout}s")
    return False


def call_model(messages: List[Dict], model: str, max_tokens: int = 1024,
               timeout: Optional[int] = None, json_mode: bool = True,
               label: str = "model") -> Optional[Dict]:
    import re
    if timeout is None:
        resolved = model
        if model in ("night_judge", "night_verify", "night_proposer", "night_reflector"):
            from lib.llm_client import resolve_model
            resolved = resolve_model(model)
        timeout = MODEL_CFG.get(resolved, {}).get("timeout", 600)
    try:
        result = call_llm(messages, model=model, max_tokens=max_tokens,
                          timeout=timeout, json_mode=json_mode, return_meta=True)
        content = result["content"]
        if isinstance(content, str):
            if not content.strip():
                raise ValueError("empty response")
            m = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', content)
            if m:
                content = m.group(1)
            content = content.strip()
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as e:
                if "Extra data" in str(e):
                    end = content.rfind("}")
                    if end > 0:
                        parsed = json.loads(content[:end + 1])
                    else:
                        raise
                else:
                    raise
        else:
            parsed = content
        return {"result": parsed, "usage": result.get("usage", {}),
                "timings": result.get("timings", {}),
                "elapsed_ms": result.get("elapsed_ms", 0),
                "model": result.get("model", model)}
    except Exception as e:
        log(f"  [error] {label} call failed: {e}")
        return None


def save_output(phase: str, data: Any) -> str:
    file_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = f"pipeline_{phase}_{file_ts}.json"
    fpath = os.path.join(EVAL_DIR, fname)
    with open(fpath, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"  [save] {fpath}")
    return fpath


def _load_latest_phase(phase_label: str, role: str) -> Dict:
    prefix = f"pipeline_{phase_label}_"
    candidates = [f for f in os.listdir(EVAL_DIR) if f.startswith(prefix) and f.endswith(".json")]
    if not candidates:
        return {}
    latest = sorted(candidates)[-1]
    fpath = os.path.join(EVAL_DIR, latest)
    log(f"  [load] {fpath}")
    with open(fpath) as f:
        return json.load(f)


# ── Phase 0: Boot ─────────────────────────────────────────────────────────

def phase_0_boot() -> Dict:
    log("\n=== Phase 0: Boot (Pod A 3B + Pod B 7B day mode) ===")
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{POD_A_PORT}/health", timeout=5) as resp:
            if resp.status == 200:
                log(f"  [pod-a] already healthy on :{POD_A_PORT}")
    except Exception:
        log("  [pod-a] starting 3B day mode...")
        with open("/opt/ai_data/scripts/current-mode-pod-a.env", "w") as f:
            f.write("MODE=day")
        r = subprocess.run(["systemctl", "--user", "start", POD_A_SERVICE],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            log(f"  [pod-a] start failed: {r.stderr.decode()[:200]}")
            return {"phase": 0, "error": r.stderr.decode()[:200]}
        t0 = time.monotonic()
        while time.monotonic() - t0 < POD_A_DAY_TIMEOUT:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{POD_A_PORT}/health", timeout=3) as resp:
                    if resp.status == 200:
                        log(f"  [pod-a] :{POD_A_PORT} ready after {time.monotonic() - t0:.0f}s")
                        break
            except Exception:
                pass
            time.sleep(3)

    if not swap_pod_b("day"):
        return {"phase": 0, "error": "Pod B day swap failed"}
    return {"phase": 0, "role": "boot", "status": "ok"}


# ── Phase 1: Python Verify ────────────────────────────────────────────────

def phase_1_python_verify(all_data: Dict) -> Dict:
    log("\n=== Phase 1: Python Verify ===")
    result = {
        "phase": 1, "role": "python_verify",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "files_loaded": all_data["meta"]["total_files"],
        "total_findings": all_data["meta"]["total_findings"],
        "findings": [],
    }
    for fname, entry in all_data["files"].items():
        result["findings"].append({
            "file": fname, "role": entry["role"],
            "finding_count": len(entry["findings"]),
            "has_verdict": bool(entry["verdict"]),
        })
        if entry.get("confidence"):
            result["findings"][-1]["confidence"] = entry["confidence"]
    save_output("01_python_verify", result)
    return result


# ── Phase 2: 7B Verify ────────────────────────────────────────────────────

def phase_2_sevenb_verify(all_data: Dict) -> Dict:
    log("\n=== Phase 2: Initial Verify (reviewer :8080) ===")
    findings = all_data.get("all_findings", [])
    log(f"  [input] {len(findings)} findings")

    # Chunk: batch=6 — optimal for ARM GEMM + cross-reference quality.
    CHUNK_SIZE = 6
    chunks = [findings[i:i+CHUNK_SIZE] for i in range(0, len(findings), CHUNK_SIZE)]
    log(f"  [chunk] {len(chunks)} chunk(s) of {CHUNK_SIZE}")

    merged = {"verification_items": [], "summary": "", "reasoning": ""}
    all_usage = {}
    total_elapsed = 0
    verdicts = []

    for ci, chunk in enumerate(chunks):
        label = f"reviewer(day_verify chunk {ci+1}/{len(chunks)})"
        msg = [
            {"role": "system", "content": VERIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": _build_findings_context(chunk, "night_initial_verify", header="Findings")},
        ]
        resp = call_model(msg, "reviewer", max_tokens=1024, label=label)
        if not resp:
            continue
        r = resp.get("result", {})
        merged["verification_items"].extend(r.get("verification_items", []))
        if r.get("final_verdict"):
            verdicts.append(r["final_verdict"])
        if r.get("summary"):
            merged["summary"] = (merged.get("summary", "") + " | " + r["summary"])[:500]
        if r.get("reasoning"):
            merged["reasoning"] = (merged.get("reasoning", "") + "\n" + r["reasoning"])[:1000]
        total_elapsed += resp.get("elapsed_ms", 0)
        for k, v in resp.get("usage", {}).items():
            all_usage[k] = all_usage.get(k, 0) + (v if isinstance(v, int) else 0)

    # Majority verdict
    if verdicts:
        counts = {}
        for v in verdicts:
            counts[v] = counts.get(v, 0) + 1
        merged["final_verdict"] = max(counts, key=counts.get)
    else:
        merged["final_verdict"] = "unknown"
    merged["confidence"] = int(len(verdicts) / max(len(chunks), 1) * 90)  # rough confidence

    result = {
        "phase": 2, "role": "seven_b_verify",
        "model": "reviewer model-Instruct", "port": REFLECTOR_PORT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "usage": all_usage, "elapsed_ms": total_elapsed,
        "chunks": len(chunks), "chunks_done": len(verdicts),
        "result": merged,
        "category_summary": _build_category_summary(merged.get("verification_items", []), len(findings)),
    }
    save_output("02_sevenb_verify", result)
    return result


# ── Phase 3: 7B-3B-7B Day PRJ ─────────────────────────────────────────────

def _day_p_item(item: Dict, cat_summary: str = "") -> List[Dict]:
    """P(7B :8080): analyze a single verification item from Phase 2."""
    check_text = str(item.get("check", item.get("description", "")))[:500]
    result_val = item.get("result", item.get("severity", "?"))
    detail = str(item.get("detail", item.get("evidence", "")))[:500]
    item_cat = item.get("category", "other")
    ctx = (f"=== VERIFICATION ITEM ===\nCheck: {check_text}\n"
           f"Result: {result_val}\nDetail: {detail}\n"
           f"Category: {item_cat}")
    if cat_summary:
        ctx = f"{cat_summary}\n\n{ctx}"
    r = call_model(
        [{"role": "system", "content": DAY_PROPOSER_SYSTEM_PROMPT},
         {"role": "user", "content": ctx}],
        "reviewer", max_tokens=2048, label="P_day")
    findings = r.get("result", {}).get("findings", []) if r else []
    return findings


def _day_r_verdicts(findings: List[Dict]) -> List[Dict]:
    """R(3B :8082): accept/reject findings from P."""
    if not findings:
        return []
    ctx = _build_findings_context(findings, "prj_reflector", header="Findings for Review")
    r = call_model(
        [{"role": "system", "content": DAY_REFLECTOR_SYSTEM_PROMPT},
         {"role": "user", "content": ctx}],
        "extractor", max_tokens=2048, label="R_day")
    return r.get("result", {}).get("verdicts", []) if r else []


def _day_j_score(findings: List[Dict], verdicts: List[Dict]) -> Optional[Dict]:
    """J(7B :8080): score and decide."""
    f_ctx = _build_findings_context(findings, "prj_judge", header="Findings")
    v_ctx = _build_findings_context(verdicts, "prj_judge", header="Verdicts",
                                    sev_key=lambda x: "high" if x.get("verdict") == "reject" else "medium")
    ctx = f"Findings ({len(findings)}):\n{f_ctx}\n\nVerdicts ({len(verdicts)}):\n{v_ctx}"
    r = call_model(
        [{"role": "system", "content": DAY_JUDGE_SYSTEM_PROMPT},
         {"role": "user", "content": ctx}],
        "reviewer", max_tokens=2048, label="J_day")
    result = r.get("result") if r else None
    if result and result.get("decision") in ("APPROVED", "REJECT"):
        return result
    return None


def phase_3_day_prj(sevenb_result: Dict, cat_summary: str = "") -> Dict:
    """Phase 3: 7B-3B-7B day cooperative debate on Phase 2 verification items.

    For each verification item from Phase 2 (7B verify):
      P(7B :8080): generate sub-findings / analysis
      R(3B :8082): accept or reject each sub-finding
      J(7B :8080): score the debate and emit a decision
    """
    log("\n=== Phase 3: 7B-3B-7B Day PRJ (on Phase 2 results) ===")

    # Extract verification items from Phase 2 output
    items = sevenb_result.get("result", {}).get("verification_items", [])
    if not items:
        # Fallback: use the top-level verification_items
        items = sevenb_result.get("result", {}).get("findings", [])
    if not items:
        items = sevenb_result.get("result", {}).get("findings", [])
    if not items:
        log("  [skip] no verification items from Phase 2")
        return {"phase": 3, "role": "day_prj", "status": "skipped",
                "reason": "no items from Phase 2"}

    log(f"  [items] {len(items)} items from Phase 2 7B verify")
    all_day_findings = []
    processed = 0
    failed = 0

    for idx, item in enumerate(items[:10]):
        cid = str(item.get("check", item.get("id", f"item{idx}")))[:20]
        log(f"\n  [{idx + 1}/{min(len(items), 10)}] item {cid}")

        findings = _day_p_item(item, cat_summary)
        if not findings:
            log("    P(7B): no findings, skip R/J")
            continue
        all_day_findings.extend(findings)

        verdicts = _day_r_verdicts(findings)
        j_result = _day_j_score(findings, verdicts)
        if j_result:
            for aid in j_result.get("approved", []):
                for f in findings:
                    if f["id"] == aid:
                        f["day_prj_verdict"] = "approved"
            for rid in j_result.get("rejected", []):
                for f in findings:
                    if f["id"] == rid:
                        f["day_prj_verdict"] = "rejected"
            processed += 1
        else:
            failed += 1

    log(f"\n  [done] {processed} processed, {failed} failed, {len(all_day_findings)} findings")
    result = {
        "phase": 3, "role": "day_prj_7b3b7b",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "items_from_phase2": len(items),
        "processed": processed, "failed": failed,
        "total_findings": len(all_day_findings),
        "findings": all_day_findings[:200],
    }
    save_output("03_day_prj", result)
    return result


# ── Phase 4: Night P-R-J ──────────────────────────────────────────────────

def _night_phase_p(all_data: Dict, rubric_data: Optional[List] = None,
                   verification_items: Optional[List] = None,
                   group_by_category: bool = False) -> Dict:
    log("\n  --- Night P: 30B Proposer (review-p) ---")
    if not swap_pod_b("review-p"):
        return {"error": "Pod B review-p swap failed"}
    # Phase 2 verification_items = SSOT when available (already categorised by 7B verify)
    source = verification_items or all_data.get("all_findings", [])
    total_src = len(source)
    log(f"  [input] {total_src} items {'(verification_items)' if verification_items else '(all_findings)'}")

    if group_by_category and verification_items:
        cat_chunks: Dict[str, list] = {}
        for item in verification_items:
            cat = item.get("category", "other")
            cat_chunks.setdefault(cat, []).append(item)
        chunks = list(cat_chunks.values())
        log(f"  [input] -> {len(chunks)} category group(s): "
            f"{', '.join(f'{k}={len(v)}' for k, v in sorted(cat_chunks.items()))}")
        header_fmt = lambda ci: f"Category: {list(cat_chunks.keys())[ci]}"
    else:
        cfg = MODEL_CFG.get("proposer", {})
        chunk_size = cfg.get("chunk_size", 10)
        chunks = [source[i:i+chunk_size] for i in range(0, len(source), chunk_size)]
        log(f"  [input] -> {len(chunks)} chunk(s) of {chunk_size}")
        header_fmt = lambda ci: "Findings"

    extra_sections = []
    if verification_items:
        cat_summary = _build_category_summary(verification_items, total_src)
        if cat_summary:
            extra_sections.append(("Global Category Distribution", cat_summary, 10))
    if rubric_data:
        rubric_lines = []
        for r in rubric_data[:20]:
            rid = r.get("id", "?")
            ws = r.get("weighted_score", 0)
            rubric_lines.append(f"    {rid}: weighted_score={ws:.2f} "
                                f"(C={r.get('correctness',0)} A={r.get('actionability',0)} "
                                f"E={r.get('evidence',0)} N={r.get('novelty',0)})")
        rubric_text = "\n".join(rubric_lines)
        extra_sections.append(("Rubric Scores", rubric_text, 8))
        hist = _load_historical_verify_context()
        if hist:
            extra_sections.append(("Previous Verify Results", hist, 7))

    all_proposals = []
    total_usage = {}
    total_elapsed = 0
    for ci, chunk in enumerate(chunks):
        label = f"proposer(proposer chunk {ci+1}/{len(chunks)})"
        ctx = _build_findings_context(chunk, "night_proposer", header=header_fmt(ci),
                                      extra=extra_sections)
        msg = [
            {"role": "system", "content": SYSTEM_PROPOSER},
            {"role": "user", "content": ctx},
        ]
        resp = call_model(msg, "proposer", max_tokens=2048, label=label)
        if not resp:
            continue
        for p in resp.get("result", {}).get("findings", []):
            p["_chunk"] = ci
            all_proposals.append(p)
        total_elapsed += resp.get("elapsed_ms", 0)
        for k, v in resp.get("usage", {}).items():
            total_usage[k] = total_usage.get(k, 0) + (v if isinstance(v, int) else 0)

    # Dedup by description
    seen = set()
    deduped = []
    for p in all_proposals:
        sig = p.get("description", "")[:100]
        if sig not in seen:
            seen.add(sig)
            deduped.append(p)
    log(f"  [result] {len(all_proposals)} raw -> {len(deduped)} deduped proposals")
    return {"proposals": deduped, "usage": total_usage,
            "elapsed_ms": total_elapsed,
            "status": "ok", "chunks": len(chunks), "chunks_done": len(chunks),
            "model": "Qwen3-Coder-30B-A3B-Q4_K_S"}


def _night_phase_r() -> Dict:
    log("\n  --- Night R: 14B Refuter (review-r) ---")
    proposer = _load_latest_phase("04_night_prj_sub_p", "night_proposer") or {}
    proposals = proposer.get("proposals", [])
    if not swap_pod_b("review-r"):
        return {"error": "Pod B review-r swap failed"}
    all_verdicts = []
    total_usage = {}
    total_elapsed = 0
    for f in proposals:
        msg = [
            {"role": "system", "content": SYSTEM_REFUTER},
            {"role": "user", "content": f"## Finding\n{json.dumps(f, ensure_ascii=False, indent=2)}"},
        ]
        resp = call_model(msg, "reflector", label=f"reflector(refuter {f.get('id','?')})")
        if not resp:
            continue
        for v in resp.get("result", {}).get("verdicts", []):
            all_verdicts.append(v)
        total_elapsed += resp.get("elapsed_ms", 0)
        for k, v in resp.get("usage", {}).items():
            total_usage[k] = total_usage.get(k, 0) + (v if isinstance(v, int) else 0)
    log(f"  [merge] {len(all_verdicts)} verdicts")
    return {"verdicts": all_verdicts, "usage": total_usage, "elapsed_ms": total_elapsed, "status": "ok"}


def _night_phase_j() -> Dict:
    log("\n  --- Night J: Judge (review-j) ---")
    refuter = _load_latest_phase("04_night_prj_sub_r", "night_refuter") or {}
    verdicts = refuter.get("verdicts", [])
    if not swap_pod_b("review-j"):
        return {"error": "Pod B review-j swap failed"}
    log(f"  [input] {len(verdicts)} verdicts (TokenBudget allocation)")
    ctx = _build_findings_context(verdicts, "night_judge", header="Verdicts",
                                  sev_key=lambda x: "high" if x.get("verdict") == "reject" else "medium")
    msg = [
        {"role": "system", "content": SYSTEM_JUDGE},
        {"role": "user", "content": ctx},
    ]
    resp = call_model(msg, "judge", max_tokens=1024, label="judge(judge all)")
    r = (resp or {}).get("result", {})
    p_score = sum(r.get("rubric_evaluation", {}).get("finder", {}).get(k, 0) for k in ("correctness", "coverage", "precision"))
    r_score = sum(r.get("rubric_evaluation", {}).get("reflector", {}).get(k, 0) for k in ("accuracy", "efficiency", "completeness"))
    log(f"  [result] P={p_score} R={r_score} -> {r.get('decision','?')}")
    return {"result": r, "usage": (resp or {}).get("usage", {}),
            "elapsed_ms": (resp or {}).get("elapsed_ms", 0), "status": "ok"}


def phase_4_night_prj(all_data: Dict, rubric_data: Optional[List] = None,
                      verification_items: Optional[List] = None,
                      group_by_category: bool = False) -> Dict:
    log("\n=== Phase 4: Night P-R-J ===")
    log("  [sequence] review-p(proposer) -> review-r(reflector) -> review-j(judge)")
    if rubric_data:
        log(f"  [rubric] {len(rubric_data)} evaluations fed into P context")
    if group_by_category:
        cats = set(v.get("category", "other") for v in (verification_items or []))
        log(f"  [category-group] {len(cats)} categories: {sorted(cats)}")
    p_result = _night_phase_p(all_data, rubric_data, verification_items, group_by_category)
    if "error" in p_result:
        return {"phase": 4, "error": f"Night P failed: {p_result['error']}"}
    save_output("04_night_prj_sub_p", p_result)
    r_result = _night_phase_r()
    if "error" in r_result:
        return {"phase": 4, "error": f"Night R failed: {r_result['error']}"}
    save_output("04_night_prj_sub_r", r_result)
    j_result = _night_phase_j()
    if "error" in j_result:
        return {"phase": 4, "error": f"Night J failed: {j_result['error']}"}
    result = {
        "phase": 4, "role": "night_prj",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "p": {"model": "Qwen3-Coder-30B-A3B-Q4_K_S", "proposals": len(p_result.get("proposals", [])),
              "usage": p_result.get("usage", {}), "elapsed_ms": p_result.get("elapsed_ms", 0)},
        "r": {"model": "Qwen2.5-Coder-14B-Instruct", "verdicts": len(r_result.get("verdicts", [])),
              "usage": r_result.get("usage", {}), "elapsed_ms": r_result.get("elapsed_ms", 0)},
        "j": {"model": "NextCoder-14B-Q4_K_M", "result": j_result.get("result", {}),
              "usage": j_result.get("usage", {}), "elapsed_ms": j_result.get("elapsed_ms", 0),
              "chunks": j_result.get("chunks", 0), "chunks_done": j_result.get("chunks_done", 0)},
        "result": j_result.get("result", {}),
    }
    save_output("04_night_prj", result)
    return result


# ── Phase 5: 27B Verify ───────────────────────────────────────────────────

def phase_5_verify(all_data: Dict, use_feedback: bool = False) -> Dict:
    log("\n=== Phase 5: 27B IQ4_XS Verify (:8081) ===")
    if use_feedback:
        log("  [feedback] using FEEDBACK_VERIFIER_SYSTEM_PROMPT (per-model feedback enabled)")
    if not swap_pod_b("verify"):
        return {"phase": 5, "error": "Pod B verify failed to load"}
    findings = all_data.get("all_findings", [])
    log(f"  [input] {len(findings)} findings (TokenBudget + chunking)")

    CHUNK_SIZE = 6
    chunks = [findings[i:i+CHUNK_SIZE] for i in range(0, len(findings), CHUNK_SIZE)]
    log(f"  [chunk] {len(chunks)} chunk(s) of {CHUNK_SIZE}")

    merged = {"verification_items": [], "summary": "", "reasoning": ""}
    all_usage = {}
    total_elapsed = 0
    verdicts = []

    for ci, chunk in enumerate(chunks):
        label = f"27B(verify chunk {ci+1}/{len(chunks)})"
        ctx = _build_findings_context(chunk, "night_final_verify", header="Findings")
        sys_prompt = FEEDBACK_VERIFIER_SYSTEM_PROMPT if use_feedback else SYSTEM_VERIFY
        msg = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": ctx},
        ]
        resp = call_model(msg, "verifier", max_tokens=1024, label=label)
        if not resp:
            continue
        r = resp.get("result", {})
        merged["verification_items"].extend(r.get("verification_items", []))
        if r.get("final_verdict"):
            verdicts.append(r["final_verdict"])
        if r.get("summary"):
            merged["summary"] = (merged.get("summary", "") + " | " + r["summary"])[:500]
        if r.get("reasoning"):
            merged["reasoning"] = (merged.get("reasoning", "") + "\n" + r["reasoning"])[:1000]
        total_elapsed += resp.get("elapsed_ms", 0)
        for k, v in resp.get("usage", {}).items():
            all_usage[k] = all_usage.get(k, 0) + (v if isinstance(v, int) else 0)

    # Majority verdict
    if verdicts:
        counts = {}
        for v in verdicts:
            counts[v] = counts.get(v, 0) + 1
        merged["final_verdict"] = max(counts, key=counts.get)
    else:
        merged["final_verdict"] = "unknown"
    merged["confidence"] = int(len(verdicts) / max(len(chunks), 1) * 90)

    log(f"  [result] verdict={merged.get('final_verdict','?')} conf={merged.get('confidence','?')}")
    result = {
        "phase": 5, "role": "verify_27b_iq4xs",
        "model": "Qwen3.6-27B-IQ4_XS", "port": VERIFY_PORT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "usage": all_usage, "elapsed_ms": total_elapsed,
        "chunks": len(chunks), "chunks_done": len(verdicts),
        "result": merged, "config": {"use_feedback": use_feedback},
    }
    save_output("05_verify", result)
    return result


# ── Phase 6: Feedback ─────────────────────────────────────────────────────

def phase_6_feedback(all_data: Dict, results: Dict,
                     use_rubric: bool = False, use_feedback: bool = False) -> Dict:
    log("\n=== Phase 6: Consolidated Feedback ===")
    total_phases = sum(1 for v in results.values() if v and "error" not in v)
    total_errors = sum(1 for v in results.values() if v and "error" in v)
    total_elapsed = sum(v.get("elapsed_ms", 0) for v in results.values() if v and "elapsed_ms" in v)
    total_prompt = sum(v.get("usage", {}).get("prompt_tokens", 0) for v in results.values() if v)
    total_gen = sum(v.get("usage", {}).get("completion_tokens", 0) for v in results.values() if v)

    # Rubric summary if available
    rubric_summary = None
    phase2 = results.get("phase2", {})
    if use_rubric and phase2.get("rubric_evaluations"):
        evals = phase2["rubric_evaluations"]
        scores = [r.get("weighted_score", 0) for r in evals if r.get("weighted_score") is not None]
        if scores:
            rubric_summary = {
                "count": len(scores),
                "avg_weighted_score": round(sum(scores) / len(scores), 2),
                "min_score": round(min(scores), 2),
                "max_score": round(max(scores), 2),
            }

    result = {
        "phase": 6, "role": "consolidated_feedback",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {"use_rubric": use_rubric, "use_feedback": use_feedback},
        "execution_summary": {
            "total_phases_completed": total_phases, "total_errors": total_errors,
            "total_elapsed_ms": total_elapsed,
            "total_prompt_tokens": total_prompt, "total_gen_tokens": total_gen,
            "total_tokens": total_prompt + total_gen,
        },
        "phase_results": {},
    }
    if rubric_summary:
        result["rubric_summary"] = rubric_summary
    for pk, pv in results.items():
        if pv and "error" not in pv:
            result["phase_results"][pk] = {
                "role": pv.get("role", ""), "model": pv.get("model", ""),
                "elapsed_ms": pv.get("elapsed_ms", 0), "usage": pv.get("usage", {}),
                "result_preview": str(pv.get("result", {}))[:300],
            }
    save_output("06_feedback", result)
    return result


# ── Phase 7: Restore Day ──────────────────────────────────────────────────

def phase_7_restore_day() -> Dict:
    log("\n=== Phase 7: Restore Day Mode ===")
    import urllib.request
    if not swap_pod_b("day"):
        log("  [warn] Pod B day mode restore failed")
        return {"phase": 7, "error": "Pod B day restore failed"}
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{POD_A_PORT}/health", timeout=5) as resp:
            if resp.status == 200:
                log(f"  [pod-a] healthy on :{POD_A_PORT}")
                return {"phase": 7, "role": "restore_day", "status": "ok"}
    except Exception:
        pass
    log("  [pod-a] starting...")
    r = subprocess.run(["systemctl", "--user", "start", POD_A_SERVICE], capture_output=True, timeout=60)
    if r.returncode != 0:
        return {"phase": 7, "role": "restore_day", "error": r.stderr.decode()[:200]}
    t0 = time.monotonic()
    while time.monotonic() - t0 < POD_A_DAY_TIMEOUT:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{POD_A_PORT}/health", timeout=3) as resp:
                if resp.status == 200:
                    log(f"  [pod-a] :{POD_A_PORT} ready after {time.monotonic() - t0:.0f}s")
                    return {"phase": 7, "role": "restore_day", "status": "ok"}
        except Exception:
            pass
        time.sleep(3)
    return {"phase": 7, "role": "restore_day", "error": "timeout"}


# ── Orchestrator ──────────────────────────────────────────────────────────

def run_pipeline(phases: Optional[List[int]] = None,
                 use_rubric: bool = False,
                 use_feedback: bool = False,
                 group_by_category: bool = False) -> Dict[str, Any]:
    t_start = time.monotonic()
    rubric_str = f" rubric={'on' if use_rubric else 'off'}"
    fb_str = f" feedback={'on' if use_feedback else 'off'}"
    cat_str = f" cat-group={'on' if group_by_category else 'off'}"
    log("=" * 60)
    log(f"DevForge Night Pipeline v3.0 — Full 6-stage pipeline{rubric_str}{fb_str}{cat_str}")
    log("extract -> py verify -> 7B verify -> day prj -> night prj -> verify")
    log("=" * 60)
    if phases is None:
        phases = [0, 1, 2, 3, 4, 5, 6, 7]
    # Build port set for preflight
    need_ports = set()
    if 0 in phases or 2 in phases or 3 in phases or 7 in phases:
        need_ports.add(8080)
    if 0 in phases or 3 in phases or 7 in phases:
        need_ports.add(POD_A_PORT)
    if 4 in phases or 5 in phases:
        need_ports.add(8080)
    if 5 in phases:
        need_ports.add(VERIFY_PORT)
    preflight_checks("night.py", required_ports=need_ports)
    log("Loading eval data files...")
    all_data = load_eval_data()
    log(f"  Loaded {all_data['meta']['total_files']} files, {all_data['meta']['total_findings']} findings")

    results: Dict[str, Any] = {}
    rubric_data = []
    cat_summary = ""

    if 0 in phases:
        boot = phase_0_boot()
        results["phase0"] = boot
        if "error" in boot:
            return results
    if 1 in phases:
        results["phase1"] = phase_1_python_verify(all_data)
    if 2 in phases:
        phase2_result = phase_2_sevenb_verify(all_data)
        results["phase2"] = phase2_result

        # Rubric evaluation: after 7B verify, before P-R-J
        if use_rubric:
            items = all_data.get("all_findings", [])
            if not items and phase2_result.get("result", {}).get("verification_items"):
                items = phase2_result["result"]["verification_items"]
            rubric_data = rubric_evaluate_findings(items, "night")
            phase2_result["rubric_evaluations"] = rubric_data
            results["phase2"] = phase2_result

        # Build global category summary from Phase 2 output
        phase2 = results.get("phase2", {})
        verify_items = (phase2.get("result") or {}).get("verification_items") or []
        cat_summary = _build_category_summary(verify_items, all_data["meta"]["total_findings"])
        if cat_summary:
            log(f"  [global-cat] {cat_summary}")
    else:
        # Phase 2 not in this run — restore cat_summary from saved Phase 2 output
        saved_phase2 = _load_latest_phase("02_sevenb_verify", "seven_b_verify")
        if saved_phase2:
            cat_summary = saved_phase2.get("category_summary", "")
        if not cat_summary:
            verify_items = (saved_phase2.get("result") or {}).get("verification_items") or []
            cat_summary = _build_category_summary(verify_items, all_data["meta"]["total_findings"])
        if cat_summary:
            log(f"  [global-cat] {cat_summary} (restored from Phase 2 output)")

    if 3 in phases:
        phase2 = results.get("phase2", {})
        if "error" in phase2 or not phase2.get("result"):
            phase2 = _load_latest_phase("02_sevenb_verify", "seven_b_verify")
        results["phase3"] = phase_3_day_prj(phase2, cat_summary)
    if 4 in phases:
        phase2 = results.get("phase2", {})
        if "error" in phase2 or not phase2.get("result"):
            phase2 = _load_latest_phase("02_sevenb_verify", "seven_b_verify")
        verify_items = (phase2.get("result") or {}).get("verification_items") or []
        if group_by_category and not verify_items:
            log("  [warn] Phase 2 verification_items empty, falling back to plain chunking")
        results["phase4"] = phase_4_night_prj(all_data, rubric_data if use_rubric else None,
                                                verify_items or None, group_by_category)
    if 5 in phases:
        results["phase5"] = phase_5_verify(all_data, use_feedback=use_feedback)
    if 6 in phases:
        results["phase6"] = phase_6_feedback(all_data, results,
                                             use_rubric=use_rubric, use_feedback=use_feedback)
    if 7 in phases:
        results["phase7"] = phase_7_restore_day()
    total = round(time.monotonic() - t_start, 1)
    log("\n" + "=" * 60)
    log(f"Pipeline complete in {total}s")
    save_output("complete", {"total_elapsed_s": total, "phases_run": phases, "results": results,
                             "config": {"use_rubric": use_rubric, "use_feedback": use_feedback}})
    return results


def main():
    import argparse
    ap = argparse.ArgumentParser(description="DevForge Night Pipeline v3.0")
    ap.add_argument("--all", action="store_true", help="Run full pipeline")
    ap.add_argument("--phases", type=int, choices=[0, 1, 2, 3, 4, 5, 6, 7], nargs="+", help="Phase(s) to run (e.g. --phases 2 4)")
    ap.add_argument("--rubric", action="store_true", help="Enable rubric evaluation after Phase 2")
    ap.add_argument("--feedback", action="store_true", help="Enable per-model feedback in Phase 5")
    ap.add_argument("--group-category", action="store_true", help="Group Phase 2 items by category for 30B chunking")
    ap.add_argument("--dry-run", action="store_true", help="Preflight + data load only, no LLM calls")
    args = ap.parse_args()
    if args.dry_run:
        phases = args.phases or [0, 1, 2, 3, 4, 5, 6, 7]
        need_ports = set()
        if any(p in phases for p in [0, 2, 3, 7]):
            need_ports.add(8080)
        if any(p in phases for p in [0, 3, 7]):
            need_ports.add(POD_A_PORT)
        if 4 in phases:
            need_ports.add(8080)
        if 5 in phases:
            need_ports.add(8080)
            need_ports.add(VERIFY_PORT)
        preflight_checks("night.py", required_ports=need_ports)
        all_data = load_eval_data()
        log(f"  [dry-run] Loaded {all_data['meta']['total_files']} files, {all_data['meta']['total_findings']} findings")
        log("  [dry-run] Dry run complete — no LLM calls made")
        return
    if args.phases:
        run_pipeline(phases=args.phases, use_rubric=args.rubric, use_feedback=args.feedback,
                     group_by_category=args.group_category)
    else:
        run_pipeline(use_rubric=args.rubric, use_feedback=args.feedback,
                     group_by_category=args.group_category)
    sys.exit(0)


if __name__ == "__main__":
    main()
