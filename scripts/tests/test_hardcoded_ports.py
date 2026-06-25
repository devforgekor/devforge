#!/usr/bin/env python3
# Status: experimental
# Path: none — manual validation of hardcoded port removal
"""Validate no hardcoded 127.0.0.1:808{0-4} ports remain in modified files.

Checks:
  1. No file references 127.0.0.1:808[0-4] except infra configs (publish, listen)
  2. Every file that uses MODEL_REGISTRY has it imported
  3. All 20 files compile
  4. Runtime import succeeds for all modified lib modules
"""

import ast
import py_compile
import sys
import os

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

FAIL_FAST = os.environ.get("FAIL_FAST", "1") == "1"

ALLOWED_PATTERNS = [
    "publish",      # podman --publish (infra config)
    "LISTEN",       # listen address (slack.py, blob_explorer.py)
    "listen",       # listen address
    "reverse_proxy", # caddy config
    "_archive",     # archived files
]

MODIFIED_FILES = [
    # Production
    "lib/llm_client.py",
    "pipelines/extract.py",
    "pipelines/embed_batch.py",
    "pipelines/mcp_cosine.py",
    "lib/search/hybrid.py",
    "lib/debate/debate_llm.py",
    "lib/code_mod/shared.py",
    "cli.py",
    "observer.py",
    "activity_summarizer.py",
    "notice/lib/bot_processor.py",
    # Tests
    "tests/pipeline_preverify_test.py",
    "tests/day_model_test.py",
    "tests/day_verify_retry.py",
    "tests/day_verify_compare.py",
    "tests/day_model_test_direct.py",
    "tests/prj_ground_truth_test.py",
    "tests/prj_model_test.py",
    "tests/model_comparison_test.py",
    "tests/test_night_prj_eval.py",
]

errors = []

def check(condition: bool, msg: str):
    if not condition:
        errors.append(msg)
        print(f"  FAIL: {msg}")
        if FAIL_FAST:
            sys.exit(1)
    else:
        print(f"  PASS: {msg}")

def main():
    global errors
    print("=" * 60)
    print("  Hardcoded Port Validation Test")
    print("=" * 60)

    # ── 1. Syntax check ──
    print(f"\n--- 1. Syntax check ({len(MODIFIED_FILES)} files) ---")
    for rel in MODIFIED_FILES:
        path = os.path.join(SCRIPTS_DIR, rel)
        if not os.path.exists(path):
            check(False, f"{rel} — file not found")
            continue
        try:
            py_compile.compile(path, doraise=True)
            check(True, f"{rel} — compiles")
        except py_compile.PyCompileError as e:
            check(False, f"{rel} — compile error: {e}")

    # ── 2. No hardcoded 127.0.0.1:808X (except allowed patterns) ──
    print(f"\n--- 2. Hardcoded port scan (all production + tests) ---")
    import re
    for rel in MODIFIED_FILES:
        path = os.path.join(SCRIPTS_DIR, rel)
        with open(path) as f:
            lines = f.readlines()
        bad_lines = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # Skip comments/docs
            if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'") or stripped.startswith("//"):
                continue
            if "127.0.0.1:808" not in stripped:
                continue
            # Check if this is an allowed pattern
            if any(p in stripped for p in ALLOWED_PATTERNS):
                continue
            # Check if it's a MODEL_REGISTRY reference (dynamic port)
            if "MODEL_REGISTRY" in stripped or "MODEL_REGISTRY" in (lines[i-2] if i >= 2 else ""):
                # Allow f-strings with MODEL_REGISTRY
                if "{MODEL_REGISTRY" not in stripped:
                    bad_lines.append((i, stripped))
            else:
                bad_lines.append((i, stripped))
        if bad_lines:
            for ln, text in bad_lines:
                check(False, f"{rel}:{ln} — hardcoded port: {text.strip()}")
        else:
            check(True, f"{rel} — no hardcoded ports")

    # ── 3. MODEL_REGISTRY import validation ──
    print(f"\n--- 3. MODEL_REGISTRY import consistency ---")
    for rel in MODIFIED_FILES:
        if rel == "lib/llm_client.py":
            continue  # defines MODEL_REGISTRY, doesn't import it
        path = os.path.join(SCRIPTS_DIR, rel)
        with open(path) as f:
            tree = ast.parse(f.read())
        
        has_import = False
        has_ref = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names]
                mod = getattr(node, 'module', '')
                if 'lib.llm_client' in mod or 'lib.llm_client' in names or 'MODEL_REGISTRY' in names:
                    has_import = True
            if isinstance(node, ast.Subscript):
                try:
                    if isinstance(node.value, ast.Name) and node.value.id == 'MODEL_REGISTRY':
                        has_ref = True
                except: pass

        if has_ref and not has_import:
            check(False, f"{rel} — uses MODEL_REGISTRY but no import")
        elif has_import and not has_ref:
            # Some files may import it but not use it in AST (e.g., via f-strings that AST can't detect)
            with open(path) as f:
                content = f.read()
            if 'MODEL_REGISTRY' in content and not has_ref:
                # AST didn't detect it — check if it's used via f-string or other dynamic access
                if not has_import:
                    check(False, f"{rel} — MODEL_REGISTRY in content but no import")
        else:
            check(True, f"{rel} — import/usage consistent")

    # ── 4. Runtime import check ──
    print(f"\n--- 4. Runtime import check ---")
    lib_modules = [
        "lib.llm_client",
        "lib.debate.debate_llm",
        "lib.search.hybrid",
        "lib.code_mod.shared",
    ]
    for mod_name in lib_modules:
        try:
            __import__(mod_name)
            # Verify key values
            from lib.llm_client import MODEL_REGISTRY
            check(True, f"{mod_name} — imported, keys: {list(MODEL_REGISTRY.keys())[:6]}...")
        except Exception as e:
            check(False, f"{mod_name} — import error: {e}")

    # ── 5. Expected port resolution ──
    print(f"\n--- 5. Port resolution check ---")
    from lib.llm_client import MODEL_REGISTRY
    expected = {
        "reranker": 8080,
        "embeder": 8081,
        "proposer": 8081,
        "extractor": 8082,
        "reviewer": 8083,
        "verifier": 8084,
    }
    for key, expected_port in expected.items():
        val = MODEL_REGISTRY.get(key)
        if val is None:
            check(False, f"MODEL_REGISTRY missing key: {key}")
        elif val.get("port") == expected_port:
            check(True, f"MODEL_REGISTRY[{key!r}].port = {expected_port}")
        else:
            check(False, f"MODEL_REGISTRY[{key!r}].port = {val.get('port')} (expected {expected_port})")

    # ── Summary ──
    total = len(MODIFIED_FILES)
    print(f"\n{'=' * 60}")
    if not errors:
        print(f"  ALL {total} CHECKS PASSED ({total} files × 5 categories)")
    else:
        print(f"  {len(errors)} FAILURES")
        for e in errors:
            print(f"    - {e}")
    print(f"{'=' * 60}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
