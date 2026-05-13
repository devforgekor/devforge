"""
core/file_io | JSONL and JSON file helpers: read, append, atomic write | read_jsonl(),append_jsonl(),atomic_write_jsonl(),read_json(),write_json()
"""
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file and return a list of dicts. Returns [] if file missing."""
    if not Path(path).exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Append rows to a JSONL file, creating parent directories as needed."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def atomic_write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write rows to a JSONL file atomically via a temp file and os.replace()."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent, encoding="utf-8") as tf:
        for row in rows:
            tf.write(json.dumps(row, ensure_ascii=False) + "\n")
        temp_name = tf.name
    os.replace(temp_name, path)


def read_json(path: Path, default: dict | None = None) -> dict:
    """Read a JSON file. Returns default (or {}) if file is missing or invalid."""
    p = Path(path)
    if not p.exists():
        return default if default is not None else {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def write_json(path: Path, data: dict) -> None:
    """Write data to a JSON file with indent=2, creating parent dirs as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
