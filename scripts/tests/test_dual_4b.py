#!/usr/bin/env python3
# Status: experimental
# Path: none — prototype of dual 4B extraction test
"""Dual 4B concurrent test: inference core 0 (:8083) B-Xplore, inference core 1 (:8082) A-Strict."""

import json
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, "/opt/projects/server/scripts")
from lib.test_common import log, test_complete, test_heartbeat, test_setup

PROMPT_A = """You are a fast Fact Detective for information retrieval.
Your ONLY job is to scan the given text and extract explicit, verifiable factual triples (Subject, Predicate, Object).

Strict Rules:
Max 3 facts: Extract up to 3 facts. If the text has fewer than 3 clear facts, return only what exists. Do NOT pad.
Prefer Specific Predicates: Use precise action verbs (e.g., founded, acquired, appointed, merged, located) when possible. If no precise verb fits, generic verbs (is, has, exists, uses) are acceptable — do not skip a valid fact just because only a generic verb works.
No Duplicates: If the same fact is mentioned twice or rephrased, extract it only once.
Skip Ambiguity: If a sentence is missing a clear subject or object, or is too vague, skip it entirely. Do not guess.
Remember: Your accuracy (precision) is far more important than filling slots. Returning an empty list is perfectly acceptable."""

PROMPT_B = """You are an Exploratory Fact Extractor. Your job is to find ALL potential facts, including those that are SLIGHTLY IMPLIED or require MINIMAL commonsense inference.

CRITICAL RULES:
1. Extract explicit facts AND strong implications.
2. Evidence can be a short supporting phrase (not necessarily verbatim, but must be grounded in the text).
3. If ambiguous, extract it as a candidate but mark it with confidence < 1.0.
4. Predicate should be a descriptive action verb. Avoid 'is/has/exists' when a stronger verb exists.
5. Deduplicate: same fact = extract once.
6. Try your best to find facts, but do NOT fabricate entirely new information not grounded in the text."""


# ── Atomic chunking v2 ──


def split_atomic(text, max_chars=400):
    raw = text.replace("\r\n", "\n")
    lines = raw.split("\n")
    groups = []
    buf = []
    in_code = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") and not in_code:
            if buf:
                groups.append("\n".join(buf))
                buf = []
            in_code = True
            buf.append(line)
        elif stripped.startswith("```") and in_code:
            buf.append(line)
            groups.append("\n".join(buf))
            buf = []
            in_code = False
        elif in_code:
            buf.append(line)
        elif stripped == "":
            if buf:
                groups.append("\n".join(buf))
                buf = []
        else:
            buf.append(line)
    if buf:
        groups.append("\n".join(buf))

    chunks = []
    for g in groups:
        if len(g) <= max_chars:
            chunks.append(g)
            continue
        paragraphs = [p.strip() for p in g.split("\n\n") if p.strip()]
        for para in paragraphs:
            if len(para) <= max_chars:
                chunks.append(para)
                continue
            sentences = re.split(r"(?<=[.!?])\s+", para)
            buf_s = ""
            for s in sentences:
                if not s.strip():
                    continue
                if not buf_s:
                    buf_s = s
                elif len(buf_s) + len(s) + 1 <= max_chars:
                    buf_s += " " + s
                else:
                    chunks.append(buf_s)
                    buf_s = s
            if buf_s:
                chunks.append(buf_s)

    merged = []
    for c in chunks:
        if not merged:
            merged.append(c)
        elif len(c) < 70:
            merged[-1] = merged[-1] + "\n" + c
        else:
            merged.append(c)
    return merged


def make_schema(max_items, has_confidence=False):
    props = {
        "subject": {"type": "string", "maxLength": 30},
        "predicate": {"type": "string", "maxLength": 40},
        "object": {"type": "string", "maxLength": 60},
        "evidence": {"type": "string", "maxLength": 120},
    }
    required = ["subject", "predicate", "object"]
    if has_confidence:
        props["confidence"] = {"type": "number"}
        required.append("confidence")
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "extraction",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "extractions": {
                        "type": "array",
                        "maxItems": max_items,
                        "items": {
                            "type": "object",
                            "properties": props,
                            "required": required,
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["extractions"],
                "additionalProperties": False,
            },
        },
    }


