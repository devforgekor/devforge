#!/usr/bin/env python3
"""scripts/gen_index | docstring -> index.yaml generator + validator | gen_index()"""
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER = "\n".join([
    "# MACHINE-READABLE INDEX. DO NOT EDIT MANUALLY.",
    "# FLOW: 1. Search keyword -> 2. Read full .py -> 3. Check recipes/ -> 4. Compose",
    "",
])

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_docstring(filepath: Path, raw: str) -> list[str]:
    errors: list[str] = []
    tag = str(filepath.relative_to(ROOT))

    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3:
        errors.append(f"{tag}: need >=3 pipe-separated fields, got {len(parts)}")
        return errors

    # field-0: declared path must match actual relative path
    declared_path = parts[0]
    actual = str(filepath.relative_to(ROOT).with_suffix("")).replace("\\", "/")
    if declared_path != actual:
        errors.append(f"{tag}: path mismatch '{declared_path}' != '{actual}'")

    # field-1: description (must be non-empty ASCII)
    desc = parts[1].strip()
    if not desc:
        errors.append(f"{tag}: description is empty")
    elif not all(ord(c) < 128 for c in desc.replace(" ","").replace("-","").replace("/","")):
        errors.append(f"{tag}: use English only in description")

    # last field: API signatures (must contain paren-pairs)
    api = parts[-1]
    if not re.search(r'\w+\(', api):
        errors.append(f"{tag}: last field must contain at least one api() signature")

    # optional fields: needs:, uses:, config:
    valid_tags = ("needs:", "uses:", "config:")
    for p in parts[2:-1]:
        if not p.startswith(valid_tags):
            errors.append(f"{tag}: unknown field '{p[:20]}...' — use needs:/uses:/config:")

    return errors

# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def extract_docstring(filepath: Path) -> str | None:
    text = filepath.read_text(encoding="utf-8")
    m = re.search(r'^\s*"""(.+?)"""', text, re.DOTALL | re.MULTILINE)
    if not m:
        return None
    raw = " ".join(line.strip() for line in m.group(1).splitlines() if line.strip())
    return raw or None

# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------
def gen_index() -> None:
    index_path = ROOT / "index.yaml"
    py_files: list[Path] = []
    for d in ("core", "domain", "azure", "oci", "deploy"):
        candidate = ROOT / d
        if candidate.is_dir():
            py_files.extend(sorted(candidate.rglob("*.py")))

    all_errors: list[str] = []
    entries: list[str] = []

    for py_file in py_files:
        raw = extract_docstring(py_file)
        if raw is None:
            all_errors.append(f"{py_file.relative_to(ROOT)}: missing docstring")
            continue
        errs = validate_docstring(py_file, raw)
        if errs:
            all_errors.extend(errs)
        entries.append(f"  - {raw}")

    if all_errors:
        print(f"[ERROR] {len(all_errors)} docstring issue(s):", file=sys.stderr)
        for e in sorted(set(all_errors)):
            print(f"  - {e}", file=sys.stderr)

    content = HEADER + "modules:\n" + "\n".join(sorted(entries)) + "\n"

    old = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
    if content != old:
        index_path.write_text(content, encoding="utf-8")
        if not all_errors:
            print(f"[OK] index.yaml updated ({len(entries)} modules)", file=sys.stderr)
    else:
        print("[OK] index.yaml unchanged", file=sys.stderr)

    sys.exit(1 if all_errors else 0)

if __name__ == "__main__":
    gen_index()
