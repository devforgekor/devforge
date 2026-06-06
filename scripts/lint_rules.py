#!/usr/bin/env python3
# Status: production
# Path: cli.py lint, cli.py status --json
"""DevForge Rule Linter — coding rules enforced by machine, not documents."""

import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPTS_DIR = Path("/opt/projects/server/scripts")
LIB_DIR = SCRIPTS_DIR / "lib"

# ═══════════════════════════════════════════════════════════════
# Rule definitions
# ═══════════════════════════════════════════════════════════════

# Files where model names in identifiers are legitimate (infrastructure config, model registry)
MODEL_NAME_OK_FILES = {
    "scripts/debate_data.py",     # MODEL_REGISTRY — model names are data, not identifiers
    "scripts/lib/infra/azure_spot.py",      # Azure Spot VM config — model names are infrastructure refs
    "scripts/lib/llm_client.py",  # LLM client — model names are API params
    "scripts/lint_rules.py",      # Linter — contains regex patterns with model names as example patterns
}

# Model size patterns that must not appear in identifiers
MODEL_SIZE_PATTERN = re.compile(
    r"\b(3b|4b|7b|14b|27b|30b|32b)\b", re.IGNORECASE
)

# Model brand names that must not appear in identifiers
MODEL_BRAND_PATTERN = re.compile(
    r"\b(qwen|codestral|selene|nemotron|phi[_-]?4|llama[_-]?3)\b", re.IGNORECASE
)

# Banned single-letter role abbreviations (standalone identifiers only)
BANNED_SINGLE_LETTER = {"P", "R", "J"}

# Banned function/variable names (from naming audit)
BANNED_NAMES = {
    "ts": "utc_timestamp",
    "_est_tok": "estimate_token_count",
    "esc_sql": "escape_sql_string",
    "j_res": "judge_result",
    "llm_r": "llm_response",
    "py_res": "python_verified_result",
    "ok_a": "pod_a_ready",
    "ok_b": "pod_b_ready",
    "max_tok": "max_tokens",
    "mcp_pt": "mcp_prompt_tokens",
    "mcp_gt": "mcp_gen_tokens",
    "mcp_em": "mcp_elapsed_ms",
    "fb_tag": "feedback_tag",
    "fb_prj": "feedback_cycle",
    "fb_nv": "feedback_nv",
    "fb_rv": "feedback_review",
    "fb_pf": "feedback_pf",
    "inv_sev": "inverted_severity",
    "sev_dist": "severity_distribution",
    "sev_name": "severity_name",
    "no_src": "without_source",
    "t_all": "total_count",
    "f_list": "finding_list",
    "day_r": "day_reviewer",
    "day_p": "day_proposer",
    "day_j": "day_judge",
    "prj_p": "phase_proposer",
    "prj_j": "phase_judge",
    "p_r": "proposer_result",
    "r_r": "reflector_result",
    "j_r": "judge_result",
}


def find_python_files(base: Path) -> List[Path]:
    """Find all .py files, excluding _archive and __pycache__."""
    files = []
    for f in base.rglob("*.py"):
        if "_archive" in f.parts or "__pycache__" in f.parts:
            continue
        files.append(f)
    return sorted(files)


def check_status_header(filepath: Path) -> List[Dict]:
    """Rule 1: Every .py file must have '# Status:' header."""
    if filepath.name == "__init__.py" and filepath.stat().st_size < 100:
        return []  # empty __init__.py doesn't need header
    try:
        content = filepath.read_text()
    except Exception:
        return []
    violations = []
    if not re.search(r"^# Status:", content, re.MULTILINE):
        violations.append({
            "rule": "status-header",
            "severity": "P0",
            "message": f"Missing '# Status:' header",
            "file": str(filepath.relative_to(SCRIPTS_DIR.parent)),
        })
    return violations


def check_utcnow(filepath: Path) -> List[Dict]:
    """Rule 3: No utcnow() usage."""
    if filepath.name == "lint_rules.py":
        return []  # self-check — rule messages contain "utcnow()" as example text
    try:
        content = filepath.read_text()
    except Exception:
        return []
    violations = []
    lines = content.split("\n")
    for i, line in enumerate(lines, 1):
        if "utcnow()" in line and not line.strip().startswith("#"):
            violations.append({
                "rule": "no-utcnow",
                "severity": "P0",
                "message": "Use datetime.now(timezone.utc) instead of utcnow()",
                "file": str(filepath.relative_to(SCRIPTS_DIR.parent)),
                "line": i,
            })
    return violations


