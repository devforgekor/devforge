"""Bidirectional refactoring verification — checks __init__.py re-exports, shims,
file references, and stale naming across the codebase.

Path resolution: all module paths are resolved from scripts/ root (NOT from the
__init__.py file's parent dir). This fixes the false-positive bug from the ad-hoc
verification in the prior session.
"""

import ast
import importlib
import re
import sys
from pathlib import Path
from typing import Optional

SCRIPTS = Path("/opt/projects/server/scripts")
LIB = SCRIPTS / "lib"

OK, WARN, ERR = 0, 0, 0


def status(ok=0, warn=0, err=0):
    global OK, WARN, ERR
    OK += ok
    WARN += warn
    ERR += err


def find_init_files(root: Path) -> list[Path]:
    return sorted(root.rglob("__init__.py"))


def parse_re_exports(init_path: Path) -> dict[str, tuple[str, str]]:
    """Parse __init__.py → {exported_name: (module_path, original_name)}.

    For `from .module import Name as Alias`: key=Alias, original=Name.
    For `from .module import Name`: key=Name, original=Name.
    """
    exports = {}
    tree = ast.parse(init_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue

            # Resolve module path
            if node.module.startswith("."):
                pkg_dir = init_path.parent
                rel_path = node.module.lstrip(".")
                depth = len(node.module) - len(rel_path)
                for _ in range(depth):
                    pkg_dir = pkg_dir.parent
                mod_path = pkg_dir / (rel_path.replace(".", "/") + ".py")
            else:
                mod_path = SCRIPTS / (node.module.replace(".", "/") + ".py")

            for alias in node.names:
                name = alias.asname or alias.name
                if name.startswith("_"):
                    continue
                exports[name] = (str(mod_path), alias.name)

    return exports


def parse_module_exports(mod_path: Path) -> set[str]:
    """Get public names defined in a module (functions, classes, UPPER_CASE constants).

    Skips local variable assignments — only reports functions, classes, and
    CONSTANT-style names as true "exports."
    """
    if not mod_path.exists():
        return set()

    try:
        tree = ast.parse(mod_path.read_text())
    except Exception:
        return set()

    # Check __all__ first
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        return {elt.value for elt in node.value.elts
                                if isinstance(elt, ast.Constant)}

    funcs_and_classes = set()
    consts = set()
    aliases: dict[str, str] = {}  # target_name → source_name

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                funcs_and_classes.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    name = target.id
                    if name.startswith("_"):
                        continue
                    if name.isupper() or name[0].isupper():
                        consts.add(name)
                    elif isinstance(node.value, ast.Name):
                        # Alias: collect = collect_phase_summary
                        aliases[name] = node.value.id

    # Include aliases only when they reference another defined export
    all_funcs_and_consts = funcs_and_classes | consts
    exports = all_funcs_and_consts.copy()
    for alias_name, source_name in aliases.items():
        if source_name in all_funcs_and_consts:
            exports.add(alias_name)

    return exports


def verify_init_re_exports() -> bool:
    """Forward: each __init__.py re-export → source module must have that name."""
    print("=" * 72)
    print("1. FORWARD: __init__.py re-exports → source module verification")
    print("=" * 72)

    all_ok = True
    for init_path in find_init_files(LIB):
        exports = parse_re_exports(init_path)
        if not exports:
            continue

        for exported_name, (mod_path, original_name) in exports.items():
            mod_exports = parse_module_exports(Path(mod_path))
            if not mod_exports:
                print(f"  ERR  {init_path.relative_to(SCRIPTS)} → {exported_name}: "
                      f"source module not found or unparseable ({mod_path})")
                all_ok = False
                status(err=1)
                continue

            if original_name not in mod_exports:
                print(f"  ERR  {init_path.relative_to(SCRIPTS)} → {exported_name}: "
                      f"'{original_name}' not in {Path(mod_path).relative_to(SCRIPTS)} "
                      f"(exports: {sorted(mod_exports)[:5]}...)")
                all_ok = False
                status(err=1)
            else:
                status(ok=1)

    if all_ok:
        print("  All re-exports valid")
    return all_ok


def verify_backward_coverage() -> bool:
    """Backward: __all__-declared names → present in __init__.py.

    Only checks modules that explicitly define __all__, since that's the
    author's declared public API. Modules without __all__ are skipped —
    we can't divine author intent for which functions are public vs internal.
    """
    print()
    print("=" * 72)
    print("2. BACKWARD: __all__-declared exports → __init__.py coverage")
    print("=" * 72)

    all_ok = True
    for init_path in find_init_files(LIB):
        parent = init_path.parent
        if parent == LIB:
            continue

        re_exported = set(parse_re_exports(init_path).keys())

        for mod in sorted(parent.glob("*.py")):
            if mod.name.startswith("_") or mod.name == "__init__.py":
                continue

            # Only check modules that declare __all__
            all_names = get_all_names(mod)
            if all_names is None:
                continue  # No __all__ — skip

            for name in sorted(all_names):
                if name not in re_exported:
                    print(f"  WARN {mod.relative_to(SCRIPTS)}: '{name}' in __all__ but not in __init__.py")
                    all_ok = False
                    status(warn=1)

    if all_ok:
        print("  All __all__ exports covered")
    return all_ok


def get_all_names(mod_path: Path) -> Optional[set[str]]:
    """Return __all__ names if defined, None otherwise."""
    try:
        tree = ast.parse(mod_path.read_text())
    except Exception:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        return {elt.value for elt in node.value.elts
                                if isinstance(elt, ast.Constant)}
    return None


def verify_shims() -> bool:
    """Each shim file must re-export from the correct new subpackage."""
    print()
    print("=" * 72)
    print("3. SHIMS: backward-compat shim verification")
    print("=" * 72)

    # Discovered from actual file contents — each flat lib/*.py that starts with
    # "Backward-compat shim" must import from its target subpackage.
    SHIM_TARGETS = {
        "agents.py": "lib.tracking.agent_names",
        "crypto.py": "lib.auth.api_key_cipher",
        "estimator.py": "lib.llm.rate_estimator",
        "key_rotator.py": "lib.auth.key_rotator",
        "parser_aider.py": "lib.parsers.aider",
        "parser_claude.py": "lib.parsers.claude",
        "parser_copilot.py": "lib.parsers.copilot",
        "parser_gemini.py": "lib.parsers.gemini",
        "phase_tracker.py": "lib.tracking.phase_tracker",
        "refs.py": "lib.tracking.dependency_tracker",
        "search_manager.py": "lib.search.manager",
        "sys_checks.py": "lib.infra.health_checks",
    }

    all_ok = True
    for shim_name, expected_pkg in SHIM_TARGETS.items():
        shim_path = LIB / shim_name
        if not shim_path.exists():
            print(f"  ERR  {shim_name}: not found")
            all_ok = False
            status(err=1)
            continue

        tree = ast.parse(shim_path.read_text())
        imports_from = [node.module for node in ast.walk(tree)
                        if isinstance(node, ast.ImportFrom) and node.module]

        if expected_pkg in imports_from:
            status(ok=1)
        else:
            print(f"  ERR  {shim_name}: expected import from {expected_pkg}, "
                  f"got {imports_from}")
            all_ok = False
            status(err=1)

    if all_ok:
        print(f"  All {len(SHIM_TARGETS)} shims valid")
    return all_ok


def verify_file_references() -> bool:
    """Check that referenced files exist (CLAUDE.yaml, timer-registry.yaml)."""
    print()
    print("=" * 72)
    print("4. FILE REFERENCES: path existence check")
    print("=" * 72)

    server_root = Path("/opt/projects/server")
    all_ok = True

    for yaml_file in [server_root / "CLAUDE.yaml",
                      server_root / "docs" / "timer-registry.yaml"]:
        if not yaml_file.exists():
            continue
        for line in yaml_file.read_text().split("\n"):
            for m in re.finditer(r"/opt/projects/server/[^\s,\"'\n]+", line):
                path_str = m.group(0)
                if not Path(path_str).exists():
                    print(f"  ERR  {yaml_file.name}: {path_str}")
                    all_ok = False
                    status(err=1)

    if all_ok:
        print("  All file references valid")
    status(ok=1)
    return all_ok


def verify_old_name_cleanup() -> bool:
    """Check that old filenames don't appear in non-historical files."""
    print()
    print("=" * 72)
    print("5. OLD NAMES: stale reference scan")
    print("=" * 72)

    OLD_TO_NEW = {
        "gen_server_state.py": "state_collector/main.py",
        "session_start.py": "session_context.py",
        "session_guard.py": "auto_commit_guard.py",
        "patch_copilot_elf.py": "patch_elf_note.py",
        "swap_mode.sh": "swap_llm_mode.sh",
    }

    # Only scan ACTIVE code/config files, not historical documents.
    # Historical docs (handover, changelog, docs/*, archive) naturally
    # contain old names — they're records of what was.
    ACTIVE_SUFFIXES = {".py", ".sh", ".container", ".json"}
    ACTIVE_YAML = {"CLAUDE.yaml", "blueprint.yaml", "tasks.yaml", "timer-registry.yaml",
                   "phases.md", "design.md"}

    EXCLUDE_DIRS = {"changelog", "_archive", ".git", "__pycache__", "docs",
                    "batch_results", "handover.yaml", "changelog.yaml",
                    "changelog_archive.yaml", "code-as-documentation-review.md",
                    "verify_refactoring.py"}

    # Lines that are clearly recording what WAS deployed (historical record),
    # not referencing a file that needs updating:
    HISTORICAL_CONTEXT = {"deployed", "bootstrap", "initial"}

    server_root = Path("/opt/projects/server")
    all_ok = True

    for old_name, new_name in OLD_TO_NEW.items():
        for f in server_root.rglob("*"):
            f_str = str(f)
            if any(e in f_str for e in EXCLUDE_DIRS):
                continue

            is_active = (f.is_file() and (
                f.suffix in ACTIVE_SUFFIXES or f.name in ACTIVE_YAML
            ))
            if not is_active:
                continue

            try:
                content = f.read_text()
            except Exception:
                continue
            if old_name in content:
                for i, line in enumerate(content.split("\n")):
                    if old_name in line:
                        if "→" in line or "renamed" in line.lower():
                            continue
                        if old_name in line and new_name in line:
                            continue
                        if f.name == "CLAUDE.yaml" and old_name == "gen_server_state.py":
                            continue
                        # Skip lines with historical-context keywords
                        if any(kw in line.lower() for kw in HISTORICAL_CONTEXT):
                            continue
                        rel = f.relative_to(server_root)
                        print(f"  WARN {rel}:{i+1}: stale ref to '{old_name}'")
                        all_ok = False
                        status(warn=1)

    if all_ok:
        print("  No stale old-name references")
    return all_ok


def verify_runtime_imports() -> bool:
    """Import every lib subpackage and verify it doesn't crash."""
    print()
    print("=" * 72)
    print("6. RUNTIME: import every lib subpackage")
    print("=" * 72)

    sys.path.insert(0, str(SCRIPTS))

    pkgs = []
    for init_path in find_init_files(LIB):
        rel = init_path.parent.relative_to(SCRIPTS)
        pkg_name = str(rel).replace("/", ".")
        if pkg_name == "lib":
            continue
        pkgs.append(pkg_name)

    all_ok = True
    for pkg in sorted(pkgs):
        try:
            importlib.import_module(pkg)
            status(ok=1)
        except Exception as e:
            print(f"  ERR  import {pkg}: {e}")
            all_ok = False
            status(err=1)

    if all_ok:
        print(f"  All {len(pkgs)} subpackages importable")
    return all_ok


# ── Main ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    results = [
        verify_init_re_exports(),
        verify_backward_coverage(),
        verify_shims(),
        verify_file_references(),
        verify_old_name_cleanup(),
        verify_runtime_imports(),
    ]

    print()
    print("=" * 72)
    print(f"RESULTS: {OK} ok, {WARN} warnings, {ERR} errors")
    if all(results):
        print("ALL CHECKS PASSED")
        sys.exit(0)
    else:
        print("SOME CHECKS FAILED — review output above")
        sys.exit(1)
