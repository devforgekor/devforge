#!/usr/bin/env python3
# Status: experimental
# Path: none — standalone tldr verification method comparison
"""TLDR Verification Method Comparison — NLI vs Reranker vs Embedding.

Compares three methods for detecting hallucinated tldr in enrich output:
  1. NLI (DeBERTa-v3, :8085) — logical entailment (ENTAIL/CONTRADICT/NEUTRAL)
  2. Reranker (Qwen3-4B Q8, :8080) — topical relevance score (0.0-1.0)
  3. Embedding cosine (Qwen3-8B, :8081) — cosine similarity (0.0-1.0)

Reads real turns + enrich_meta from DB, runs all 3 methods, compares verdicts.
"""

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional
from collections import defaultdict

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(os.path.join(SCRIPTS_DIR, "pipelines"))

from lib.llm_client import _call_nli_server, reranker_score
from lib.db import esc_sql, psql_json


# ── Config ──────────────────────────────────────────────────────────────────
NLI_PORT = 8085
RERANKER_PORT = 8080
EMBED_PORT = 8081
SAMPLE_LIMIT = 10  # Number of tldrs to test


def _embed_cosine(tldr: str, source: str) -> Optional[float]:
    """Compute cosine similarity between tldr and source via embed server."""
    try:
        import urllib.request
        body = json.dumps({"input": [tldr, source], "model": "default"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{EMBED_PORT}/v1/embeddings",
            data=body, headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        embeds = [d["embedding"] for d in data.get("data", [])]
        if len(embeds) == 2:
            import math
            a, b = embeds[0], embeds[1]
            dot = sum(x*y for x, y in zip(a, b))
            na = math.sqrt(sum(x*x for x in a))
            nb = math.sqrt(sum(x*x for x in b))
            return dot / (na * nb) if na and nb else 0.0
        return None
    except Exception as e:
        return None


def _nli_entail(tldr: str, source: str) -> Dict[str, Any]:
    """Check if source entails tldr via DeBERTa NLI."""
    t0 = time.monotonic()
    label = _call_nli_server(source, tldr, strict=False, nli_port=NLI_PORT, timeout=30)
    elapsed = (time.monotonic() - t0) * 1000
    verdict = "PASS" if label == "ENTAILMENT" else ("FAIL" if label == "CONTRADICTION" else "WARN")
    return {"label": label, "verdict": verdict, "elapsed_ms": round(elapsed, 1)}


def _reranker_relevance(tldr: str, source: str) -> Dict[str, Any]:
    """Score tldr-source relevance via reranker."""
    t0 = time.monotonic()
    score = reranker_score(tldr, source)
    elapsed = (time.monotonic() - t0) * 1000
    from lib.llm_client import reranker_nli_verdict
    verdict = reranker_nli_verdict(score)
    return {"score": round(score, 4), "verdict": verdict, "elapsed_ms": round(elapsed, 1)}


def _mock_embed_cosine(tldr: str, source: str) -> Dict[str, Any]:
    """Try real embed, return mock if unavailable."""
    cos = _embed_cosine(tldr, source)
    if cos is not None:
        verdict = "GROUNDED" if cos >= 0.90 else ("AMBIGUOUS" if cos >= 0.75 else "UNGROUNDED")
        return {"cosine": round(cos, 4), "verdict": verdict, "elapsed_ms": 0, "real": True}
    return {"cosine": None, "verdict": "UNAVAILABLE", "elapsed_ms": 0, "real": False}


def _load_turns_with_enrich(limit: int = SAMPLE_LIMIT) -> List[Dict]:
    """Load turns with enrich_meta from DB."""
    sql = (
        "SELECT t.id, t.user_turn, t.text, "
        "       rf.evidence::text AS enrich_json "
        "FROM turns t "
        "JOIN review_facts rf ON rf.turn_id = t.id AND rf.fact_type = 'enrich_meta' "
        "WHERE t.text != '' "
        "ORDER BY t.created_at DESC "
        f"LIMIT {limit}"
    )
    rows = psql_json(sql)
    result = []
    for r in rows:
        enrich_raw = r.get("enrich_json", "")
        enrich = None
        if enrich_raw:
            try:
                enrich = json.loads(enrich_raw)
            except json.JSONDecodeError:
                pass
        if not enrich:
            continue
        tldr = enrich.get("tldr", "") or ""
        if not tldr:
            continue
        result.append({
            "id": r["id"],
            "user_turn": r.get("user_turn", ""),
            "text": r.get("text", ""),
            "tldr": tldr,
            "enrich": enrich,
        })
    return result


def main():
    print("=" * 72)
    print("TLDR Verification Method Comparison")
    print("=" * 72)

    check_embed = False
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:8081/health")
        with urllib.request.urlopen(req, timeout=3) as resp:
            check_embed = True
    except Exception:
        pass

    print(f"\nAvailable services:")
    print(f"  NLI DeBERTa (:8085)     — {'OK' if _ping(8085) else 'DOWN'}")
    print(f"  Reranker 4B (:8080)     — {'OK' if _ping(8080) else 'DOWN'}")
    print(f"  Embed 8B  (:8081)       — {'OK' if check_embed else 'DOWN'}")
    print(f"\nLoading {SAMPLE_LIMIT} turns with enrich_meta from DB...")

    turns = _load_turns_with_enrich(SAMPLE_LIMIT)
    if not turns:
        print("ERROR: No turns with enrich_meta found")
        sys.exit(1)

    print(f"Loaded {len(turns)} turns\n")

    # ── Generate adversarial test cases ─────────────────────────────────
    # Real tldrs from DB + synthetic hallucinated tldrs for comparison
    test_cases = []
    for turn in turns:
        source = f"{turn['user_turn']} {turn['text']}"
        real_tldr = turn["tldr"]

        # Create a hallucinated version by swapping key entities
        hallu_tldr = _make_hallucinated(real_tldr)

        test_cases.append({
            "id": turn["id"][:8],
            "source": source[:500],
            "tldr_real": real_tldr,
            "tldr_hallu": hallu_tldr,
            "label": "real",
        })

    # ── Run all methods ─────────────────────────────────────────────────
    results = []
    for tc in test_cases:
        for variant, tldr in [("real", tc["tldr_real"]), ("hallu", tc["tldr_hallu"])]:
            row = {"id": tc["id"], "variant": variant, "tldr": tldr[:80]}

            # 1. NLI
            row["nli"] = _nli_entail(tldr, tc["source"])

            # 2. Reranker
            row["reranker"] = _reranker_relevance(tldr, tc["source"])

            # 3. Embedding (real or unavailable)
            row["embed"] = _mock_embed_cosine(tldr, tc["source"])

            results.append(row)

    # ── Print results ──────────────────────────────────────────────────
    print(f"{'─' * 72}")
    print(f"{'ID':<8} {'Var':<6} {'NLI':<14} {'Reranker':<14} {'Embed':<14}")
    print(f"{'─' * 72}")

    for r in results:
        nli_str = f"{r['nli']['label']}({r['nli']['verdict']})"
        rer_str = f"{r['reranker']['score']:.2f}({r['reranker']['verdict']})"
        if r['embed'].get('real'):
            emb_str = f"{r['embed']['cosine']:.2f}({r['embed']['verdict']})"
        else:
            emb_str = "UNAVAIL"
        print(f"{r['id']:<8} {r['variant']:<6} {nli_str:<14} {rer_str:<14} {emb_str:<14}")

    # ── Statistical summary ────────────────────────────────────────────
    print(f"\n{'=' * 72}")
    print("Summary: Ground Truth = real tldr should PASS, hallucinated should FAIL")
    print(f"{'=' * 72}")

    # Count correct detections
    nli_correct = 0
    reranker_correct = 0
    total = len(results) // 2  # pairs

    for i in range(total):
        real = results[i * 2]
        hallu = results[i * 2 + 1]

        # NLI: real should be ENTAILMENT, hallu should be CONTRADICTION
        nli_ok = (real["nli"]["verdict"] == "PASS" and hallu["nli"]["verdict"] == "FAIL")
        if nli_ok:
            nli_correct += 1

        # Reranker: real should be > hallu by significant margin
        rer_ok = real["reranker"]["score"] > hallu["reranker"]["score"] + 0.1
        if rer_ok:
            reranker_correct += 1

    print(f"  NLI (DeBERTa)       : {nli_correct}/{total} correct ({nli_correct/total*100:.0f}%)")
    print(f"  Reranker (4B Q8)    : {reranker_correct}/{total} correct ({reranker_correct/total*100:.0f}%)")

    # Average elapsed time
    avg_nli = sum(r["nli"]["elapsed_ms"] for r in results) / len(results)
    avg_rer = sum(r["reranker"]["elapsed_ms"] for r in results) / len(results)
    print(f"\n  Avg latency:")
    print(f"    NLI:      {avg_nli:.0f}ms")
    print(f"    Reranker: {avg_rer:.0f}ms")

    print(f"\n{'=' * 72}")
    print("Method characteristics (from web research):")
    print("  NLI (DeBERTa-v3):       논리적 entailment 측정 — negation/수치 모순 감지")
    print("                           단점: 435M으로 recall 낮음 (NEUTRAL 과다)")
    print("  Reranker (Qwen3-4B):    topical relevance 측정 — 주제 이탈 감지")
    print("                           단점: 논리적 모순(negation) 놓침")
    print("  Embed cosine (8B Q8):   의미적 유사도 — CIKM 2025: hallucination")
    print("                           탐지에 신뢰 불가 (표면 유사도에 의존)")
    print("=" * 72)

    # Recommend
    print(f"\n추천: NLI + Reranker 조합")
    print(f"  NLI가 CONTRADICTION → FAIL (높은 정밀도)")
    print(f"  NLI가 NEUTRAL → Reranker score 확인 (fallback)")
    print(f"  Reranker < 0.40 → FAIL (주제 이탈)")
    print(f"  Embed cosine은 단독으로 신뢰 불가 (보조 참고만)")


def _ping(port: int) -> bool:
    """Quick health check."""
    import urllib.request
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=3):
            return True
    except Exception:
        return False


def _make_hallucinated(tldr: str) -> str:
    """Create a hallucinated version of tldr by entity substitution."""
    import re

    # Korean/English entity swap patterns
    swaps = [
        ("PostgreSQL", "MySQL"),
        ("Python", "JavaScript"),
        ("Linux", "Windows"),
        ("Docker", "Kubernetes"),
        ("ARM", "x86"),
        ("Ubuntu", "CentOS"),
        ("Qwen3", "Llama 3"),
        ("구축", "설치"),
        ("설정", "제거"),
        ("개발", "배포"),
        ("embed", "vector search"),
        ("7B", "70B"),
        ("8B", "4B"),
        ("14B", "32B"),
        ("NLI", "NER"),
        ("reranker", "classifier"),
        (":8080", ":3000"),
        (":8082", ":5000"),
        ("Pod", "Cluster"),
        ("systemd", "supervisord"),
        ("podman", "docker"),
        ("quadlet", "compose"),
        ("postgres", "redis"),
        ("한국어", "영어"),
        ("tldr", "summary"),
    ]

    for src, dst in swaps:
        pattern = re.compile(re.escape(src), re.IGNORECASE)
        if pattern.search(tldr):
            return pattern.sub(dst, tldr, count=1)

    # If no entity matched, prepend something hallucinated
    return f"System migration from ARM to x86: {tldr}"


if __name__ == "__main__":
    main()
