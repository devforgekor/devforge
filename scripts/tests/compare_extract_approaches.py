#!/usr/bin/env python3
# Status: experimental
# Path: none — test script
"""Compare 3 extract approaches on short/medium/long turns.

Usage: python3 scripts/tests/compare_extract_approaches.py
"""
import json, os, sys, time, re, urllib.request, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPTS_DIR = "/opt/projects/server/scripts"
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(SCRIPTS_DIR)

from lib.db import psql_json, esc_sql
from lib.llm_client import call_llm, MODEL_REGISTRY
from lib.common import log

# ── System prompts ─────────────────────────────

# APPROACH A: Current — "Extract at most 5 facts per fact_type"
PROMPT_A = """\
You are a fact extractor for a developer-assistant conversation turn.
Each turn has three parts: user_turn (the user's message), thinking (the
model's internal reasoning, may be empty), and text (the model's response).

Extract key factual statements that are EXPLICITLY present in the text.
Do NOT infer, summarize, or add information not present in the source.

Output STRICT JSON:
{
  "extractions": [
    {
      "fact_type": "user|thinking|text",
      "evidence": "Exact quote or close paraphrase from the source",
      "category": "requirement|decision|explanation|code|reasoning|other"
    }
  ]
}

Rules:
- fact_type must match which source field the evidence came from
- evidence must be directly traceable to the source text
- Skip thinking if it is empty or contains only formatting
- Extract at most 5 facts per fact_type
- If nothing extractable, return {"extractions": []}"""

# APPROACH C: Prompt only — remove max, add emphasis
PROMPT_C = """\
You are a fact extractor for a developer-assistant conversation turn.
Each turn has three parts: user_turn (the user's message), thinking (the
model's internal reasoning, may be empty), and text (the model's response).

Extract key factual statements that are EXPLICITLY present in the text.
Do NOT infer, summarize, or add information not present in the source.

Output STRICT JSON:
{
  "extractions": [
    {
      "fact_type": "user|thinking|text",
      "evidence": "Exact quote or close paraphrase from the source",
      "category": "requirement|decision|explanation|code|reasoning|other"
    }
  ]
}

Rules:
- fact_type must match which source field the evidence came from
- evidence must be directly traceable to the source text
- Extract facts from EACH section: extract at least 1-2 facts from user_turn,
  at least 1-2 facts from thinking, and at least 1-2 facts from text
- If nothing extractable, return {"extractions": []}"""

# ── Test turns ─────────────────────────────────
TEST_TURNS = {
    "short": {
        "user_turn": "보여줘야 검토하지",
        "thinking": "The user is right — I should have shown it proactively after writing. They want me to present the code first so they can review it before I apply changes. This is valid feedback.",
        "text": "위에 보여드렸습니다. 47줄 전체입니다. 검토 부탁드립니다.",
    },
    "medium": {
        "user_turn": "복잡한 로직 있는지 검토",
        "thinking": "The user is asking me to review the code for complex logic. Let me re-read all the code changes and identify any complex patterns.",
        "text": (
            "검토 완료. 복잡한 로직은 없고, 대부분 단순 위임/집계 패턴입니다. "
            "다만 3건의 경미한 개선 포인트가 있습니다.\n\n"
            "**1. `search_dense()` in `hybrid_search.py`**\n"
            "임베딩 차원이 4096d로 통일되었으나 TOP_K=10이 하드코딩되어 있습니다.\n\n"
            "**2. `fact_search()` in `cli.py`**\n"
            "user_id 필터링이 누락되어 있습니다. 다른 검색과 일관성이 필요합니다.\n\n"
            "**3. `_merge_hybrid()` in `hybrid_search.py`**\n"
            "RRF 가중치(k=60)가 고정값입니다. config에서 주입 가능해야 합니다.\n\n"
            "전체적으로 리팩토링이 잘 되어 있고, 이 3건은 기능상 버그가 아닌 "
            "일관성/확장성 측면의 제안입니다."
        ),
    },
    "long": {
        "user_turn": (
            "혹시 내가 ssh에 접속하면 인증서를 사용하는데 그걸 이용하면? "
            "세션을 나가면 인증키가 사라져서 누가와도 보안이 철저해 지잖아."
        ),
        "thinking": (
            "The user has a clever idea. When they SSH into the server, they use key-based "
            "authentication. The SSH private key stays in the SSH agent (or ssh-agent) only "
            "for the duration of their session. When they log out, the key material is gone.\n\n"
            "Their proposal: use the SSH key as the root of trust for a session-scoped security "
            "mechanism. This is actually a sound concept - SSH keys are FIDO2-compatible, "
            "ECDSA P-256, and tied to the user's physical possession.\n\n"
            "Let me think about how to implement this:\n"
            "1. On SSH login, .bashrc runs and derives a session key from the SSH key via ssh-agent\n"
            "2. This session key is used to decrypt a passphrase-encrypted key stored on disk\n"
            "3. When the session ends (ssh logout), the session key is gone\n\n"
            "The practical implementation would use ssh-agent's SSH_ASKPASS or a custom "
            "socket wrapper. The authentication flow would be:\n"
            "SSH login → .bashrc → ssh-add -L (check key presence) → derive session secret "
            "→ decrypt local secrets\n\n"
            "This is similar to how gpg-agent works but scoped to SSH keys. "
            "The main advantage is zero configuration - the user already uses SSH keys."
        ),
        "text": (
            "세션 범위로 보안을 가둘 수 있습니다.\n\n"
            "**구조:**\n"
            "```\n"
            "SSH 로그인 → .bashrc 실행 → SSH 개인키로 passphrase 복호화\n"
            "→ 세션 전용 secret 획득 → 종료 시 소멸\n"
            "```\n\n"
            "**장점:**\n"
            "- SSH 로그아웃하면 모든 키가 사라짐 (포렌식 불가)\n"
            "- 추가 하드웨어 불필요 (이미 SSH 키 사용 중)\n"
            "- `pam_ssh`나 `ssh-agent`가 이미 OCI 표준\n\n"
            "**단점:**\n"
            "- sudo/su 전환 시 키 체인 끊김\n"
            "- 백그라운드 서비스(chronyd, systemd)는 세션 밖\n"
            "- 여러 세션 동시 접속 시 키 공유 문제\n\n"
            "구체화하려면 `$SSH_AUTH_SOCK`을 활용한 `ssh-keygen -y` 파이프라인이 "
            "가장 간단합니다."
        ),
    },
}

