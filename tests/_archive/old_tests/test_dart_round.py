#!/usr/bin/env python3
# Status: experimental
# Path: tests/test_dart_round.py — pytest
"""Single DART round live test — Proposer → Refuter → Judge."""
import json
import os
import random
import re
import sys
import time
import urllib.request

SWITCH_FILE = "/opt/ai_data/debate/switch/model-switch.json"
HEALTH_URL = "http://127.0.0.1:8081/health"
LLM_URL = "http://127.0.0.1:8081/v1/chat/completions"

MODELS = {
    "qwen25": {
        "filename": "qwen2.5-coder-14b-instruct-q4_k_m.gguf",
        "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 256, "temperature": 0.1, "system_prompt": True,
    },
    "deepcoder": {
        "filename": "agentica-org_DeepCoder-14B-Preview-Q4_K_M.gguf",
        "ctx": 4096, "threads": 4, "mlock": 1,
        "max_tokens": 256, "temperature": 0.6, "top_p": 0.95, "system_prompt": False,
    },
    "phi4": {
        "filename": "phi-4-Q4_K_M.gguf",
        "ctx": 4096, "threads": 4, "mlock": 1,
        "max_tokens": 256, "temperature": 0.1, "system_prompt": True,
    },
}

def log(msg):
    print(msg, flush=True)

def switch_and_wait(model_id, cfg):
    data = {
        "model_file": cfg["filename"],
        "port": 8081, "ctx": cfg["ctx"],
        "threads": cfg["threads"], "mlock": cfg["mlock"],
    }
    with open(SWITCH_FILE, "w") as f:
        json.dump(data, f, indent=2)
    log(f"  Switching to {model_id} ({cfg['filename']})...")
    time.sleep(5)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 420:
        try:
            req = urllib.request.Request(HEALTH_URL)
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    d = json.loads(resp.read())
                    if d.get("status") == "ok":
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
    log(f"  [{label}] {elapsed:.0f}s, {len(content)} chars, "
        f"{result['usage']['completion_tokens']} tokens")
    return content

