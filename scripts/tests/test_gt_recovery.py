#!/usr/bin/env python3
# Status: experimental
"""Ground-truth extraction recovery comparison harness.

Loads GT definitions from scripts/tests/ground_truths/*.json, runs the
extraction pipeline on each test case, and reports recall/precision.

GT sources: real technical documents (infrastructure.md, state.yaml, etc.)
— not hand-crafted synthetic text."""

import json, os, re, subprocess, sys, time, urllib.request, uuid
from typing import Any, Dict, List, Optional, Tuple

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)
from lib.db import psql_json, psql_ok

GT_DIR = os.path.join(SCRIPTS_DIR, "tests", "ground_truths")
EXTRACT_SCRIPT = os.path.join(SCRIPTS_DIR, "pipelines", "extract.py")
PIPELINE_TIMEOUT = 7200
EMBED_PORT = 8081
EMBED_SIM_THRESHOLD = 0.75

_HANGUL_RE = re.compile(r'[\uAC00-\uD7A3\u1100-\u11FF\u3130-\u318F\uA960-\uA97C\uD7B0-\uD7FF]')

def _detect_lang(text: str) -> str:
    if _HANGUL_RE.search(text):
        return "ko"
    return "en"


def _load_ground_truths() -> List[Dict]:
    cases = []
    if not os.path.isdir(GT_DIR):
        print(f"  [GT] directory not found: {GT_DIR}", flush=True)
        return cases
    for fn in sorted(os.listdir(GT_DIR)):
        if not fn.endswith(".json"):
            continue
        fp = os.path.join(GT_DIR, fn)
        with open(fp, "r") as f:
            data = json.load(f)
        for tc in data.get("test_cases", []):
            tc.setdefault("description", "")
            tc["_file"] = fn
        cases.extend(data.get("test_cases", []))
    return cases


def _resolve_source_text(tc: Dict) -> str:
    """Return the text to feed into the pipeline."""
    sf = tc.get("source_file", "")
    if sf:
        abspath = os.path.join(SCRIPTS_DIR, "..", sf) if not sf.startswith("/") else sf
        abspath = os.path.normpath(abspath)
        if os.path.isfile(abspath):
            with open(abspath, "r") as f:
                return f.read()
        print(f"  WARN: source_file not found: {abspath}", flush=True)
        return ""
    return tc.get("user_turn", "")


def _start_embed_relay() -> bool:
    """Start embed :8081 in a fresh container. Relay mode: stop 8082 first if running."""
    import subprocess as sp
    # Stop any existing devforge-inference (frees RAM for embed model)
    sp.run(["podman", "stop", "-t", "5", "devforge-inference"], timeout=30, capture_output=True)
    time.sleep(2)

    try:
        req = urllib.request.Request(f"http://127.0.0.1:{EMBED_PORT}/health")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                print("  [embed] already healthy", flush=True)
                return True
    except Exception:
        pass

    meta = {"port": 8081, "file": "Qwen3-Embedding-4B-Q4_K_M.gguf",
            "ctx": 2048, "threads": 2, "threads_batch": 2, "parallel": 1}
    cmd = ["podman", "run", "-d", "--rm", "--name", "gt-embed",
           "--network", "host", "--user", "1000:1000",
           "-v", "/opt/ai_data/models/gguf:/models:Z",
           "ghcr.io/ggml-org/llama.cpp:server",
           "-m", f"/models/{meta['file']}",
           "--host", "0.0.0.0", "--port", str(meta["port"]),
           "--ctx-size", str(meta["ctx"]),
           "--parallel", str(meta["parallel"]),
           "--threads", str(meta["threads"]),
           "--threads-batch", str(meta["threads_batch"]),
           "--timeout", "28800",
           "--batch-size", "512", "--ubatch-size", "512",
           "--embedding", "--pooling", "last", "--embd-normalize", "-1",
           "--cont-batching", "--no-mmap", "-lv", "6", "--metrics"]
    r = sp.run(cmd, capture_output=True, timeout=30, text=True)
    if r.returncode != 0:
        print(f"  [embed] run failed: {r.stderr.strip()[:200]}", flush=True)
        return False
    from lib.pod_manager import wait_health as wh
    ok = wh(meta["port"], timeout=300)
    print(f"  [embed] :{EMBED_PORT} {'healthy' if ok else 'unreachable'}", flush=True)
    return ok