# ── Reranker score ─────────────────────────────
RERANKER_URL = f"http://127.0.0.1:{MODEL_REGISTRY['reranker']['port']}/rerank"

def rerank(evidence, source):
    """Score evidence vs source via reranker."""
    try:
        data = json.dumps({"query": evidence[:500], "passages": [source[:2000]], "model":"default"}).encode()
        req = urllib.request.Request(RERANKER_URL, data=data, headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
            scores = resp.get("scores", [resp.get("result", [])])
            return scores[0] if scores else 0.0
    except Exception as e:
        return None

# ── Single fact extraction ─────────────────────
def extract_facts(user_turn, thinking, text, system_prompt, label="test", timeout=600):
    """Call LLM and parse JSON result."""
    parts = [
        "=== user_turn ===", user_turn or "(empty)", "",
        "=== thinking ===", thinking or "(empty)", "",
        "=== text ===", text or "(empty)",
    ]
    t0 = time.monotonic()
    meta = call_llm(
        [{"role": "system", "content": system_prompt},
         {"role": "user", "content": "\n".join(parts)}],
        model="day_extract", max_tokens=768, temperature=0.12,
        timeout=timeout, json_mode=True, return_meta=True,
    )
    elapsed = time.monotonic() - t0
    raw = meta["content"]
    # Parse
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Try strip markdown
        cleaned = re.sub(r'```json\s*|\s*```', '', raw)
        try:
            parsed = json.loads(cleaned)
        except Exception:
            return {"error": f"JSON parse failed: {raw[:200]}", "elapsed_s": round(elapsed, 1)}
    extractions = parsed.get("extractions", [])
    if not isinstance(extractions, list):
        return {"error": f"extractions not a list: {extractions}", "elapsed_s": round(elapsed, 1)}
    return {"extractions": extractions, "elapsed_s": round(elapsed, 1),
            "usage": meta.get("usage", {})}

def chunk_text(text, max_chars=1000, overlap=200):
    """Sentence-bounded chunking similar to extract.py."""
    import re as _re
    sents = _re.split(r'(?<=[.!?])\s+', text)
    if len(sents) <= 1:
        return [text]
    chunks = [] ; start = 0
    while start < len(sents):
        end = start ; char_count = 0
        while end < len(sents) and char_count + len(sents[end]) <= max_chars:
            char_count += len(sents[end]) ; end += 1
        if end == start: end = start + 1
        chunks.append(" ".join(sents[start:end]))
        # overlap
        ov = 0 ; ns = end
        while ns > start and ov < overlap:
            ns -= 1 ; ov += len(sents[ns])
        start = ns if ns > start else end
    return chunks

def extract_facts_chunked(user_turn, thinking, text, system_prompt, max_chars=1000, timeout=900):
    """Chunked extraction: split text, sequential to avoid slot contention."""
    chunks = chunk_text(text, max_chars=max_chars)
    results = []
    t0 = time.monotonic()
    for i, c in enumerate(chunks):
        log(f"    chunk {i+1}/{len(chunks)} ({len(c)} chars)...")
        results.append(extract_facts(user_turn, thinking, c, system_prompt, f"chunk{i}", timeout=timeout))
    elapsed = time.monotonic() - t0
    # Merge extractions (dedup by evidence)
    seen = set() ; merged = []
    for r in results:
        for ex in r.get("extractions", []):
            ev = ex.get("evidence", "").strip()
            if ev and ev not in seen:
                seen.add(ev) ; merged.append(ex)
    return {"extractions": merged, "elapsed_s": round(elapsed, 1), "chunks": len(chunks),
            "chunk_times": [r.get("elapsed_s", 0) for r in results]}

# ── Main ───────────────────────────────────────
log("=" * 70)
log("EXTRACT APPROACH COMPARISON")
log("Turn: S=short(207ch) M=medium(1514ch) L=long(5099ch)")
log("=" * 70)

APPROACHES = {
    "A-current": {"system": PROMPT_A, "fn": lambda u,t,x: extract_facts(u,t,x,PROMPT_A)},
    "B-chunked": {"system": PROMPT_C, "fn": lambda u,t,x: extract_facts_chunked(u,t,x,PROMPT_C, max_chars=1000)},
    "C-prompt_only": {"system": PROMPT_C, "fn": lambda u,t,x: extract_facts(u,t,x,PROMPT_C)},
}

results = {}

for turn_label, turn_data in TEST_TURNS.items():
    u, t, x = turn_data["user_turn"], turn_data["thinking"], turn_data["text"]
    results[turn_label] = {}
    for approach_label, approach in APPROACHES.items():
        log(f"\n--- {turn_label} / {approach_label} ---")
        res = approach["fn"](u, t, x)
        ex = res.get("extractions", [])
        # Count by fact_type
        by_type = {}
        for e in ex:
            ft = e.get("fact_type", "unknown")
            by_type.setdefault(ft, 0)
            by_type[ft] += 1

        # Reranker faithfulness on each extraction
        # Since reranker is expensive, sample up to 3 per fact_type
        faithful_results = {"total": len(ex), "grounded": 0, "ungrounded": 0, "ambig": 0}
        sampled = 0
        source_map = {"user": u, "thinking": t, "text": x}
        for e in ex:
            if sampled >= 9:
                break
            ft = e.get("fact_type", "")
            ev = e.get("evidence", "")
            src = source_map.get(ft, "")
            if ev and src:
                score = rerank(ev, src)
                if score is not None:
                    if score >= 0.75:
                        faithful_results["grounded"] += 1
                    elif score < 0.40:
                        faithful_results["ungrounded"] += 1
                    else:
                        faithful_results["ambig"] += 1
                    sampled += 1

        results[turn_label][approach_label] = {
            "fact_types": by_type,
            "total_facts": len(ex),
            "elapsed_s": res.get("elapsed_s", 0),
            "faithful_sample": faithful_results,
            "chunks": res.get("chunks", "N/A"),
            "evidence_snippets": [e.get("evidence","")[:60] for e in ex[:5]],
        }
        log(f"  facts: {by_type} total={len(ex)}")
        log(f"  time: {res.get('elapsed_s',0):.1f}s")
        log(f"  faithful(sample): {faithful_results}")
        if res.get("chunks", "N/A") != "N/A":
            log(f"  chunks: {res['chunks']}")

# ── Print summary table ────────────────────────
log("\n" + "=" * 70)
log("SUMMARY")
log("=" * 70)
header = f"{'Turn':<8} {'Approach':<15} {'user':>5} {'think':>5} {'text':>5} {'total':>5} {'time(s)':>8} {'Grounded':>9} {'Ungrnd':>7} {'Amb':>4}"
log(header)
log("-" * 75)
for turn_label in TEST_TURNS:
    for approach_label in APPROACHES:
        r = results[turn_label][approach_label]
        ft = r["fact_types"]
        fs = r["faithful_sample"]
        log(f"{turn_label:<8} {approach_label:<15} {ft.get('user',0):>5} {ft.get('thinking',0):>5} {ft.get('text',0):>5} {r['total_facts']:>5} {r['elapsed_s']:>8.1f} {fs['grounded']:>9} {fs['ungrounded']:>7} {fs['ambig']:>4}")

# Save detailed results
out = {"turns": TEST_TURNS, "results": results}
report = f"data/eval/extract_compare_{time.strftime('%Y%m%d_%H%M%S')}.json"
with open(report, "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
log(f"\nFull report: {report}")
