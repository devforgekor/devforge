#!/usr/bin/env python3.11
# Status: experimental
# Path: systemd:devforge-openrouter-free-models.timer
"""
OpenRouter free model auto-refresh for opencode.

Fetches the OpenRouter model catalog, filters :free, scores by coding ability
(Artificial Analysis coding_index, plus fallback heuristics), live-tests the
top candidates, and writes the top 3 usable ones into opencode.json
(base model + fallback chain).

Usage:
  refresh_openrouter_free_models.py            # normal run
  refresh_openrouter_free_models.py --dry-run  # print ranking, don't write
  refresh_openrouter_free_models.py --force    # ignore 1-day freshness cache
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROXY_URL = "http://127.0.0.1:8451/v1/models"
OPCODE_CONFIG = Path.home() / ".config/opencode/opencode.json"
CACHE_FILE = Path.home() / ".cache/devforge/openrouter_free_models.json"
CACHE_TTL_SEC = 24 * 60 * 60  # 1 day

SCORE_BASE = 1000.0  # base to make small coding_index differences visible
TEST_MAX_TOKENS = 8
TEST_TIMEOUT = 30
TEST_SAMPLE = ["Hello", "Please say hi", "Whats 2+2?"]

# Free models that are known-broken / not-for-coding (domain-specific)
EXCLUDE_KEYWORDS = [
    "sante",
    "fin",
    "japanese",
    "content-safety",
    "audio",
    "video",
    "note",
    "billing",
    "voice",
    "image",
    "vision",
    "omni",
    "embed",
]

# ---------------------------------------------------------------------------
# Fetch catalog
# ---------------------------------------------------------------------------


def fetch_models() -> list[dict]:
    """Fetch full OpenRouter model list via local proxy."""
    req = urllib.request.Request(PROXY_URL, headers={"User-Agent": "devforge-rr-auto"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    return data.get("data", [])


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _coding_score(model: dict) -> float:
    """Score model for coding. Uses coding_index if present, else heuristics."""
    benchmarks = model.get("benchmarks", {}) or {}
    aa = benchmarks.get("artificial_analysis", {}) or {}
    coding = aa.get("coding_index")

    if coding is not None:
        # coding_index is a benchmark index comparable across models
        return float(coding) + _context_bonus(model)

    # No benchmark — cap below ANY benchmarked model.
    # Benchmarks run roughly 30-60 for free models; cap unknown at 25 so
    # verified models (with data) always outrank guesswork.
    desc = (model.get("description") or "").lower()
    bonus = 0.0
    if "code" in desc or "coding" in desc:
        bonus += 10.0
    if "agentic" in desc.lower():
        bonus += 8.0
    context = model.get("context_length") or 0
    if context >= 100000:
        bonus += 5.0
    return min(25.0 + bonus, 29.9)  # hard cap under 30 (below any coding_index)


def _context_bonus(model: dict) -> float:
    """Small bonus for large context (useful for coding)."""
    context = model.get("context_length") or 0
    if context >= 256000:
        return 5.0
    return 0.0


def _test_model(model_id: str, key_idx: int = 0) -> tuple[bool, str]:
    """Live-test a free model through the RR proxy. Returns (ok, detail)."""
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": TEST_SAMPLE[0]}],
        "max_tokens": TEST_MAX_TOKENS,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8451/v1/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "devforge-rr-auto",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TEST_TIMEOUT) as resp:
            result = json.loads(resp.read().decode())
            if result.get("choices"):
                return True, result["choices"][0].get("finish_reason", "ok")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)[:80]
    return False, "no choices"


# ---------------------------------------------------------------------------
# opencode.json write
# ---------------------------------------------------------------------------


def _read_opencode() -> dict:
    with open(OPCODE_CONFIG) as f:
        return json.load(f)


def _write_opencode(config: dict) -> None:
    # Atomic write: tmp then rename
    tmp = OPCODE_CONFIG.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, OPCODE_CONFIG)


def _apply_opencode(models: list[dict]) -> None:
    """Set opencode.json model/chain/provider.models to top-N free models."""
    top = models[:3]
    if not top:
        print("✗ No top models to apply")
        return

    config = _read_opencode()
    provider = config.setdefault("provider", {}).setdefault("openrouter", {})
    prov_models = provider.setdefault("models", {})

    # Rebuild models dict preserving existing names where possible
    new_models = {}
    for m in top:
        mid = m["id"]
        name = (m.get("name") or mid).split("(")[0].strip()
        new_models[mid] = {"name": name[:40]}
    provider["models"] = new_models

    # Main model = best free
    config["model"] = f"openrouter/{top[0]['id']}"

    # Fallback chain: top-3 in order
    chain = [f"openrouter/{m['id']}" for m in top]
    config.setdefault("experimental", {}).setdefault("modelFallbackChain", {})["chains"] = [chain]

    _write_opencode(config)
    print(f"✓ opencode.json updated: primary={top[0]['id']}, chain={len(top)} models")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _load_cached() -> list[dict] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE) as f:
            entry = json.load(f)
        if time.time() - entry.get("ts", 0) < CACHE_TTL_SEC:
            return entry.get("models", [])
    except Exception:
        pass
    return None


def _save_cache(models: list[dict]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump({"ts": time.time(), "models": models}, f)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print ranking only")
    parser.add_argument("--force", action="store_true", help="ignore cache")
    parser.add_argument("--top", type=int, default=3, help="number of models to select")
    args = parser.parse_args()

    # 1. Fetch (with cache)
    models = None
    if not args.force:
        models = _load_cached()
    if models is None:
        print("Fetching OpenRouter model catalog...")
        try:
            all_models = fetch_models()
        except Exception as e:
            print(f"✗ Fetch failed: {e}")
            return 1
        free_models = [m for m in all_models if str(m.get("id", "")).endswith(":free")]
        models = [
            m
            for m in free_models
            if not any(k in m.get("id", "").lower() for k in EXCLUDE_KEYWORDS)
        ]
        _save_cache(models)
    print(f"  {len(models)} candidate free models")

    # 2. Score / rank
    for m in models:
        m["_score"] = _coding_score(m)
    models.sort(key=lambda m: m["_score"], reverse=True)

    # 3. Live-test top 15 (don't waste proxy calls on all)
    usable = []
    for m in models[:15]:
        model_id = m["id"]
        ok, detail = _test_model(model_id)
        print(f"  {'✓' if ok else '✗'} {model_id:50s} score={m['_score']:6.1f} test={detail}")
        if ok:
            usable.append(m)
        time.sleep(0.3)  # avoid hammering RR proxy / OpenRouter

    # 4. Pick top-N
    selected = usable[: args.top]
    print(f"\nSelected top {len(selected)}:")
    for i, m in enumerate(selected, 1):
        print(f"  {i}. {m['id']} (score {m['_score']:.1f})")

    # 5. Apply
    if args.dry_run:
        print("\n(dry-run: opencode.json not modified)")
        return 0
    if not selected:
        print("\n✗ No working free models found — opencode.json NOT modified")
        return 1
    _apply_opencode(selected)
    return 0


if __name__ == "__main__":
    sys.exit(main())