def check_bare_except(filepath: Path) -> List[Dict]:
    """Rule 4: No bare except: statements."""
    try:
        content = filepath.read_text()
    except Exception:
        return []
    violations = []
    for i, line in enumerate(content.split("\n"), 1):
        stripped = line.strip()
        if stripped == "except:" or stripped.startswith("except:"):
            # false positive: not followed by another except
            if "except:" == stripped or not stripped.startswith("except: "):
                violations.append({
                    "rule": "no-bare-except",
                    "severity": "P0",
                    "message": "Use 'except Exception:' instead of bare 'except:'",
                    "file": str(filepath.relative_to(SCRIPTS_DIR.parent)),
                    "line": i,
                })
    return violations


def _extract_identifiers_from_code(content: str) -> List[Tuple[str, int]]:
    """Use AST to extract function names, class names, and variable assignments."""
    identifiers = []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return identifiers

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            identifiers.append((node.name, node.lineno))
        elif isinstance(node, ast.ClassDef):
            identifiers.append((node.name, node.lineno))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    identifiers.append((target.id, target.lineno))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            identifiers.append((node.target.id, node.lineno))
    return identifiers


def _check_name_parts(name: str, filepath: Path, lineno: int, relpath: str) -> List[Dict]:
    """Split identifier by underscore, check each part for model sizes/brands."""
    violations = []
    parts = name.split("_")
    for part in parts:
        if MODEL_SIZE_PATTERN.fullmatch(part):
            violations.append({
                "rule": "no-model-in-identifier",
                "severity": "P1",
                "message": f"'{name}' contains model size '{part}'. Use role name instead.",
                "file": relpath,
                "line": lineno,
            })
        if MODEL_BRAND_PATTERN.fullmatch(part):
            violations.append({
                "rule": "no-model-in-identifier",
                "severity": "P1",
                "message": f"'{name}' contains model brand '{part}'. Use role name instead.",
                "file": relpath,
                "line": lineno,
            })
    return violations


