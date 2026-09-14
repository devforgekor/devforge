#!/usr/bin/env python3
# Status: experimental
# Path: tests/test_experiment.py — pytest (통합)
"""Experiment tests: DART round, experiment runner verification.

Usage:  python3 tests/test_experiment.py
"""

import json
import re
import time
import urllib.request

SWITCH_FILE = "/opt/ai_data/debate/switch/model-switch.json"
HEALTH_URL = "http://127.0.0.1:8081/health"
LLM_URL = "http://127.0.0.1:8081/v1/chat/completions"

MODELS = {
    "qwen25": {"filename": "qwen2.5-coder-14b-instruct-q4_k_m.gguf", "ctx": 4096, "threads": 4, "mlock": 0,
               "max_tokens": 256, "temperature": 0.1, "system_prompt": True},
    "deepcoder": {"filename": "agentica-org_DeepCoder-14B-Preview-Q4_K_M.gguf", "ctx": 4096, "threads": 4, "mlock": 1,
                  "max_tokens": 256, "temperature": 0.6, "top_p": 0.95, "system_prompt": False},
    "phi4": {"filename": "phi-4-Q4_K_M.gguf", "ctx": 4096, "threads": 4, "mlock": 1,
             "max_tokens": 256, "temperature": 0.1, "system_prompt": True},
}


def log(msg):
    print(msg, flush=True)

def switch_and_wait(model_id, cfg):
    data = {"model_file": cfg["filename"], "port": 8081, "ctx": cfg["ctx"],
            "threads": cfg["threads"], "mlock": cfg["mlock"]}
    with open(SWITCH_FILE, "w") as f:
        json.dump(data, f, indent=2)
    log(f"  Switching to {model_id} ({cfg['filename']})...")
    time.sleep(5)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 420:
        try:
            req = urllib.request.Request(HEALTH_URL)
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200 and json.loads(resp.read()).get("status") == "ok":
                    log(f"  Ready in {time.monotonic()-t0:.0f}s")
                    return True
        except Exception:
            pass
        time.sleep(3)
    log("  TIMEOUT")
    return False

def call_llm(model_id, cfg, messages, label=""):
    body = {"messages": messages, "temperature": cfg["temperature"], "max_tokens": cfg["max_tokens"]}
    if "top_p" in cfg:
        body["top_p"] = cfg["top_p"]
    log(f"  [{label}] Calling {model_id}...")
    t0 = time.monotonic()
    req = urllib.request.Request(LLM_URL, data=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        result = json.loads(resp.read())
    elapsed = time.monotonic() - t0
    content = result["choices"][0]["message"]["content"]
    log(f"  [{label}] {elapsed:.0f}s, {len(content)} chars, {result['usage']['completion_tokens']} tokens")
    return content

def parse_json(raw):
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        pass
    for m in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", raw, re.DOTALL):
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
    return None


def test_dart_round():
    """Single DART round: Proposer → Refuter → Judge."""
    question = "How should we handle database connection pooling in a FastAPI async application?"
    log("=" * 60)
    log("DART Round: P-R-J live test")
    log(f"Q: {question}")
    log("=" * 60)
    total_start = time.monotonic()

    # A: Proposer (Qwen2.5-Coder-14B)
    log("\n-- A: Proposer (Qwen2.5-14B) --")
    if not switch_and_wait("qwen25", MODELS["qwen25"]):
        return False
    proposer_raw = call_llm("qwen25", MODELS["qwen25"], [
        {"role": "system", "content": "You are a solution PROPOSER. Respond with STRICT JSON only."},
        {"role": "user", "content": f"Topic: {question}\n\nPropose a concrete solution with code.\nOutput JSON: {{\"logic_summary\":\"...\",\"code_snippet\":\"...\",\"confidence_score\":0-100}}"}
    ], "PROPOSER")
    proposer_json = parse_json(proposer_raw)
    log(f"  Proposer JSON: {proposer_json is not None}")

    # B: Refuter (DeepCoder-14B)
    log("\n-- B: Refuter (DeepCoder-14B) --")
    if not switch_and_wait("deepcoder", MODELS["deepcoder"]):
        return False
    refuter_raw = call_llm("deepcoder", MODELS["deepcoder"], [
        {"role": "user", "content": f"[ROLE: CRITICAL REFUTER]\nTopic: {question}\nProposer: {proposer_raw}\nFind weaknesses, propose alternatives.\nOutput JSON: {{\"logic_summary\":\"...\",\"code_snippet\":\"...\",\"confidence_score\":0-100}}"}
    ], "REFUTER")

    # C: Judge (Phi-4-14B)
    log("\n-- C: Judge (Phi-4-14B) --")
    if not switch_and_wait("phi4", MODELS["phi4"]):
        return False

    import random
    if random.random() < 0.5:
        alpha_label, beta_label = "qwen25", "deepcoder"
        alpha_raw, beta_raw = proposer_raw, refuter_raw
    else:
        alpha_label, beta_label = "deepcoder", "qwen25"
        alpha_raw, beta_raw = refuter_raw, proposer_raw
    log(f"  Alpha={alpha_label}, Beta={beta_label}")

    judge_raw = call_llm("phi4", MODELS["phi4"], [
        {"role": "system", "content": "You are a 3-person jury (security, perf, readability). Judge ANONYMIZED drafts."},
        {"role": "user", "content": f"Topic: {question}\nAlpha:\n{alpha_raw[:2000]}\nBeta:\n{beta_raw[:2000]}\nOutput JSON: {{\"consensus_score\":0-100,\"winner\":\"alpha|beta|tie\"}}"}
    ], "JUDGE")
    judge_json = parse_json(judge_raw)

    total_elapsed = time.monotonic() - total_start
    log(f"\n{'='*60}")
    log(f"DART complete: {total_elapsed/60:.1f} min")
    if judge_json:
        log(f"Consensus: {judge_json.get('consensus_score')}%")
        winner = judge_json.get("winner", "?")
        log(f"Winner: {winner} -> {alpha_label if winner == 'alpha' else (beta_label if winner == 'beta' else 'tie')}")
    return True


if __name__ == "__main__":
    test_dart_round()
