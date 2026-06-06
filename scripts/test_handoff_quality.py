#!/usr/bin/env python3
"""test_handoff_quality.py v2 — P/R/J 핸드오프 문서 품질 비교 (경량).

접근법: 기존 파이프라인 데이터로 각 모델의 handoff 문서를 구성하고
27B로 평가. P와 R은 LLM 재호출 없이 확정적(deterministic)으로 구성.

실행:
  python3 test_handoff_quality.py          # 순차 실행
  python3 test_handoff_quality.py --eval   # 저장된 결과 재평가

출력: data/experiment/handoff_comparison/
"""

import json
import os
import sys
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
EXPER_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "experiment")
OUT_DIR = os.path.join(EXPER_DIR, "handoff_comparison")
os.makedirs(OUT_DIR, exist_ok=True)
sys.path.insert(0, SCRIPTS_DIR)

from lib.llm_client import call_llm, MODEL_REGISTRY
from lib.llm.json_parser import parse_llm_json

TIMEOUT = 600


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# ── Load pipeline data ──────────────────────────────────────────────

def load_data():
    """Load P findings, R verdicts, J handoff from existing outputs."""
    state = json.load(open(os.path.join(EXPER_DIR, "pipeline_state_r1_norubric.json")))

    p = json.load(open(os.path.join(EXPER_DIR, "exp_p_rot1_r1_norubric_r1_norubric.json")))
    r = json.load(open(os.path.join(EXPER_DIR, "exp_r_rot1_r1_norubric_r1_norubric.json")))
    j = json.load(open(os.path.join(EXPER_DIR, "exp_j_rot1_r1_norubric_r1_norubric.json")))

    inp = state.get("input", {})
    findings = inp.get("findings", [])
    p_findings = (p.get("result") or p).get("findings", [])
    r_verdicts = (r.get("result") or r).get("verdicts", [])
    j_res = j.get("result", {})

    return findings, p_findings, r_verdicts, j_res


# ── Build handoffs for each model ───────────────────────────────────

def build_p_handoff(p_findings, r_verdicts):
    """Construct P's handoff from its findings + R's accept/reject."""
    r_map = {v["id"]: v for v in r_verdicts}
    approved = []
    rejected = []
    for pf in p_findings:
        fid = pf["id"]
        rv = r_map.get(fid, {})
        verdict = rv.get("verdict", "?")
        entry = {"id": fid, "severity": pf.get("severity", "?"),
                 "category": pf.get("category", "?"),
                 "finding": pf.get("description", "")[:150],
                 "rationale": rv.get("reason", "")[:100]}
        if verdict == "accept":
            approved.append(entry)
        else:
            rejected.append(entry)

    return {
        "handoff": {
            "source": "P(Qwen30B)+R(Qwen14B)",
            "method": "deterministic_from_pipeline",
            "total_proposed": len(p_findings),
            "approved": approved,
            "rejected": rejected,
            "critical_remaining": [e["id"] for e in rejected if e["severity"] in ("critical", "high")],
            "unresolved_count": len(rejected),
        }
    }


def build_j_handoff(j_res):
    """J's LLM-generated handoff — already in proper format."""
    return {"handoff": j_res.get("handoff", {}),
            "source": "J(SeleneMini Q8)", "method": "llm_generated"}


# ── Evaluation prompt ───────────────────────────────────────────────

EVAL_SYSTEM = """You are a handoff quality judge. Three handoff documents are below.
Each is intended for a final verifier who needs to approve/reject code review findings.

Score each on 4 criteria (1-10):

1. completeness — includes ALL needed info (which items approved/rejected, critical items,
   verifier priorities)? 10 = exhaustive, 1 = missing half the data
2. accuracy — are finding IDs, severities, and decisions correct? 10 = perfect, 1 = errors
3. clarity — is it well-structured and easy to scan? 10 = excellent layout, 1 = confusing
4. verifier_usefulness — if YOU were the verifier, does this help you decide?
   10 = immediately actionable, 1 = requires cross-referencing source data

CRITICAL: Judge each document on its OWN merits. If one document is missing info
that another has, score it lower.

Return ONLY valid JSON — no markdown.
Schema:
{
  "evaluations": [
    {"model": "P(30B)", "scores": {"completeness": 1-10, "accuracy": 1-10, "clarity": 1-10, "verifier_usefulness": 1-10},
     "strengths": ["..."], "weaknesses": ["..."]}
  ],
  "overall_winner": "P(30B)|R(14B)|J(Selene)",
  "ranking": ["1st", "2nd", "3rd"],
  "verdict": "The best handoff for the verifier step is from [model] because ..."
}"""


# ── Helper: switch Pod B mode ───────────────────────────────────────

def start_pod_b(mode, port):
    import subprocess
    log(f"  POD B -> {mode} (:{port})")
    with open("/opt/ai_data/scripts/current-mode-pod-b.env", "w") as f:
        f.write(f"MODE={mode}")
    # Stop existing
    subprocess.run(["systemctl", "--user", "stop", "container-devforge-swap.service"],
                   capture_output=True, timeout=30)
    subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-swap.service"],
                   capture_output=True, timeout=10)
    time.sleep(3)
    subprocess.run(["systemctl", "--user", "start", "container-devforge-swap.service"],
                   capture_output=True, timeout=60)
    # Wait for health
    t0 = time.monotonic()
    while time.monotonic() - t0 < 600:
        try:
            req = __import__("urllib.request").request.Request(f"http://127.0.0.1:{port}/health")
            with __import__("urllib.request").request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    log(f"  :{port} ready ({time.monotonic()-t0:.0f}s)")
                    time.sleep(3)
                    return True
        except Exception:
            pass
        time.sleep(5)
    log(f"  :{port} TIMEOUT")
    return False


