#!/usr/bin/env python3
# Status: experimental
# Path: none — test only
"""ModelRegistry unit tests + parity with legacy MODEL_METADATA."""
from __future__ import annotations

import importlib.util
import pathlib

from devforge.domain.model_management.metadata import ModelMetadata
from devforge.domain.model_management.registry import ModelNotFoundError, ModelRegistry

LEGACY_PATH = pathlib.Path("/opt/projects/server/scripts/lib/model_registry.py")


def _load_legacy():
    spec = importlib.util.spec_from_file_location("legacy", LEGACY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MODEL_METADATA


def _build_registry() -> tuple[ModelRegistry, dict]:
    """Build registry from the legacy mapping (bridge pattern)."""
    raw = _load_legacy()
    meta = {}
    for key, val in raw.items():
        meta[key] = ModelMetadata(
            key=key,
            file=val["file"],
            port=val["port"],
            mode=val["mode"],
            model_name=val["model_name"],
            ctx=val.get("ctx", 8192),
            threads=val.get("threads", 4),
            threads_batch=val.get("threads_batch", 0),
            parallel=val.get("parallel", 1),
        )
    return ModelRegistry(meta), raw


def test_get_existing():
    reg, _ = _build_registry()
    m = reg.get("day-extractor")
    assert m.mode == "day"
    assert m.port == 8082


def test_get_missing_raises():
    reg, _ = _build_registry()
    try:
        reg.get("nonexistent")
        assert False, "should raise"
    except ModelNotFoundError:
        pass


def test_by_mode():
    reg, _ = _build_registry()
    embed = reg.by_mode("embed")
    assert len(embed) == 1
    assert embed[0].key == "embeder"


def test_parity_keys():
    """devforge registry keys must equal legacy MODEL_METADATA keys."""
    reg, legacy = _build_registry()
    assert set(reg.keys()) == set(legacy.keys())


def test_parity_fields():
    """devforge registry fields must match legacy values (for overlapping fields)."""
    reg, legacy = _build_registry()
    for key in reg:
        m = reg.get(key)
        lv = legacy[key]
        assert m.file == lv["file"], f"{key}: file mismatch"
        assert m.port == lv["port"], f"{key}: port mismatch"
        assert m.mode == lv["mode"], f"{key}: mode mismatch"
        assert m.model_name == lv["model_name"], f"{key}: model_name mismatch"
