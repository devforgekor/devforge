#!/usr/bin/env python3
# Status: production
"""DevForge Rule Linter — coding rules enforced by machine, not documents."""

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from lint_rules.checks import (
    check_status_header, check_utcnow, check_bare_except,
    check_identifier_naming, check_doc_language,
)

SCRIPTS_DIR = Path("/opt/projects/server/scripts")
LIB_DIR = SCRIPTS_DIR / "lib"


def find_python_files(base: Path) -> List[Path]:
    files = []
    for f in base.rglob("*.py"):
        if "_archive" in f.parts or "__pycache__" in f.parts:
            continue
        files.append(f)
    return sorted(files)


def run_all_checks(files: Optional[List[Path]] = None) -> Dict:
    if files is None:
        files = find_python_files(SCRIPTS_DIR)

    all_violations = []
    relpath_root = SCRIPTS_DIR.parent
    checks = {
        "status-header": check_status_header,
        "no-utcnow": check_utcnow,
        "no-bare-except": check_bare_except,
        "naming": check_identifier_naming,
    }

    for filepath in files:
        for check_fn in checks.values():
            violations = check_fn(filepath, relpath_root)
            all_violations.extend(violations)

    yaml_dir = SCRIPTS_DIR.parent / "docs"
    if yaml_dir.exists():
        for yf in yaml_dir.rglob("*.yaml"):
            if "_archive" in yf.parts:
                continue
            all_violations.extend(check_doc_language(yf, relpath_root))
        for yf in yaml_dir.rglob("*.yml"):
            if "_archive" in yf.parts:
                continue
            all_violations.extend(check_doc_language(yf, relpath_root))

    total_yaml = len([1 for yf in yaml_dir.rglob("*.yaml") if "_archive" not in yf.parts]) if yaml_dir.exists() else 0

    p0 = [v for v in all_violations if v["severity"] == "P0"]
    p1 = [v for v in all_violations if v["severity"] == "P1"]
    p2 = [v for v in all_violations if v["severity"] == "P2"]

    return {
        "total_files": len(files) + total_yaml,
        "violations_total": len(all_violations),
        "violations_by_severity": {"P0": len(p0), "P1": len(p1), "P2": len(p2)},
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
            print("\u2705 All rules passed \u2014 no violations found.")
        else:
            print(f"\u274c {result['status']}")
            for v in result["violations"]:
                loc = f"{v['file']}:{v.get('line', '')}" if v.get("line") else v["file"]
                print(f"  [{v['severity']}] {v['rule']}: {v['message']}")
                print(f"        at {loc}")

    sys.exit(0 if result["passed"] else 1)