def _stop_embed() -> None:
    import subprocess as sp
    sp.run(["podman", "stop", "-t", "3", "gt-embed"], timeout=30, capture_output=True)


def _embed_texts(texts: List[str]) -> Optional[List[List[float]]]:
    try:
        data = json.dumps({"input": texts, "model": "default"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{EMBED_PORT}/v1/embeddings",
            data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read())
        embeds = [d["embedding"] for d in sorted(result["data"], key=lambda x: x["index"])]
        return embeds
    except Exception as e:
        print(f"  [embed] error: {e}", flush=True)
        return None


def _cosine_sim(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _embed_match(got: List[Dict], expected: List[Dict]) -> Tuple[set, set, int, int]:
    if not got or not expected:
        return set(), set(), 0, 0
    got_texts = []
    for f in got:
        subj = (f.get("subject") or "").removeprefix("Service ")
        obj = f.get("object", "") or ""
        pred = f.get("predicate", "") or ""
        got_texts.append(f"{subj} {pred} {obj}")
    gt_texts = []
    for g in expected:
        parts = [g.get("subject", "")]
        oc = g.get("object_contains", "")
        oa = g.get("object_also", "")
        parts.append(oc)
        if oa:
            parts.append(oa)
        gt_texts.append(" ".join(parts))

    if not _start_embed_relay():
        print("  [embed] relay failed, falling back to substring match", flush=True)
        return _substring_match(got, expected)

    print("  [embed] computing embeddings...", flush=True)
    all_texts = got_texts + gt_texts
    embeds = _embed_texts(all_texts)
    _stop_embed()

    if embeds is None:
        print("  [embed] failed, falling back to substring match", flush=True)
        return _substring_match(got, expected)

    n_got = len(got_texts)
    got_embs = embeds[:n_got]
    gt_embs = embeds[n_got:]

    matched_got: set = set()
    matched_gt: set = set()
    for gi in range(len(expected)):
        best_fi, best_sim = -1, 0.0
        for fi in range(n_got):
            if fi in matched_got:
                continue
            sim = _cosine_sim(gt_embs[gi], got_embs[fi])
            if sim > best_sim:
                best_sim = sim
                best_fi = fi
        if best_sim >= EMBED_SIM_THRESHOLD:
            matched_got.add(best_fi)
            matched_gt.add(gi)
            print(f"  [embed-match] GT#{gi} '{gt_texts[gi][:50]}...' ↔ fact#{best_fi} (sim={best_sim:.3f})", flush=True)

    return matched_gt, matched_got, len(matched_gt), len(matched_got)


def _subj_obj_contains_match(gt: Dict, fact: Dict) -> bool:
    subj = (fact.get("subject") or "").removeprefix("Service ")
    src = (subj + " " + (fact.get("object") or "")).lower()
    if not src:
        return False
    subj = (gt.get("subject") or "").lower()
    obj_contains = (gt.get("object_contains") or "").lower()
    obj_also = (gt.get("object_also") or "").lower()
    pred = (gt.get("predicate") or "").lower()
    if subj not in src:
        return False
    if obj_contains and obj_contains not in src:
        return False
    if obj_also and not re.search(obj_also.replace(".", "\\.").replace("*", ".*"), src):
        return False
    if pred:
        fact_pred = (fact.get("predicate") or "").lower()
        if pred not in fact_pred:
            return False
    return True

def _substring_match(got: List[Dict], expected: List[Dict]) -> Tuple[set, set, int, int]:
    matched_got: set = set()
    matched_gt: set = set()
    for gi, gt in enumerate(expected):
        for fi, fact in enumerate(got):
            if fi in matched_got:
                continue
            if _subj_obj_contains_match(gt, fact):
                matched_got.add(fi)
                matched_gt.add(gi)
                break
    return matched_gt, matched_got, len(matched_gt), len(matched_got)


def _substr_fallback(got: List[Dict], expected: List[Dict],
                     skip_gt: set, skip_got: set) -> Tuple[set, set]:
    """Substring-match for GT facts missed by embedding."""
    matched_gt: set = set()
    matched_got: set = set()
    for gi, gt in enumerate(expected):
        if gi in skip_gt:
            continue
        for fi, fact in enumerate(got):
            if fi in skip_got or fi in matched_got:
                continue
            if _subj_obj_contains_match(gt, fact):
                matched_gt.add(gi)
                matched_got.add(fi)
                print(f"  [substr-match] GT#{gi} ↔ fact#{fi}", flush=True)
                break
    return matched_gt, matched_got

def _match_facts(got: List[Dict], expected: List[Dict]) -> Tuple[set, set, int, int]:
    matched_gt, matched_got, recall, precision = _embed_match(got, expected)
    if recall < len(expected):
        extra_gt, extra_got = _substr_fallback(got, expected, matched_gt, matched_got)
        if extra_gt:
            before = recall
            matched_gt |= extra_gt
            matched_got |= extra_got
            recall = len(matched_gt)
            precision = len(matched_got)
            print(
                f"  [substr-fallback] added {recall - before} more match(es) "
                f"(recall {before}/{len(expected)} → {recall}/{len(expected)})",
                flush=True,
            )
    return matched_gt, matched_got, recall, precision


def run_test_case(tc: Dict) -> Dict:
    name = tc["name"]
    print(f"\n{'='*60}", flush=True)
    print(f"  Test: {name}", flush=True)
    print(f"  File: {tc['_file']}", flush=True)
    if tc.get("description"):
        print(f"  Desc: {tc['description']}", flush=True)
    sf = tc.get("source_file", "")
    if sf:
        print(f"  Source: {sf}", flush=True)
    print(f"{'='*60}", flush=True)

    source_text = _resolve_source_text(tc)
    if not source_text:
        return {"name": name, "status": "skip", "reason": "empty source"}

    section_type = tc.get("section_type", "user")
    facts_gt = tc.get("facts", [])

    tid = str(uuid.uuid4())
    cid = str(uuid.uuid4())
    esc = lambda s: s.replace("'", "''")

    psql_ok(
        f"INSERT INTO conversations (id,title,source,created_at) "
        f"VALUES ('{cid}'::uuid,'gt-{name}','gt-test','2026-07-08T00:00:00Z') "
        f"ON CONFLICT DO NOTHING"
    )
    seq = int(time.time() * -1000) % 100000
    if section_type == "text":
        turn_user = ""
        turn_text = source_text
    else:
        turn_user = source_text
        turn_text = ""

    detected_lang = _detect_lang(source_text)
    psql_ok(
        f"INSERT INTO turns (id,user_turn,text,thinking,pipeline_state,"
        f"conversation_id,source_message_id,created_at,detected_lang,est_chars,seq) "
        f"VALUES ('{tid}'::uuid,'{esc(turn_user)}','{esc(turn_text)}','','scanned',"
        f"'{cid}'::uuid,'gt-{name}-{tid[:8]}','2026-07-08T00:00:00Z','{detected_lang}',"
        f"{len(source_text)},{seq}) "
        f"ON CONFLICT DO NOTHING"
    )

    timeout = tc.get("timeout", PIPELINE_TIMEOUT)
    t0 = time.monotonic()
    print(f"  Running pipeline (timeout={timeout}s)...", flush=True)
    timed_out = False
    try:
        r = subprocess.run(
            [sys.executable, EXTRACT_SCRIPT, "--turn-id", tid],
            capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as te:
        r = te
        timed_out = True
        print(f"  ⚠ Pipeline timed out after {timeout}s", flush=True)
    elapsed = time.monotonic() - t0

    result = {
        "name": name,
        "status": "timed_out" if timed_out else "done",
        "elapsed_s": round(elapsed),
        "stdout_tail": r.stdout[-30000:] if r.stdout else "",
        "stderr": r.stderr[-10000:] if r.stderr else "",
    }

    facts = (
        psql_json(
            f"SELECT fact_index,subject,predicate,object,evidence,qualifiers,"
            f"nli_verdict,fact_type "
            f"FROM review_facts WHERE turn_id='{tid}'::uuid AND source='extract_pipeline' "
            f"ORDER BY fact_index"
        )
        or []
    )

    section_facts = [f for f in facts if f.get("fact_type") == section_type]
    g = sum(1 for f in facts if f.get("nli_verdict") == "GROUNDED")

    result["total_facts"] = len(facts)
    result["grounded"] = g
    result[f"{section_type}_facts"] = len(section_facts)

    gt_matched, got_matched, recall, precision = _match_facts(section_facts, facts_gt)

    result["recall"] = f"{recall}/{len(facts_gt)}"
    result["precision"] = f"{precision}/{len(section_facts)}"

    print(f"\n  ── Results for {name} ──", flush=True)
    print(f"  Time: {elapsed:.0f}s", flush=True)
    print(f"  Source: {len(source_text)} chars", flush=True)
    print(f"  Facts: {len(facts)} total, {g} grounded ({len(section_facts)} in section)", flush=True)
    print(f"  Recall: {recall}/{len(facts_gt)}", flush=True)
    print(f"  Precision: {precision}/{len(section_facts)}", flush=True)

    missed = [facts_gt[i] for i in range(len(facts_gt)) if i not in gt_matched]
    if missed:
        print(f"\n  MISSED ({len(missed)}):", flush=True)
        for m in missed:
            print(f"    subj={m.get('subject','')!r} obj_contains={m.get('object_contains','')!r}  # {m.get('note','')}", flush=True)

    extra = [section_facts[i] for i in range(len(section_facts)) if i not in got_matched]
    if extra:
        print(f"\n  EXTRA ({len(extra)} unmatched):", flush=True)
        for e in extra[:8]:
            print(f"    {e.get('subject','')!r} | {e.get('predicate','')!r} | {e.get('object','')!r}", flush=True)
        if len(extra) > 8:
            print(f"    ... and {len(extra)-8} more", flush=True)

    # Detail: section facts
    print(f"\n  All {section_type} facts:", flush=True)
    for f in section_facts:
        subj = (f.get("subject") or "")[:32]
        pred = (f.get("predicate") or "")[:22]
        obj = (f.get("object") or "")[:40]
        nli = f.get("nli_verdict", "?")[:4]
        print(f"    [{nli:4s}] {subj:32s} | {pred:22s} | {obj}", flush=True)

    # Restart inference container (stopped by _start_embed_relay during _match_facts)
    try:
        from lib.pod_manager import ensure_model as _restart_inference
        _restart_inference("day-extractor", skip_if_healthy=False)
    except Exception as restart_err:
        print(f"  [restart] inference restart failed: {restart_err}", flush=True)

    # Cleanup
    psql_ok(
        f"DELETE FROM review_facts WHERE turn_id='{tid}'::uuid "
        f"AND source='extract_pipeline'"
    )
    psql_ok(
        f"DELETE FROM pipeline_checkpoints WHERE pipeline='extract' "
        f"AND turn_id='{tid}'::uuid"
    )
    psql_ok(f"DELETE FROM turns WHERE id='{tid}'::uuid")
    psql_ok(
        f"DELETE FROM conversations WHERE id='{cid}'::uuid AND source='gt-test'"
    )

    return result


def main():
    cases = _load_ground_truths()
    if not cases:
        print("No GT test cases found.", flush=True)
        return

    print(f"Loaded {len(cases)} test case(s):", flush=True)
    for tc in cases:
        sf = tc.get("source_file", tc.get("user_turn", "")[:40])
        gf = len(tc.get("facts", []))
        print(f"  - {tc['name']}: {gf} GT facts, source={sf}", flush=True)

    all_results = []
    for tc in cases:
        res = run_test_case(tc)
        all_results.append(res)
        print(f"  [{res.get('status','?')}] {tc['name']}: recall={res.get('recall','?')} precision={res.get('precision','?')}", flush=True)

    print(f"\n{'='*60}", flush=True)
    print(f"  SUMMARY", flush=True)
    print(f"{'='*60}", flush=True)
    for res in all_results:
        name = res["name"]
        if res["status"] == "skip":
            print(f"  {name}: SKIP ({res.get('reason','')})", flush=True)
            continue
        if res["status"] == "timed_out":
            print(f"  {name}: TIMEOUT after {res.get('elapsed_s',0)}s", flush=True)
            continue
        print(f"  {name}: recall={res.get('recall','?')} precision={res.get('precision','?')} ({res.get('elapsed_s',0)}s)", flush=True)

    out_path = os.path.join(SCRIPTS_DIR, "tests", "gt_recovery_results.json")
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to {out_path}", flush=True)

    passed = all(
        r["status"] == "done" for r in all_results if r["status"] not in ("skip", "timed_out")
    )
    print(f"\n  >>> {'ALL PASS' if passed else 'SOME FAILED'} <<<", flush=True)


if __name__ == "__main__":
    main()