# ── Main ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log("=" * 60)
    log("HANDOFF QUALITY COMPARISON v2 (경량)")
    log("=" * 60)

    # 1. Load data
    findings, p_findings, r_verdicts, j_res = load_data()
    log(f"  Loaded: {len(findings)} total findings, {len(p_findings)} P findings, "
        f"{len(r_verdicts)} R verdicts")

    # 2. Build handoffs
    log("\n[1] Building handoffs from existing data...")
    p_handoff = build_p_handoff(p_findings, r_verdicts)
    j_handoff = build_j_handoff(j_res)
    # R: same as P since R produces verdicts (not stand-alone findings)
    # R's handoff = P handoff filtered + R verdicts as separate section
    r_handoff = {
        "handoff": {
            "source": "R(Qwen14B)",
            "method": "deterministic_from_pipeline",
            "verdicts": [{"id": v["id"], "verdict": v["verdict"], "reason": v.get("reason", "")[:100]}
                         for v in r_verdicts],
            "accept_count": len([v for v in r_verdicts if v.get("verdict") == "accept"]),
            "reject_count": len([v for v in r_verdicts if v.get("verdict") == "reject"]),
        }
    }

    for name, ho in [("P", p_handoff), ("R", r_handoff), ("J", j_handoff)]:
        path = os.path.join(OUT_DIR, f"handoff_{name}.json")
        with open(path, "w") as f:
            json.dump(ho, f, ensure_ascii=False, indent=2)
        log(f"  Saved {path}")

    # 3. Evaluate (if "--eval" not passed, use saved data; always requires 27B)
    log("\n[2] Evaluating handoffs with 27B...")

    eval_input = "Below are 3 handoff documents for a final verifier.\n\n"
    for name, ho in [("P(30B)", p_handoff), ("R(14B)", r_handoff), ("J(Selene)", j_handoff)]:
        eval_input += f"### {name} handoff:\n{json.dumps(ho, ensure_ascii=False, indent=2)}\n\n"

    eval_messages = [
        {"role": "system", "content": EVAL_SYSTEM},
        {"role": "user", "content": eval_input},
    ]

    do_eval = "--eval" not in sys.argv
    eval_result = None

    if do_eval:
        port = MODEL_REGISTRY.get("Qwen27B", {}).get("port", 8081)
        ok = start_pod_b("verify", port)
        if ok:
            try:
                log("  Calling 27B evaluator...")
                resp = call_llm(eval_messages, model="Qwen27B",
                                max_tokens=1024, timeout=TIMEOUT,
                                json_mode=True, return_meta=True)
                content = resp["content"] if isinstance(resp, dict) else resp
                eval_result = parse_llm_json(content)
                log("  Evaluation successful")
            except Exception as e:
                log(f"  27B failed: {e}")

        # Fallback to 30B if 27B fails
        if not eval_result:
            port = MODEL_REGISTRY.get("Qwen30B", {}).get("port", 8080)
            ok = start_pod_b("review-p", port)
            if ok:
                try:
                    log("  Falling back to 30B evaluator...")
                    resp = call_llm(eval_messages, model="Qwen30B",
                                    max_tokens=1024, timeout=TIMEOUT,
                                    json_mode=True, return_meta=True)
                    content = resp["content"] if isinstance(resp, dict) else resp
                    eval_result = parse_llm_json(content)
                except Exception as e:
                    log(f"  30B also failed: {e}")
    else:
        # Load saved evaluation
        path = os.path.join(OUT_DIR, "evaluation.json")
        if os.path.exists(path):
            with open(path) as f:
                eval_result = json.load(f)
            log("  Loaded saved evaluation")

    if eval_result:
        path = os.path.join(OUT_DIR, "evaluation.json")
        with open(path, "w") as f:
            json.dump(eval_result, f, ensure_ascii=False, indent=2)
        log(f"  Saved: {path}")

        # Summary
        log("\n" + "=" * 60)
        log("RESULTS")
        log("=" * 60)
        log(f"  Winner: {eval_result.get('overall_winner', '?')}")
        log(f"  Ranking: {eval_result.get('ranking', [])}")
        for ev in eval_result.get("evaluations", []):
            sc = ev.get("scores", {})
            log(f"\n  {ev.get('model', '?')}:")
            log(f"    C={sc.get('completeness','?')} A={sc.get('accuracy','?')}"
                f" Cl={sc.get('clarity','?')} U={sc.get('verifier_usefulness','?')}")
            for s in ev.get("strengths", []):
                log(f"    + {s}")
            for w in ev.get("weaknesses", []):
                log(f"    - {w}")
        if eval_result.get("verdict"):
            log(f"\n  {eval_result['verdict'][:300]}")
    else:
        log("ERROR: No evaluation result")

    log(f"\nResults: {OUT_DIR}/")
