#!/usr/bin/env python3
"""scripts/stats | module reference counter: scan builds/ → classify hot/warm/cold → _stats.json | stats(),archive()"""

import sys
import json
import re
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent


def _all_modules():
    """Extract module paths from index.yaml."""
    lines = (ROOT / "index.yaml").read_text().splitlines()
    modules = []
    for line in lines:
        m = re.match(r'  - ([^ ]+) ', line)
        if m:
            modules.append(m.group(1))
    return modules


def _used_modules():
    """Count module references from builds/*.json. Normalizes import paths to file paths."""
    builds_dir = ROOT / "builds"
    counter = Counter()
    for f in sorted(builds_dir.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            record = json.loads(f.read_text())
            for mod in record.get("modules_used", []):
                counter[mod.replace(".", "/")] += 1
        except (json.JSONDecodeError, KeyError):
            pass
    return counter


def _classify(all_modules, used_counter):
    hot, warm, cold = [], [], []
    for mod in sorted(all_modules):
        count = used_counter.get(mod, 0)
        entry = (mod, count)
        if count >= 3:
            hot.append(entry)
        elif count >= 1:
            warm.append(entry)
        else:
            cold.append(entry)
    return hot, warm, cold


def _write_stats(hot, warm, cold):
    data = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "hot": [{"module": m, "used": c} for m, c in hot],
        "warm": [{"module": m, "used": c} for m, c in warm],
        "cold": [{"module": m, "used": c} for m, c in cold],
    }
    path = ROOT / "builds" / "_stats.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return path


def _show(hot, warm, cold):
    def bar(count):
        return "#" * min(count, 10)

    print(f"{'HOT':>5} (>=3): {len(hot)} modules")
    for m, c in hot:
        print(f"  {m:45s} {bar(c):10s} ({c})")

    print(f"\n{'WARM':>5} (1-2): {len(warm)} modules")
    for m, c in warm:
        print(f"  {m:45s} {bar(c):10s} ({c})")

    print(f"\n{'COLD':>5} (0):   {len(cold)} modules")
    if cold:
        for m, _ in cold[:10]:
            print(f"  {m}")
        if len(cold) > 10:
            print(f"  ... and {len(cold) - 10} more")


def stats():
    all_modules = _all_modules()
    used = _used_modules()
    hot, warm, cold = _classify(all_modules, used)
    _show(hot, warm, cold)
    path = _write_stats(hot, warm, cold)
    print(f"\n[STATS] {path.relative_to(ROOT)}")


def archive():
    data_path = ROOT / "builds" / "_stats.json"
    if not data_path.is_file():
        print("ERROR: run 'python scripts/stats.py' first", file=sys.stderr)
        sys.exit(1)
    data = json.loads(data_path.read_text())
    cold = data.get("cold", [])
    if not cold:
        print("No cold modules to archive.")
        return

    print("Cold modules (0 references):")
    for i, entry in enumerate(cold):
        print(f"  [{i}] {entry['module']}")

    print("\nWhich to archive? (e.g. '0 3 5' or 'all' or 'none')")
    selection = input("> ").strip()
    if selection.lower() == "none":
        return

    to_archive = set()
    if selection.lower() == "all":
        to_archive = {e["module"] for e in cold}
    else:
        for idx in selection.split():
            try:
                to_archive.add(cold[int(idx)]["module"])
            except (ValueError, IndexError):
                pass

    if not to_archive:
        return

    archive_dir = ROOT / "builds" / "archive"
    archive_dir.mkdir(exist_ok=True)

    builds_dir = ROOT / "builds"
    moved = 0
    for f in sorted(builds_dir.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            record = json.loads(f.read_text())
            mods = set(record.get("modules_used", []))
            if mods & to_archive:
                f.rename(archive_dir / f.name)
                moved += 1
        except (json.JSONDecodeError, KeyError):
            pass

    record = {
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "modules": sorted(to_archive),
    }
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (archive_dir / f"_archive-{ts}.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    )

    print(f"[ARCHIVE] moved {moved} builds, {len(to_archive)} modules to archive/")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "archive":
        archive()
    else:
        stats()
