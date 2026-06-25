#!/usr/bin/env python3
# Status: experimental
# Path: tests/test_all.py — unified test entry point
"""DevForge Test Suite — 단일 진입점.
도메인별 테스트 파일을 그대로 __main__으로 실행.
서브-플래그(--27b, --3b, --faith 등)는 각 파일이 직접 처리.

Usage:
  python3 tests/test_all.py --all
  python3 tests/test_all.py --night --27b
  python3 tests/test_all.py --day --faith
  python3 tests/test_all.py --experiment
  python3 tests/test_all.py --lib
  python3 tests/test_all.py          # interactive menu
"""

import os, sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "scripts"))
sys.path.insert(0, TESTS_DIR)

DOMAINS = [
    ("--day",   "test_day_pipeline"),
    ("--night", "test_night_cycle"),
    ("--experiment", "test_experiment"),
    ("--lib",   "test_lib"),
]
MENU = [
    ("all",       "Day-Night All Pipeline Test"),
    ("--day",     "Day Pipeline Test"),
    ("--night",   "Night Pipeline Test"),
    ("--experiment", "Experiment Test"),
    ("--lib",     "Library Test"),
]
KNOWN = {f for f, _ in DOMAINS} | {"--all", "--list", "--help", "-h"}


def _run(mod_name):
    path = os.path.join(TESTS_DIR, mod_name + ".py")
    sub_argv = [a for a in sys.argv[1:] if a not in KNOWN]
    saved = sys.argv[:]
    try:
        sys.argv = [path] + sub_argv
        with open(path) as f:
            exec(f.read(), {"__name__": "__main__", "__file__": path})
    except Exception as e:
        print(f"  └─ FAILED: {type(e).__name__}: {e}")
    finally:
        sys.argv = saved


def show_menu():
    print("=" * 40)
    print("  DevForge Test Suite")
    print("=" * 40)
    for i, (_, title) in enumerate(MENU, 1):
        print(f"  {i}. {title}")
    print("  0. Exit")
    print("=" * 40)


def _cli_targets():
    if "--all" in sys.argv:
        return [m for _, m in DOMAINS]
    return [m for f, m in DOMAINS if f in sys.argv]


def main():
    flags = [a for a in sys.argv[1:] if a.startswith("--")]

    # ── interactive ──
    if not flags:
        while True:
            show_menu()
            try:
                choice = input("Select (1-5, 0=exit): ").strip()
            except (EOFError, KeyboardInterrupt):
                print(); return
            if choice == "0":
                return
            if choice == "1":
                for _, mod in DOMAINS:
                    print(f"\n═══ {mod} ═══")
                    _run(mod)
                    print()
            elif choice.isdigit() and 2 <= int(choice) <= 5:
                _, mod = DOMAINS[int(choice) - 2]
                print(f"\n═══ {mod} ═══")
                _run(mod)
                print()
            else:
                print("Invalid.")
        return

    # ── CLI ──
    targets = _cli_targets()
    if not targets:
        print(__doc__); return
    for mod in targets:
        print(f"\n═══ {mod} ═══")
        _run(mod)
        print()


if __name__ == "__main__":
    main()