def parse_json(raw):
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        pass
    matches = list(re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", raw, re.DOTALL))
    for m in reversed(matches):
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
    return None

# ── Main ──
QUESTION = "How should we handle database connection pooling in a FastAPI async application?"

log("=" * 60)
log("STEP 2: Single DART Round Live Test")
log(f"Question: {QUESTION}")
log("=" * 60)

total_start = time.monotonic()

# ═══ A: Proposer (Qwen2.5-Coder-14B) ═══
log("\n── Phase A: Proposer (Qwen2.5-Coder-14B) ──")
if not switch_and_wait("qwen25", MODELS["qwen25"]):
    sys.exit(1)

proposer_msgs = [
    {"role": "system", "content": "You are a solution PROPOSER in a code debate. Respond with STRICT JSON only."},
    {"role": "user", "content": f"""Topic: {QUESTION}

This is Round 1. No prior history yet.
Your task: Propose a concrete solution with code.

Output STRICT JSON:
{{"logic_summary": "max 3 sentences", "code_snippet": "```python\\n...\\n```", "confidence_score": 0-100, "disagreement_points": ["point1"], "citations": []}}"""}
]
proposer_raw = call_llm("qwen25", MODELS["qwen25"], proposer_msgs, "PROPOSER")
proposer_json = parse_json(proposer_raw)
log(f"  Proposer JSON parsed: {proposer_json is not None}")

# ═══ B: Refuter (DeepCoder-14B) ═══
log("\n── Phase B: Refuter (DeepCoder-14B) ──")
if not switch_and_wait("deepcoder", MODELS["deepcoder"]):
    sys.exit(1)

refuter_msgs = [
    {"role": "user", "content": f"""[ROLE: You are a CRITICAL REFUTER in a code debate. Find weaknesses and propose alternatives. Be constructive.]

Topic: {QUESTION}

Proposer's argument:
{proposer_raw}

Your task:
1. Identify logical flaws, missing edge cases, or performance issues.
2. Propose a concrete alternative for each weakness found.

Output STRICT JSON:
{{"logic_summary": "max 3 sentences", "code_snippet": "```python\\n...\\n```", "confidence_score": 0-100, "disagreement_points": ["point1"], "citations": []}}"""}
]
refuter_raw = call_llm("deepcoder", MODELS["deepcoder"], refuter_msgs, "REFUTER")
refuter_json = parse_json(refuter_raw)
log(f"  Refuter JSON parsed: {refuter_json is not None}")

# ═══ C: Judge (Phi-4-14B) ═══
log("\n── Phase C: Judge (Phi-4-14B) ──")
if not switch_and_wait("phi4", MODELS["phi4"]):
    sys.exit(1)

# Random Alpha/Beta assignment (Winner Mapping F4)
if random.random() < 0.5:
    alpha_label, beta_label = "qwen25-coder-14b", "deepcoder-14b"
    alpha_raw, beta_raw = proposer_raw, refuter_raw
else:
    alpha_label, beta_label = "deepcoder-14b", "qwen25-coder-14b"
    alpha_raw, beta_raw = refuter_raw, proposer_raw

log(f"  Alpha = {alpha_label}")
log(f"  Beta  = {beta_label}")

judge_msgs = [
    {"role": "system", "content": """You are a 3-person jury panel for a code debate:
- Juror 1: Security expert
- Juror 2: Performance optimization expert
- Juror 3: Code readability/maintainability expert

Rules:
- You see two ANONYMIZED proposals (Draft Alpha, Draft Beta). Order is random.
- IGNORE: response length, comment style, politeness, formatting verbosity.
- JUDGE ONLY: logical correctness, code integrity, factual accuracy."""},
    {"role": "user", "content": f"""Topic: {QUESTION}

Draft Alpha:
{alpha_raw[:2000]}

Draft Beta:
{beta_raw[:2000]}

Each juror: which draft is stronger and why?
Overall consensus score (0-100).

Output STRICT JSON:
{{"jury_opinions": {{"security": "...", "performance": "...", "readability": "..."}}, "consensus_score": 0-100, "winner": "alpha|beta|tie", "suggested_search_query": "keyword or null", "disagreement_analysis": "1 sentence"}}"""}
]
judge_raw = call_llm("phi4", MODELS["phi4"], judge_msgs, "JUDGE")
judge_json = parse_json(judge_raw)
log(f"  Judge JSON parsed: {judge_json is not None}")

# ═══ Results ═══
total_elapsed = time.monotonic() - total_start
log(f"\n{'='*60}")
log(f"DART Round 1 Complete — total: {total_elapsed/60:.1f} min")
log(f"{'='*60}")

if judge_json:
    log("\n═══ JUDGE VERDICT ═══")
    log(f"  Consensus Score: {judge_json.get('consensus_score')}%")
    winner_label = judge_json.get('winner', '?')
    log(f"  Winner (label): {winner_label}")
    if winner_label == "alpha":
        log(f"  Winner (model): {alpha_label}")
    elif winner_label == "beta":
        log(f"  Winner (model): {beta_label}")
    else:
        log("  Winner (model): TIE")
    log(f"  Search Query: {judge_json.get('suggested_search_query')}")
    log(f"  Disagreement: {judge_json.get('disagreement_analysis')}")
    log("\n  Jury Opinions:")
    for juror, opinion in judge_json.get("jury_opinions", {}).items():
        log(f"    [{juror}] {opinion[:120]}...")

    # Save results
    result = {
        "question": QUESTION,
        "round": 1,
        "proposer_model": "qwen25-coder-14b",
        "refuter_model": "deepcoder-14b",
        "judge_model": "phi-4-14b",
        "alpha": alpha_label,
        "beta": beta_label,
        "winner_label": winner_label,
        "winner_model": alpha_label if winner_label == "alpha" else (beta_label if winner_label == "beta" else "tie"),
        "consensus_score": judge_json.get("consensus_score"),
        "total_elapsed_s": int(total_elapsed),
    }
    os.makedirs("/opt/ai_data/debate_sessions/test_results", exist_ok=True)
    with open("/opt/ai_data/debate_sessions/test_results/dart_round1.json", "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    log("\n  Results saved to /opt/ai_data/debate_sessions/test_results/dart_round1.json")
else:
    log(f"\n  Judge RAW ({len(judge_raw)} chars):")
    log(judge_raw[:600])
