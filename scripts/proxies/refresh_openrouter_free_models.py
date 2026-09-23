#!/usr/bin/env python3.11
# Status: experimental
# Path: systemd:devforge-openrouter-free-models.timer
"""
OpenRouter free model auto-refresh for opencode.

Fetches the OpenRouter model catalog, filters :free, scores by coding ability
(Artificial Analysis coding_index, plus fallback heuristics), live-tests the
top candidates, and writes the top 3 usable ones into opencode-rr.json
(base model + fallback chain). The default opencode.json (opencode-go) is
never touched.

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
# auto-router target: the opencode-rr profile (RR proxy free models).
# The default (global) opencode.json stays pinned to opencode-go/deepseek-v4-flash.
OPCODE_CONFIG = Path.home() / ".config/opencode/opencode-rr.json"
ANALYSIS_CONFIG = Path.home() / ".config/devforge/analysis_models.json"
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
    # Google free models: frequent upstream API errors → unreliable calls.
    "google",
]

# Final verification: retry a dummy "hello" call a few times before selecting.
FINAL_VERIFY_RETRIES = 3
FINAL_VERIFY_DELAY = 1.0  # seconds between retries (avoid hammering the proxy)

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
# Scoring (role-aware; impl extracted to model_score.py)
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent))
from model_score import (  # noqa: E402
    enrich_with_aa,
    fetch_aa_models,
    load_aa_cache,
    save_aa_cache,
)
from model_score import rank as _rank_models  # noqa: E402


def _coding_score(model: dict) -> float:
    """Backward-compatible alias — coding role score (see model_score.score)."""
    from model_score import score as _score

    return _score(model, "coding")


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


def _verify_model(model_id: str, key_idx: int = 0) -> tuple[bool, str]:
    """Final-gate verification: send a dummy 'hello' call with retries.

    Free upstreams are flaky, so a single failure doesn't disqualify a model —
    only a model that fails every retry is rejected before final selection.
    """
    last = "no attempts"
    for attempt in range(1, FINAL_VERIFY_RETRIES + 1):
        ok, detail = _test_model(model_id, key_idx)
        if ok:
            return True, detail
        last = detail
        if attempt < FINAL_VERIFY_RETRIES:
            time.sleep(FINAL_VERIFY_DELAY)
    return False, f"failed {FINAL_VERIFY_RETRIES}x (last: {last})"


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


def _extract_org(model_id: str) -> str:
    """Extract upstream org from model ID (e.g. 'google/gemma-4-31b:free' -> 'google')."""
    base = model_id.split(":free")[0]
    return base.split("/")[0] if "/" in base else base


def _select_quality_diverse(models: list[dict], top_n: int) -> list[dict]:
    """Pick top_n: quality-first, then org diversity to keep fallbacks viable.

    Rationale: org diversity avoids one provider's pool exhaustion breaking the
    whole chain, but picking a low-quality model purely for org coverage puts a
    weak fallback in place. So we prefer the single best model overall, then fill
    remaining slots with the best model from each *other* org.
    """
    if not models:
        return []
    ranked = sorted(models, key=lambda m: m["_score"], reverse=True)
    picked = [ranked[0]]
    used_orgs = {_extract_org(ranked[0]["id"])}
    for m in ranked[1:]:
        if len(picked) >= top_n:
            break
        org = _extract_org(m["id"])
        if org in used_orgs:
            continue
        picked.append(m)
        used_orgs.add(org)
    # If org diversity left slots empty, fill with next-best remaining models.
    if len(picked) < top_n:
        for m in ranked:
            if m in picked:
                continue
            picked.append(m)
            if len(picked) >= top_n:
                break
    return picked


def _apply_opencode(models: list[dict], dry_print: bool = False) -> None:
    """Set opencode-rr.json model/chain/provider.models to diverse free models.

    Prefers models from *different* upstream orgs so that if one provider's
    shared pool is exhausted (upstream_provider_shared_pool 429), the fallback
    chain still has working providers from other orgs.

    Before final selection, each candidate is verified with a dummy "hello"
    call (with retries) through the RR proxy — only models that actually
    respond are selected.
    """
    # Group usable models by upstream org, keep best-scoring per org.
    by_org: dict[str, dict] = {}
    for m in models:
        org = _extract_org(m["id"])
        if org not in by_org or m["_score"] > by_org[org]["_score"]:
            by_org[org] = m

    # Order candidates quality-first, then org diversity: the single best model
    # leads, then best-of-each-other-org (avoids a weak model entering the chain
    # purely for org coverage — see _select_quality_diverse rationale).
    diverse = _select_quality_diverse(list(by_org.values()), len(by_org))
    top: list[dict] = []
    print("\n(final verification — dummy hello call, up to 3 retries):")
    for m in diverse:
        if len(top) >= 3:
            break
        ok, detail = _verify_model(m["id"])
        status = "✓" if ok else "✗"
        print(f"  {status} {m['id']:50s} test={detail}")
        if ok:
            top.append(m)

    if not top:
        print("✗ No models passed final verification")
        return

    if dry_print:
        selected_orgs = [(_extract_org(m["id"]), m["id"]) for m in top]
        print("\n(selected top-3 verified — diverse orgs):")
        for i, (org, mid) in enumerate(selected_orgs, 1):
            print(f"  {i}. [{org}] {mid}")
        return

    config = _read_opencode()
    provider = config.setdefault("provider", {}).setdefault("openrouter", {})
    provider.setdefault("models", {})   # ensure key exists (side effect only)

    # Rebuild models dict preserving existing names where possible
    new_models = {}
    for i, m in enumerate(top):
        mid = m["id"]
        new_models[mid] = {"name": f"ORP-{i+1}(free)"}
    provider["models"] = new_models

    # Main model = best free
    config["model"] = f"openrouter/{top[0]['id']}"

    # Fallback chain: top-3 in order (diverse orgs)
    chain = [f"openrouter/{m['id']}" for m in top]
    config.setdefault("experimental", {}).setdefault("modelFallbackChain", {})["chains"] = [chain]

    orgs = "/".join(_extract_org(m["id"]) for m in top)
    _write_opencode(config)
    print(f"✓ opencode-rr.json updated: primary={top[0]['id']} (orgs: {orgs})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _apply_analysis(models: list[dict], top_n: int = 3, dry_run: bool = False) -> int:
    """Write reasoning-role selection to analysis_models.json (not opencode-rr.json).

    Consumed by the separate error-analysis logic (file contract only, no code
    coupling). Quality-first org-diverse chain + local Qwen fallback.
    """
    if not models:
        print("\n✗ No working free reasoning models found — analysis_models.json NOT modified")
        return 1

    top = _select_quality_diverse(models, top_n)

    if dry_run:
        print("\n(dry-run: analysis_models.json not modified)")
        for i, m in enumerate(top, 1):
            print(f"  {i}. [{_extract_org(m['id'])}] {m['id']} score={m['_score']:.1f}")
        return 0

    payload = {
        "schema_version": 1,
        "role": "reasoning",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "primary": f"openrouter/{top[0]['id']}",
        "chain": [f"openrouter/{m['id']}" for m in top],
        "fallback_local": "qwen3-8b",
    }
    tmp = ANALYSIS_CONFIG.with_suffix(".json.tmp")
    ANALYSIS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, ANALYSIS_CONFIG)
    orgs = "/".join(_extract_org(m["id"]) for m in top)
    print(f"✓ analysis_models.json updated: primary={top[0]['id']} (orgs: {orgs})")
    return 0


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
    parser.add_argument("--role", choices=["coding", "reasoning"], default="coding",
                        help="coding → opencode-rr.json (default); reasoning → analysis_models.json")
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
    print(f"  {len(models)} candidate free models (role={args.role})")

    # 2a. Enrich with Artificial Analysis free API (fill missing indices)
    aa = load_aa_cache()
    if not aa:
        aa = fetch_aa_models()
        if aa:
            save_aa_cache(aa)
    enriched = enrich_with_aa(models, aa)
    if enriched:
        print(f"  AA enrichment: +{enriched} models (source: artificialanalysis.ai)")

    # 2b. Score / rank (role-aware)
    _rank_models(models, args.role)

    # 3. Live-test top 15 (don't waste proxy calls on all)
    usable = []
    for m in models[:15]:
        model_id = m["id"]
        ok, detail = _test_model(model_id)
        print(f"  {'✓' if ok else '✗'} {model_id:50s} score={m['_score']:6.1f} test={detail}")
        if ok:
            usable.append(m)
        time.sleep(0.3)  # avoid hammering RR proxy / OpenRouter

    # 4. Apply — pass ALL usable models; _apply_opencode picks diverse orgs.
    if args.role == "reasoning":
        return _apply_analysis(usable, top_n=args.top, dry_run=args.dry_run)
    if args.dry_run:
        print("\n(dry-run: opencode-rr.json not modified)")
        # still show what would be selected
        _apply_opencode(usable, dry_print=True)
        return 0
    if not usable:
        print("\n✗ No working free models found — opencode-rr.json NOT modified")
        return 1
    _apply_opencode(usable)
    return 0


if __name__ == "__main__":
    sys.exit(main())