def call_llm(label, container, port, prompt, schema, chunk_text):
    body = json.dumps(
        {
            "model": "default",
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": chunk_text},
            ],
            "max_tokens": 1024,
            "temperature": 0.0,
            "response_format": schema,
        },
        ensure_ascii=False,
    )
    t0 = time.monotonic()
    r = subprocess.run(
        [
            "podman",
            "exec",
            "-i",
            container,
            "curl",
            "-s",
            "-m",
            "900",
            f"http://127.0.0.1:{port}/v1/chat/completions",
            "-H",
            "Content-Type: application/json",
            "-d",
            body,
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )
    elapsed = time.monotonic() - t0
    try:
        data = json.loads(r.stdout)
        if "error" in data:
            return {
                "label": label,
                "error": str(data["error"])[:200],
                "elapsed": f"{elapsed:.0f}s",
                "facts": [],
            }
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return {
            "label": label,
            "facts": parsed.get("extractions", []),
            "elapsed": f"{elapsed:.0f}s",
        }
    except Exception as e:
        return {
            "label": label,
            "error": f"{type(e).__name__}: {str(e)[:80]}",
            "elapsed": f"{elapsed:.0f}s",
            "facts": [],
        }


def main():
    test_setup("dual_4b", "Dual 4B concurrent: inference B-Xplore :8083 + inference A-Strict :8082")
    text = open("/tmp/long_turn_text.txt").read()
    chunks = split_atomic(text, max_chars=400)
    log(f"Text: {len(text)} chars -> {len(chunks)} chunks")
    for i, c in enumerate(chunks):
        log(f"  chunk[{i}]: {len(c)} chars")

    models = [
        ("A-Strict", 8082, "devforge-inference", PROMPT_A, False),
        ("B-Xplore", 8083, "devforge-inference", PROMPT_B, True),
    ]

    for max_items in [4]:
        log(f"\n{'=' * 60}")
        log(f"maxItems={max_items} | Dual concurrent")
        log(f"{'=' * 60}")

        all_results = {
            "A-Strict": {"facts": [], "elapsed": 0},
            "B-Xplore": {"facts": [], "elapsed": 0},
        }

        for ci, chunk in enumerate(chunks):
            test_heartbeat(f"chunk {ci + 1}/{len(chunks)}")
            log(f"\n--- Chunk {ci + 1}/{len(chunks)} ({len(chunk)} chars) ---")

            results = {}
            threads = []

            def run(name, port, container, prompt_str, has_conf):
                schema = make_schema(max_items, has_conf)
                r = call_llm(name, container, port, prompt_str, schema, chunk)
                results[name] = r

            for name, port, container, prompt_str, has_conf in models:
                t = threading.Thread(target=run, args=(name, port, container, prompt_str, has_conf))
                t.start()
                threads.append(t)

            for t in threads:
                t.join()

            for name in ["A-Strict", "B-Xplore"]:
                r = results.get(name, {})
                if r.get("error"):
                    log(f"  [{name}] ERROR: {r['error']} | {r.get('elapsed', '?')}")
                else:
                    facts = r.get("facts", [])
                    log(f"  [{name}] {len(facts)} facts | {r.get('elapsed', '?')}")
                    for f in facts:
                        all_results[name]["facts"].append(f)
                        conf_str = f" [{f.get('confidence')}]" if f.get("confidence") else ""
                        log(
                            f"    {f.get('subject', '')} -> {f.get('predicate', '')} -> {f.get('object', '')}{conf_str}"
                        )
                        log(f"      ev: {f.get('evidence', '')[:50]}")

        # Summary
        log(f"\n{'=' * 60}")
        log(f"Summary: {len(chunks)} chunks, maxItems={max_items}")
        log(f"{'=' * 60}")

        for name in ["A-Strict", "B-Xplore"]:
            facts = all_results[name]["facts"]
            seen = set()
            deduped = []
            for f in facts:
                key = (f.get("subject", ""), f.get("predicate", ""), f.get("object", ""))
                if key not in seen:
                    seen.add(key)
                    deduped.append(f)
            bad = [
                f["predicate"]
                for f in deduped
                if f.get("predicate", "")
                in ("is", "has", "exists", "relates_to", "equals", "was", "does")
            ]
            bad_str = f" BAD={bad}" if bad else ""
            log(f"[{name}] {len(facts)} raw -> {len(deduped)} unique{bad_str}")
            for f in deduped:
                conf_str = f" [{f.get('confidence')}]" if f.get("confidence") else ""
                log(
                    f"  {f.get('subject', '')} -> {f.get('predicate', '')} -> {f.get('object', '')}{conf_str}"
                )
                log(f"    ev: {f.get('evidence', '')[:50]}")

    test_complete("Dual 4B concurrent test done")


if __name__ == "__main__":
    main()