def _extract_all_identifiers(content: str) -> List[Tuple[str, int]]:
    """Extract identifiers from AST (top-level) + regex (local vars/params inside functions)."""
    identifiers = set()  # deduplicate

    # AST: top-level defs, classes, assignments
    try:
        tree = ast.parse(content)
    except SyntaxError:
        tree = None

    if tree:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                identifiers.add((node.name, node.lineno))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        identifiers.add((target.id, target.lineno))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                identifiers.add((node.target.id, node.lineno))

    # Regex: catch local variables (snake_case identifiers with model sizes)
    # Match identifiers that contain model size patterns: run_32b_4stage, v27b, etc.
    for i, line in enumerate(content.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
            continue
        # Find all snake_case identifiers
        for match in re.finditer(r"\b([a-z][a-z0-9]*(?:_[a-z][a-z0-9]*)*)\b", line):
            name = match.group(1)
            # Only check if it might contain a model pattern
            has_model = False
            for part in name.split("_"):
                if MODEL_SIZE_PATTERN.fullmatch(part) or MODEL_BRAND_PATTERN.fullmatch(part):
                    has_model = True
                    break
            if has_model:
                identifiers.add((name, i))

    return sorted(identifiers, key=lambda x: x[1])


def check_identifier_naming(filepath: Path) -> List[Dict]:
    """Rules 2+5+6: No model names, no banned abbreviations in identifiers."""
    relpath = str(filepath.relative_to(SCRIPTS_DIR.parent))
    violations = []

    # Skip files where model names in identifiers are legitimate
    skip_model_check = relpath in MODEL_NAME_OK_FILES

    # Check filename
    stem = filepath.stem.lower()
    for part in stem.split("_"):
        if MODEL_SIZE_PATTERN.fullmatch(part):
            violations.append({
                "rule": "no-model-in-filename",
                "severity": "P1",
                "message": f"Filename contains model size '{part}': '{filepath.name}'",
                "file": relpath,
            })
        if MODEL_BRAND_PATTERN.fullmatch(part):
            violations.append({
                "rule": "no-model-in-filename",
                "severity": "P1",
                "message": f"Filename contains model brand '{part}': '{filepath.name}'",
                "file": relpath,
            })

    # Check identifiers
    try:
        content = filepath.read_text()
    except Exception:
        return violations

    identifiers = _extract_all_identifiers(content)

    for name, lineno in identifiers:
        # Check model size/brand in identifier parts (skip for config files)
        if not skip_model_check:
            violations.extend(_check_name_parts(name, filepath, lineno, relpath))

        # Check banned abbreviations (full name match)
        if name in BANNED_NAMES:
            violations.append({
                "rule": "no-abbreviation",
                "severity": "P2",
                "message": f"'{name}' is a banned abbreviation. Rename to '{BANNED_NAMES[name]}'.",
                "file": relpath,
                "line": lineno,
            })

    return violations


# Korean character range (Hangul syllables, Jamo)
_KOREAN_RE = re.compile(r"[가-힯ᄀ-ᇿ㄰-㆏]")


def check_doc_language(filepath: Path) -> List[Dict]:
    """Rule 5: Machine-readable YAML docs must be English only."""
    violations = []
    if filepath.suffix.lower() not in (".yaml", ".yml"):
        return violations
    if "_archive" in filepath.parts:
        return violations
    try:
        content = filepath.read_text()
    except Exception:
        return violations

    lines = content.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        korean_chars = _KOREAN_RE.findall(line)
        if korean_chars:
            relpath = str(filepath.relative_to(SCRIPTS_DIR.parent))
            snippet = line.strip()[:80]
            violations.append({
                "rule": "english-only-docs",
                "severity": "P1",
                "message": f"Korean chars in machine-readable doc: '{snippet}' ({len(korean_chars)} chars)",
                "file": relpath,
                "line": i,
            })
    return violations


def run_all_checks(files: Optional[List[Path]] = None) -> Dict:
    """Run all rule checks, return structured results."""
    if files is None:
        files = find_python_files(SCRIPTS_DIR)

    all_violations = []
    checks = {
        "status-header": check_status_header,
        "no-utcnow": check_utcnow,
        "no-bare-except": check_bare_except,
        "naming": check_identifier_naming,
    }

    for filepath in files:
        for check_name, check_fn in checks.items():
            violations = check_fn(filepath)
            all_violations.extend(violations)

    # Also check YAML docs for language
    yaml_dir = SCRIPTS_DIR.parent / "docs"
    if yaml_dir.exists():
        for yf in yaml_dir.rglob("*.yaml"):
            if "_archive" in yf.parts:
                continue
            all_violations.extend(check_doc_language(yf))
        for yf in yaml_dir.rglob("*.yml"):
            if "_archive" in yf.parts:
                continue
            all_violations.extend(check_doc_language(yf))

    total_yaml = len([1 for yf in yaml_dir.rglob("*.yaml") if "_archive" not in yf.parts]) if yaml_dir.exists() else 0

    # Summarize by severity
    p0 = [v for v in all_violations if v["severity"] == "P0"]
    p1 = [v for v in all_violations if v["severity"] == "P1"]
    p2 = [v for v in all_violations if v["severity"] == "P2"]

    return {
        "total_files": len(files) + total_yaml,
        "violations_total": len(all_violations),
        "violations_by_severity": {
            "P0": len(p0),
            "P1": len(p1),
            "P2": len(p2),
        },
        "violations": all_violations,
        "passed": len(all_violations) == 0,
        "status": "CLEAN" if len(all_violations) == 0 else f"VIOLATIONS: {len(p0)}P0 {len(p1)}P1 {len(p2)}P2",
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="DevForge Rule Linter")
    parser.add_argument("--json", "-j", action="store_true", help="Machine-readable JSON output")
    parser.add_argument("--files", nargs="*", help="Specific files to check (default: all scripts/)")
    args = parser.parse_args()

    if args.files:
        files = [Path(f) for f in args.files]
    else:
        files = find_python_files(SCRIPTS_DIR)

    result = run_all_checks(files)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        if result["passed"]:
            print("✅ All rules passed — no violations found.")
        else:
            print(f"❌ {result['status']}")
            for v in result["violations"]:
                loc = f"{v['file']}:{v.get('line', '')}" if v.get("line") else v["file"]
                print(f"  [{v['severity']}] {v['rule']}: {v['message']}")
                print(f"        at {loc}")

    sys.exit(0 if result["passed"] else 1)
