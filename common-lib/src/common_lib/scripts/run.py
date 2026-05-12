#!/usr/bin/env python3
"""scripts/run | recipe runner: load JSON recipe → resolve deps → execute steps → record build | run_recipe()"""

import sys
import os
import json
import importlib
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent


def _resolve(val, namespace, config):
    if isinstance(val, str) and val.startswith("$"):
        name = val[1:]
        if name in config:
            return config[name]
        if name in namespace:
            return namespace[name]
        return os.environ.get(name, "")
    if isinstance(val, dict):
        return {k: _resolve(v, namespace, config) for k, v in val.items()}
    if isinstance(val, list):
        return [_resolve(v, namespace, config) for v in val]
    return val


def _record(recipe, modules_used, config_path):
    builds_dir = ROOT / "builds"
    builds_dir.mkdir(exist_ok=True)
    record = {
        "recipe": recipe["name"],
        "summary": recipe.get("summary", ""),
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "modules_used": modules_used,
        "result": "ok",
    }
    if config_path:
        record["config"] = config_path
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = builds_dir / f"{recipe['name']}-{ts}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    return path


def run_recipe(name, config_path=None):
    recipe_path = ROOT / "recipes" / f"{name}.json"
    if not recipe_path.is_file():
        print(f"ERROR: recipe not found: {recipe_path}", file=sys.stderr)
        sys.exit(1)

    try:
        recipe = json.loads(recipe_path.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON in {recipe_path}: {e}", file=sys.stderr)
        sys.exit(1)

    if "steps" not in recipe:
        print(f"ERROR: recipe missing 'steps' key", file=sys.stderr)
        sys.exit(1)

    config = {}
    if config_path:
        cfg_path = Path(config_path)
        if not cfg_path.is_file():
            print(f"ERROR: config file not found: {config_path}", file=sys.stderr)
            sys.exit(1)
        try:
            config = json.loads(cfg_path.read_text())
        except json.JSONDecodeError as e:
            print(f"ERROR: invalid JSON in config: {e}", file=sys.stderr)
            sys.exit(1)

    namespace = {}
    modules_used = []

    for i, step in enumerate(recipe["steps"]):
        if "module" not in step or "call" not in step:
            print(f"ERROR: step {i} missing 'module' or 'call'", file=sys.stderr)
            sys.exit(1)
        try:
            mod = importlib.import_module(step["module"])
        except ModuleNotFoundError as e:
            print(f"ERROR: step {i} import failed: {e}", file=sys.stderr)
            sys.exit(1)

        modules_used.append(step["module"])
        fn = getattr(mod, step["call"], None)
        if fn is None:
            print(f"ERROR: step {i} function not found: {step['module']}.{step['call']}", file=sys.stderr)
            sys.exit(1)

        args = _resolve(step.get("with", {}), namespace, config)

        try:
            result = fn(**args) if isinstance(args, dict) else fn(args)
        except Exception as e:
            print(f"ERROR: step {i} {step['module']}.{step['call']}() failed: {e}", file=sys.stderr)
            sys.exit(1)

        if "save" in step:
            namespace[step["save"]] = result

    record_path = _record(recipe, modules_used, config_path)
    print(f"[OK] {name}")
    print(f"[BUILD] {record_path.relative_to(ROOT)}")
    return namespace


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Run a recipe from recipes/")
    p.add_argument("recipe", help="Recipe name (without .json)")
    p.add_argument("--config", "-c", help="Project config JSON file", default=None)
    args = p.parse_args()
    run_recipe(args.recipe, args.config)
